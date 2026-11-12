"""RPC contention must not stall relay, command, or album coroutines."""
import ast
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import deltachat2
except ImportError:
    sys.modules['deltachat2'] = MagicMock()
try:
    import deltabot_cli
except ImportError:
    sys.modules['deltabot_cli'] = MagicMock()

import bot
import database
import tg_albums
import tg_commands
import tg_events

MODULES = (
    'tg_events.py', 'tg_albums.py', 'relay.py', 'userbot.py',
    'logging_setup.py', 'bot.py', 'dc_helpers.py', 'tg_commands.py',
    'dc_commands.py',
)

# This whole synchronous helper runs through loop.run_in_executor; each RPC
# therefore already runs on a worker thread. Do not add to_thread inside it.
ALLOWLIST = {
    ('tg_commands.py', 'tg_channels_command._build_channels_text', method)
    for method in (
        '_bot_module.dc_bot_instance.rpc.get_basic_chat_info',
        '_bot_module.dc_bot_instance.rpc.get_chat_contacts',
        '_bot_module.dc_bot_instance.rpc.get_contact',
    )
}


def dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return base + '.' + node.attr if base else ''
    return ''


def blocking_calls(source, filename):
    """Inspect async bodies, including nested defs, without confusing attributes
    passed as worker callables with actual calls executed on the event loop.
    """
    found = []

    class Visitor(ast.NodeVisitor):
        in_async = False
        scope = ()

        def visit_FunctionDef(self, node):
            old = self.scope
            self.scope += (node.name,)
            self.generic_visit(node)
            self.scope = old

        def visit_AsyncFunctionDef(self, node):
            old = self.in_async
            self.in_async = True
            self.visit_FunctionDef(node)
            self.in_async = old

        def visit_Call(self, node):
            name = dotted_name(node.func)
            key = (filename, '.'.join(self.scope), name)
            if self.in_async and ('.rpc.' in name or name.endswith('_dc_send_msg_with_stats')):
                if key not in ALLOWLIST:
                    found.append((node.lineno, key))
            # A lambda supplied as the callable executes on the worker. Arguments
            # are still evaluated on the loop, so to_thread(bot.rpc.foo()) fails.
            worker_arg = None
            if name == 'asyncio.to_thread':
                worker_arg = 0
            elif name.endswith('.run_in_executor'):
                worker_arg = 1
            for index, arg in enumerate(node.args):
                if index == worker_arg and isinstance(arg, ast.Lambda):
                    continue
                self.visit(arg)
            self.visit(node.func)
            for keyword in node.keywords:
                self.visit(keyword.value)

    Visitor().visit(ast.parse(source))
    return found


class TestRpcCallSites(unittest.TestCase):
    def test_no_rpc_calls_on_event_loop(self):
        root = Path(__file__).resolve().parents[1]
        found = []
        for filename in MODULES:
            found.extend(blocking_calls((root / filename).read_text(), filename))
        self.assertEqual(found, [], '\n'.join(str(item) for item in found))

    def test_allowlisted_helper_still_runs_in_executor(self):
        source = (Path(__file__).resolve().parents[1] / 'tg_commands.py').read_text()
        tree = ast.parse(source)
        handler = next(node for node in tree.body
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == 'tg_channels_command')
        calls = [node for node in ast.walk(handler) if isinstance(node, ast.Call)]
        self.assertFalse(any(dotted_name(node.func) == '_build_channels_text' for node in calls))
        worker_calls = [node for node in calls
                        if dotted_name(node.func) == 'loop.run_in_executor'
                        and len(node.args) > 1
                        and dotted_name(node.args[1]) == '_build_channels_text']
        self.assertEqual(len(worker_calls), 1)

    def test_scanner_distinguishes_worker_callables_and_eager_calls(self):
        source = '''
def command():
    async def run():
        bot.rpc.get_message(1, 2).get("text")
        await asyncio.to_thread(bot.rpc.send_msg, 1, 2, msg)
        await asyncio.to_thread(bot.rpc.send_msg(1, 2, msg))
        await asyncio.to_thread(lambda: bot.rpc.get_contact(1, 1))
        await loop.run_in_executor(None, lambda: bot.rpc.get_contact(1, 1))
        def helper():
            bot.rpc.delete_chat(1, 2)
'''
        found = blocking_calls(source, 'example.py')
        self.assertEqual([key[2] for _, key in found], [
            'bot.rpc.get_message', 'bot.rpc.send_msg', 'bot.rpc.delete_chat',
        ])
        self.assertEqual(found[-1][1][1], 'command.run.helper')


class SlowRpc:
    def __init__(self):
        self.calls = []

    def _block(self, method, args, result):
        started = time.monotonic()
        time.sleep(0.5)  # Simulate waiting for the shared RpcProxy lock.
        self.calls.append((method, args, threading.get_ident(), started, time.monotonic()))
        return result

    def send_msg(self, *args):
        return self._block('send_msg', args, 501)

    def get_chat_securejoin_qr_code(self, *args):
        return self._block('get_chat_securejoin_qr_code', args, 'OPEN:invite')


class TestEventLoopProgress(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp.name, 'bridge.db')
        database.init_db()
        database.add_bridge(42, -1001)
        self.rpc = SlowRpc()
        self.instance = SimpleNamespace(rpc=self.rpc)
        self.patches = [
            patch.object(bot, 'dc_bot_instance', self.instance),
            patch.object(bot, 'dc_accid', 1),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        database.DB_PATH = self.original_db
        self.temp.cleanup()

    async def assert_loop_progress(self, handler):
        ticks = []
        stop = asyncio.Event()

        async def ticker():
            while not stop.is_set():
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0)  # Start the independent ticker before the handler.
        loop_thread = threading.get_ident()
        try:
            await handler
        finally:
            stop.set()
            await task
        ticks.append(time.monotonic())
        self.assertTrue(self.rpc.calls, 'Handler must exercise the real RPC call')
        self.assertLess(max(b - a for a, b in zip(ticks, ticks[1:])), 0.15)
        for _, _, thread, started, ended in self.rpc.calls:
            self.assertNotEqual(thread, loop_thread)
            self.assertGreaterEqual(sum(started < tick < ended for tick in ticks), 5)

    def message(self, msg_id=7):
        msg = MagicMock()
        for attr in ('caption', 'photo', 'video', 'animation', 'voice', 'audio',
                     'document', 'sticker', 'video_note', 'venue', 'location',
                     'poll', 'reply_to_message', 'paid_media', 'story', 'giveaway',
                     'contact', 'invoice', 'dice', 'game', 'forward_origin'):
            setattr(msg, attr, None)
        msg.message_id = msg_id
        msg.text = 'Hello'
        msg.entities = msg.caption_entities = []
        msg.from_user = SimpleNamespace(id=7, first_name='Alice', last_name=None, is_bot=False)
        msg.chat = SimpleNamespace(id=-1001, username=None, title='Group')
        msg.media_group_id = None
        return msg

    async def test_hot_message_relay_does_not_block(self):
        msg = self.message()
        update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=-1001, type='supergroup'))
        with patch.object(tg_events, '_mark_processed', return_value=False), \
             patch.object(tg_events, '_is_rate_limited', return_value=False), \
             patch.object(tg_events, '_wait_for_global_dc_rate_limit', AsyncMock()), \
             patch.object(tg_events.group_watch, 'note_outcome'), \
             patch.object(tg_events, '_edit_timestamps', {}):
            await self.assert_loop_progress(tg_events.handle_tg_message(update, None))
        self.assertEqual(database.get_dc_msg_id(7, -1001, 42), 501)
        self.assertEqual(self.rpc.calls[0][1][2].text, 'Hello')

    async def test_invite_command_does_not_block(self):
        update = SimpleNamespace(effective_chat=SimpleNamespace(type='private'),
                                 effective_message=SimpleNamespace(reply_text=AsyncMock()))
        with patch.object(tg_commands, '_check_invite_permissions', AsyncMock(return_value=True)):
            await self.assert_loop_progress(tg_commands.tg_invite_command(update, None))
        self.assertIn('https://i.delta.chat/#invite', update.effective_message.reply_text.call_args.args[0])

    async def test_album_relay_does_not_block(self):
        msg = self.message()
        msg.caption = 'Album'
        msg.photo = [object()]
        msg.date = None
        items = [{'update': SimpleNamespace(message=msg), 'kind': 'photo'}]
        path = os.path.join(self.temp.name, 'photo.jpg')
        Path(path).write_bytes(b'photo')
        with patch.object(tg_albums, '_download_item', AsyncMock(return_value=path)), \
             patch.object(tg_albums, '_download_author_avatar', AsyncMock(return_value=None)), \
             patch.object(tg_albums, '_wait_for_global_dc_rate_limit', AsyncMock()), \
             patch.object(bot, '_package_tg_post_webxdc', AsyncMock(return_value=True)):
            await self.assert_loop_progress(tg_albums._relay_album(-1001, 'album1', items))
        self.assertEqual(database.get_dc_msg_id(7, -1001, 42), 501)
        self.assertFalse(os.path.exists(path))


if __name__ == '__main__':
    unittest.main()
