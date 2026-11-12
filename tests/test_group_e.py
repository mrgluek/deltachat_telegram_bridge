"""Regression tests for audit findings 14–17 and 26."""
import asyncio
import gzip
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

for dependency in ("deltachat2", "deltabot_cli"):
    try:
        __import__(dependency)
    except ImportError:
        sys.modules[dependency] = MagicMock()

import httpx
import bot
import database
import dc_events
import formatting
import media
import rpc_proxy
import runtime_patches
import userbot


class DbTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp.name, "test.db")
        database.init_db()
        self.addCleanup(setattr, database, "DB_PATH", old)
        self.addCleanup(setattr, userbot, '_bridged_chats_expires', 0.0)
        userbot._bridged_chats_expires = 0.0


class Stream:
    def __init__(self, size=0, body=None, status=200, headers=None):
        self.size = size if body is None else len(body)
        self.body = body
        self.status_code = status
        self.headers = headers or {}
        self.read = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def aiter_bytes(self, chunk_size=65536):
        while self.read < self.size:
            start = self.read
            self.read = min(self.read + chunk_size, self.size)
            yield self.body[start:self.read] if self.body is not None else b'x' * (self.read - start)


class TestDownloads(DbTest):
    def setUp(self):
        super().setUp()
        # Use the real validator with deterministic public DNS; no network is contacted.
        self.dns = patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('149.154.167.99', 443))])
        self.dns.start()
        self.addCleanup(self.dns.stop)
        self.addCleanup(setattr, rpc_proxy, '_download_semaphore', None)
        rpc_proxy._download_semaphore = None

    def test_images_stop_at_cap(self):
        cap = 20 * 1024 * 1024
        for download in (media._download_image_url, media._download_image_to_file):
            response = Stream(size=100 * 1024 * 1024)
            path = os.path.join(self.temp.name, 'image.jpg')
            args = ('https://t.me/image.jpg',) if download is media._download_image_url else ('https://t.me/image.jpg', path)
            async def buffered(*args, **kwargs):
                response.read = response.size
                return httpx.Response(200, content=b'x' * (cap + 1))
            with patch('httpx.AsyncClient.stream', return_value=response), patch('httpx.AsyncClient.get', side_effect=buffered):
                result = asyncio.run(download(*args))
            self.assertEqual(result, None if download is media._download_image_url else False)
            self.assertLessEqual(response.read, cap + 65536)
            self.assertGreater(response.read, cap)
            self.assertTrue(response.closed)
            self.assertFalse(os.path.exists(path))

    def test_embed_stops_at_cap(self):
        response = Stream(size=100 * 1024 * 1024)
        with patch('httpx.AsyncClient.stream', return_value=response):
            self.assertIsNone(asyncio.run(media._extract_public_tg_post_rich('channel', 1)))
        self.assertEqual(response.read, 2 * 1024 * 1024 + 65536)
        self.assertTrue(response.closed)

    def test_content_length_rejected_without_reading(self):
        for kind in ('image', 'html', 'video'):
            response = Stream(size=100 * 1024 * 1024, headers={'content-length': str(100 * 1024 * 1024)})
            with patch('httpx.AsyncClient.stream', return_value=response):
                if kind == 'image':
                    result = asyncio.run(media._download_image_url('https://t.me/image.jpg'))
                elif kind == 'html':
                    result = asyncio.run(media._extract_public_tg_post_rich('channel', 1))
                else:
                    result = asyncio.run(media._download_video_with_limit('https://t.me/video.mp4', os.path.join(self.temp.name, 'video'), 1024))
            self.assertFalse(result)
            self.assertEqual(response.read, 0)

    def test_video_stops_and_removes_partial_file(self):
        response = Stream(size=100 * 1024 * 1024)
        path = os.path.join(self.temp.name, 'video.mp4')
        with patch('httpx.AsyncClient.stream', return_value=response):
            self.assertFalse(asyncio.run(media._download_video_with_limit('https://t.me/video.mp4', path, 100000)))
        self.assertLessEqual(response.read, 100000 + 65536)
        self.assertFalse(os.path.exists(path))

    def test_redirects_checked_on_each_hop(self):
        for target in ('http://127.0.0.1/secret', 'http://[::1]/secret', 'file:///secret'):
            with self.subTest(target=target):
                response = Stream(status=302, headers={'location': target})
                with patch('httpx.AsyncClient.stream', return_value=response) as request:
                    self.assertIsNone(asyncio.run(media._download_image_url('https://t.me/image.jpg')))
                self.assertEqual(request.call_count, 1)
                self.assertTrue(response.closed)

    def test_redirect_guard_with_real_httpx_redirect_handling(self):
        real_client = httpx.AsyncClient
        for target, expected_requests in (('http://127.0.0.1/secret', 1), ('https://t.me/next', 4)):
            with self.subTest(target=target):
                requests = []
                def respond(request):
                    requests.append(str(request.url))
                    if request.url.host == '127.0.0.1':
                        return httpx.Response(200, content=b'private')
                    return httpx.Response(302, headers={'location': target})
                transport = httpx.MockTransport(respond)
                with patch('httpx.AsyncClient', side_effect=lambda **kw: real_client(transport=transport, **kw)):
                    path = asyncio.run(media._download_image_url('https://t.me/image.jpg'))
                if path:
                    self.addCleanup(os.unlink, path)
                self.assertIsNone(path)
                self.assertEqual(len(requests), expected_requests)

    def test_allowed_redirect_and_three_hop_limit(self):
        final = Stream(body=b'image')
        responses = [Stream(status=302, headers={'location': 'https://cdn4.telesco.pe/image.jpg'}), final]
        with patch('httpx.AsyncClient.stream', side_effect=responses) as request:
            path = asyncio.run(media._download_image_url('https://t.me/image.jpg'))
        self.addCleanup(os.unlink, path)
        with open(path, 'rb') as image:
            self.assertEqual(image.read(), b'image')
        self.assertEqual(request.call_args_list[1].args[1], 'https://cdn4.telesco.pe/image.jpg')
        redirects = [Stream(status=302, headers={'location': '/next'}) for _ in range(4)]
        with patch('httpx.AsyncClient.stream', side_effect=redirects) as request:
            self.assertIsNone(asyncio.run(media._download_image_url('https://t.me/image.jpg')))
        self.assertEqual(request.call_count, 4)
        self.assertTrue(all(response.closed for response in redirects))
        with patch('httpx.AsyncClient.stream', side_effect=redirects[:3] + [Stream(body=b'ok')]):
            path = asyncio.run(media._download_image_url('https://t.me/image.jpg'))
        self.addCleanup(os.unlink, path)

    def test_embed_https_redirect_and_video_redirect_guard(self):
        for kind in ('embed', 'video'):
            target = 'http://t.me/embed' if kind == 'embed' else 'http://127.0.0.1/video'
            response = Stream(status=302, headers={'location': target})
            with patch('httpx.AsyncClient.stream', return_value=response) as request:
                if kind == 'embed':
                    self.assertIsNone(asyncio.run(media._extract_public_tg_post_rich('channel', 1)))
                else:
                    self.assertFalse(asyncio.run(media._download_video_with_limit('https://t.me/video', os.path.join(self.temp.name, 'video'), 1024)))
            self.assertEqual(request.call_count, 1)

    def test_dns_failure_and_private_redirect_dns_fail_closed(self):
        with patch('socket.getaddrinfo', side_effect=socket.gaierror('offline')):
            self.assertFalse(formatting._is_safe_telegram_url('https://t.me/post'))
        with patch('socket.getaddrinfo', return_value=[]):
            self.assertFalse(formatting._is_safe_telegram_url('https://t.me/post'))
        def resolve(host, *args):
            return [(2, 1, 6, '', ('127.0.0.1' if host == 'cdn4.telesco.pe' else '149.154.167.99', 443))]
        response = Stream(status=302, headers={'location': 'https://cdn4.telesco.pe/image'})
        with patch('socket.getaddrinfo', side_effect=resolve), patch('httpx.AsyncClient.stream', return_value=response) as request:
            self.assertIsNone(asyncio.run(media._download_image_url('https://t.me/image')))
        self.assertEqual(request.call_count, 1)

    def test_download_concurrency(self):
        async def scenario():
            active = peak = 0
            release = asyncio.Event()
            class BlockingStream(Stream):
                async def aiter_bytes(self, chunk_size=65536):
                    nonlocal active, peak
                    active += 1
                    peak = max(peak, active)
                    await release.wait()
                    active -= 1
                    yield b'ok'
            async def download():
                async with media._fetch_telegram_bytes('https://t.me/image', {}, 15, 100):
                    pass
            with patch('httpx.AsyncClient.stream', side_effect=lambda *a, **k: BlockingStream()):
                tasks = [asyncio.create_task(download()) for _ in range(8)]
                for _ in range(8):
                    await asyncio.sleep(0)
                self.assertEqual(active, 3)
                release.set()
                await asyncio.gather(*tasks)
            self.assertEqual(peak, 3)
        asyncio.run(scenario())

    def test_decoded_bytes_and_charset(self):
        real_client = httpx.AsyncClient
        class Compressed(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield gzip.compress(('café' * 100).encode('latin-1'))
        def response(request):
            return httpx.Response(200, headers={'content-encoding': 'gzip', 'content-type': 'text/html; charset=iso-8859-1'}, stream=Compressed())
        async def scenario():
            async with media._fetch_telegram_bytes('https://t.me/post', {}, 15, 500) as response:
                self.assertEqual(response.text, 'café' * 100)
            async with media._fetch_telegram_bytes('https://t.me/post', {}, 15, 399) as response:
                self.assertIsNone(response)
        with patch('httpx.AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(response), **kw)):
            asyncio.run(scenario())


class TestPreviews(DbTest):
    def setUp(self):
        super().setUp()
        if hasattr(dc_events, '_preview_requests'):
            dc_events._preview_requests.clear()

    def test_sender_limit_before_scheduling_and_expiry(self):
        dc = MagicMock()
        dc.has_command.return_value = False
        dc.rpc.get_basic_chat_info.return_value = {'type': 2}
        dc.rpc.get_contact.return_value = NS(is_bot=False)
        event = NS(msg=NS(id=10, chat_id=20, from_id=30, text='https://t.me/channel/1', is_bot=False), command='')
        def scheduled(coro, loop):
            coro.close()
        with patch.object(bot, 'main_loop', MagicMock()), patch.object(bot, 'bot_contact_id', None), \
                patch('asyncio.run_coroutine_threadsafe', side_effect=scheduled) as schedule, \
                patch.object(dc_events.time, 'monotonic', return_value=100) as now:
            for _ in range(6):
                dc_events.handle_dc_message(dc, 1, event)
            self.assertEqual(schedule.call_count, 5)
            dc.rpc.send_reaction.assert_called_with(1, 10, ['⛔'])
            event.msg.from_id = 31
            dc_events.handle_dc_message(dc, 1, event)
            self.assertEqual(schedule.call_count, 6)
            now.return_value = 160
            event.msg.from_id = 30
            dc_events.handle_dc_message(dc, 1, event)
            self.assertEqual(schedule.call_count, 7)

    def test_preview_work_semaphore(self):
        async def scenario():
            active = peak = 0
            release = asyncio.Event()
            async def extract(*args):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await release.wait()
                active -= 1
                return None
            with patch.object(bot, 'userbot_client', None), patch.object(bot, '_extract_public_tg_post_rich', extract):
                tasks = [asyncio.create_task(dc_events._async_handle_direct_tg_post(MagicMock(), 1, 2, 'channel', i + 1)) for i in range(9)]
                for _ in range(8):
                    await asyncio.sleep(0)
                self.assertEqual(active, 3)
                release.set()
                await asyncio.gather(*tasks)
            self.assertEqual(peak, 3)
        asyncio.run(scenario())

    def test_cached_preview_waits_for_global_limiter(self):
        database.add_cached_tg_post('channel/1', 'text', 'cached')
        calls = []
        async def limit():
            calls.append('limit')
        def send(*args):
            calls.append('send')
        with patch.object(dc_events, '_wait_for_global_dc_rate_limit', side_effect=limit, create=True), patch.object(bot, '_dc_send_msg_with_stats', side_effect=send):
            asyncio.run(dc_events._async_handle_direct_tg_post(MagicMock(), 1, 2, 'channel', 1))
        self.assertEqual(calls, ['limit', 'send'])


class TestUserbotQueues(DbTest):
    def test_unbridged_events_allocate_nothing_and_cache_registry(self):
        async def scenario():
            event = NS(chat_id=-100876543, message=NS(id=1, chat=None))
            try:
                with patch.object(database, 'get_all_channels', wraps=database.get_all_channels) as lookup:
                    for kind in ('new', 'edit', 'delete'):
                        await userbot._queue_userbot_event(event.chat_id, kind, event)
                    self.assertNotIn(event.chat_id, userbot._channel_queues)
                    self.assertNotIn(event.chat_id, userbot._channel_workers)
                    self.assertEqual(lookup.call_count, 1)
            finally:
                task = userbot._channel_workers.pop(event.chat_id, None)
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                userbot._channel_queues.pop(event.chat_id, None)
        asyncio.run(scenario())

    def test_bridged_channel_group_and_username_allocate_workers(self):
        database.add_channel_by_id(-100876544, 10, username='channel')
        database.add_bridge(11, -100876545)
        async def scenario():
            for chat_id, username in ((-100876544, None), (-100876545, None), (-100876546, 'channel')):
                event = NS(message=NS(id=1, chat=NS(username=username)))
                with patch.object(userbot, '_process_userbot_event_internal', AsyncMock()):
                    await userbot._queue_userbot_event(chat_id, 'new', event)
                    self.assertIn(chat_id, userbot._channel_queues)
                    self.assertIn(chat_id, userbot._channel_workers)
                    self.assertEqual(userbot._channel_queues[chat_id].maxsize, 0)
                    await userbot._channel_queues[chat_id].join()
                    task = userbot._channel_workers.pop(chat_id)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    userbot._channel_queues.pop(chat_id)
        asyncio.run(scenario())

    def test_service_codes_bypass_queues(self):
        event = NS(chat_id=777000)
        with patch.object(userbot, '_process_userbot_event_internal', AsyncMock()) as process, patch.object(bot, '_queue_userbot_event', AsyncMock()) as queue:
            asyncio.run(userbot._process_userbot_event(event))
        process.assert_awaited_once_with(event, is_edit=False)
        queue.assert_not_awaited()


class TestRestart(DbTest):
    def test_hung_disconnect_aborts_restart_without_new_client(self):
        database.set_config('api_id', '123')
        database.set_config('api_hash', 'test')
        client = MagicMock()
        client.is_connected.return_value = True
        async def never():
            await asyncio.Event().wait()
        client.disconnect = never
        async def scenario():
            original_wait = asyncio.wait_for
            async def timeout(future, timeout):
                return await original_wait(future, 0.001)
            with patch.object(bot, 'userbot_client', client), patch.object(userbot, 'TelegramClient') as factory, \
                    patch.object(userbot.asyncio, 'wait_for', side_effect=timeout), \
                    patch.object(userbot, 'time', NS(monotonic=MagicMock(side_effect=[0, 16]))):
                self.assertFalse(await userbot.start_userbot())
                factory.assert_not_called()
                client.session.close.assert_not_called()
                self.assertIs(bot.userbot_client, client)
                self.assertFalse(userbot._is_starting_userbot)
        asyncio.run(scenario())

    def test_pending_disconnect_coro_blocks_session_close(self):
        async def scenario():
            async def _disconnect_coro():
                await asyncio.Event().wait()
            task = asyncio.create_task(_disconnect_coro())
            client = MagicMock()
            client.is_connected.return_value = False
            client.disconnect = AsyncMock()
            with patch.object(userbot, 'time', NS(monotonic=MagicMock(side_effect=[0, 16]))):
                self.assertFalse(await userbot._close_userbot_client(client))
            client.session.close.assert_not_called()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        asyncio.run(scenario())

    def test_clean_disconnect_closes_session(self):
        client = MagicMock()
        client.is_connected.return_value = False
        client.disconnect = AsyncMock()
        self.assertTrue(asyncio.run(userbot._close_userbot_client(client)))
        client.session.close.assert_called_once()

    def test_disconnect_can_finish_during_grace_period(self):
        async def scenario():
            release = asyncio.Event()
            client = MagicMock()
            client.is_connected.return_value = True
            order = []
            async def disconnect():
                await release.wait()
                client.is_connected.return_value = False
                order.append('disconnected')
            async def timeout(future, timeout):
                asyncio.get_running_loop().call_soon(release.set)
                raise asyncio.TimeoutError()
            client.disconnect = disconnect
            client.session.close.side_effect = lambda: order.append('closed')
            with patch.object(userbot.asyncio, 'wait_for', side_effect=timeout):
                self.assertTrue(await userbot._close_userbot_client(client))
            self.assertEqual(order, ['disconnected', 'closed'])
        asyncio.run(scenario())

    def test_session_close_failure_aborts_before_constructing_client(self):
        database.set_config('api_id', '123')
        database.set_config('api_hash', 'test')
        client = MagicMock()
        client.is_connected.return_value = False
        client.disconnect = AsyncMock()
        client.session.close.side_effect = sqlite3.OperationalError('busy')
        with patch.object(bot, 'userbot_client', client), patch.object(userbot, 'TelegramClient') as factory:
            self.assertFalse(asyncio.run(userbot.start_userbot()))
        factory.assert_not_called()
        self.assertFalse(userbot._is_starting_userbot)

    def test_backoff_values_and_success_reset(self):
        self.assertEqual([userbot._userbot_restart_delay(n) for n in range(1, 8)], [30, 60, 120, 240, 300, 300, 300])
        async def scenario():
            with patch.object(userbot, '_userbot_restart_failures', 0), patch.object(userbot, '_userbot_restart_after', 0), \
                    patch.object(bot, 'start_userbot', AsyncMock(return_value=False)), patch.object(userbot.time, 'monotonic', return_value=100):
                for delay in (30, 60, 120, 240, 300):
                    await userbot._restart_userbot_with_backoff()
                    self.assertEqual(userbot._userbot_restart_after, 100 + delay)
                with patch.object(bot, 'start_userbot', AsyncMock(return_value=True)):
                    await userbot._restart_userbot_with_backoff()
                self.assertEqual(userbot._userbot_restart_after, 0)
                self.assertEqual(userbot._userbot_restart_failures, 0)
        asyncio.run(scenario())

    def test_scheduler_respects_deadline_and_inflight_restart(self):
        async def scenario():
            with patch.object(userbot, '_userbot_restart_after', time.monotonic() + 300), patch.object(userbot, '_userbot_restart_task', None):
                userbot._schedule_userbot_restart()
                self.assertIsNone(userbot._userbot_restart_task)
            with patch.object(userbot, '_userbot_restart_after', 0), patch.object(userbot, '_userbot_restart_task', None), \
                    patch.object(userbot, '_restart_userbot_with_backoff', AsyncMock()) as restart:
                userbot._schedule_userbot_restart()
                first = userbot._userbot_restart_task
                userbot._schedule_userbot_restart()
                self.assertIs(userbot._userbot_restart_task, first)
                await first
                restart.assert_awaited_once()
        asyncio.run(scenario())

    def test_sqlite_busy_timeout_and_short_write_lock(self):
        from telethon.sessions import SQLiteSession
        runtime_patches.apply()
        path = os.path.join(self.temp.name, 'telethon.session')
        session = SQLiteSession(path)
        self.addCleanup(session.close)
        self.assertEqual(session._cursor().execute('PRAGMA busy_timeout').fetchone()[0], 30000)
        acquired = threading.Event()
        errors = []
        def hold_lock():
            try:
                with sqlite3.connect(path) as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    acquired.set()
                    time.sleep(1.2)
            except Exception as e:
                errors.append(e)
                acquired.set()
        thread = threading.Thread(target=hold_lock)
        thread.start()
        self.addCleanup(thread.join)
        self.assertTrue(acquired.wait(3))
        started = time.monotonic()
        session.set_dc(2, '149.154.167.50', 443)
        session.save()
        self.assertGreaterEqual(time.monotonic() - started, 1)
        thread.join()
        self.assertFalse(errors)
        session.close()
        # Lazy reopening must also receive the timeout.
        self.assertEqual(session._cursor().execute('PRAGMA busy_timeout').fetchone()[0], 30000)


if __name__ == '__main__':
    unittest.main()
