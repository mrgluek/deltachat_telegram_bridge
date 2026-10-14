import asyncio
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import bot
import database
import userbot

TEST_DB = "test_userbot_queue.db"


class MockEntity:
    left = False


class MockMsg:
    def __init__(self, msg_id, chat_id, age_seconds=0):
        self.id = msg_id
        self.chat_id = chat_id
        self.date = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)


class TestUserbotQueue(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = TEST_DB
        database.init_db()
        userbot._channel_pending.clear()
        userbot._channel_queued_max.clear()
        bot._last_msg_id_cache.clear()

    def tearDown(self):
        userbot._channel_pending.clear()
        userbot._channel_queued_max.clear()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)

    def _run_reconcile(self, tg_id, latest_id, age_seconds=0):
        """Reconcile one channel whose newest post is latest_id; return (result, queued ids, min_id used)."""
        chan = database.get_channel_by_tg_id(tg_id)
        calls = {}

        async def get_entity(target):
            return MockEntity()

        async def get_messages(entity, limit=None, min_id=None, reverse=None):
            if limit == 1 and min_id is None:
                return [MockMsg(latest_id, tg_id)]
            calls["min_id"] = min_id
            return [MockMsg(i, tg_id, age_seconds) for i in range(min_id + 1, latest_id + 1)]

        ub = MagicMock()
        ub.is_connected.return_value = True
        ub.get_entity = get_entity
        ub.get_messages = get_messages
        queued_ids = []

        async def fake_queue(cid, evt_type, evt):
            queued_ids.append(evt.message.id)

        with patch.object(bot, "userbot_client", ub), patch.object(bot, "_queue_userbot_event", fake_queue):
            res = asyncio.run(userbot.reconcile_channel_detailed(chan))
        return res, queued_ids, calls.get("min_id")

    def test_reconcile_skips_posts_already_queued(self):
        database.add_channel_by_id(tg_channel_id=-1001234, dc_chat_id=500, username="queued_ch")
        bot._update_cached_last_msg_id(-1001234, 100)
        # Posts 101..105 arrived live and are still waiting for the rate limiter.
        key = userbot._chan_key(-1001234)
        userbot._channel_pending[key] = 5
        userbot._channel_queued_max[key] = 105

        res, queued_ids, min_id = self._run_reconcile(-1001234, latest_id=107)

        self.assertEqual(min_id, 105)
        self.assertEqual(queued_ids, [106, 107])
        self.assertEqual(res.queued, 2)

    def test_reconcile_refetches_after_queue_drained(self):
        # Queue drained but watermark stayed at 100 (relay failed): fetch from the watermark again.
        database.add_channel_by_id(tg_channel_id=-1001235, dc_chat_id=501, username="drained_ch")
        bot._update_cached_last_msg_id(-1001235, 100)
        userbot._channel_queued_max[userbot._chan_key(-1001235)] = 105

        res, queued_ids, min_id = self._run_reconcile(-1001235, latest_id=103)

        self.assertEqual(min_id, 100)
        self.assertEqual(queued_ids, [101, 102, 103])

    def test_reconcile_counts_stale_posts(self):
        database.add_channel_by_id(tg_channel_id=-1001236, dc_chat_id=502, username="stale_ch")
        bot._update_cached_last_msg_id(-1001236, 10)

        res, _, _ = self._run_reconcile(-1001236, latest_id=12, age_seconds=userbot.STALE_MISS_AGE + 60)
        self.assertEqual(res.stale, 2)

        bot._update_cached_last_msg_id(-1001236, 12)
        res, _, _ = self._run_reconcile(-1001236, latest_id=13, age_seconds=5)
        self.assertEqual(res.stale, 0)

    def test_queue_bookkeeping_tracks_pending(self):
        async def scenario():
            processed = asyncio.Event()

            async def fake_process(event, is_edit=False):
                processed.set()

            with patch.object(userbot, "_process_userbot_event_internal", fake_process):
                evt = MagicMock()
                evt.message.id = 42
                await userbot._queue_userbot_event(-1009999, "new", evt)
                key = userbot._chan_key(-1009999)
                self.assertEqual(userbot._channel_queued_max[key], 42)
                await userbot._channel_queues[-1009999].join()
                self.assertTrue(processed.is_set())
                self.assertEqual(userbot.get_pending_event_count(), 0)
                userbot._channel_workers[-1009999].cancel()

        asyncio.run(scenario())

    def test_stall_detection_restarts_userbot(self):
        async def scenario(stale, silent_for):
            userbot._last_live_event_ts = time.time() - silent_for
            userbot._last_stall_restart_ts = 0.0
            start = AsyncMock()
            with patch.object(bot, "start_userbot", start), patch.object(bot, "tg_app", None):
                await userbot._check_live_updates_stall(stale)
            return start.await_count

        three = ["@a", "@b", "@c"]
        self.assertEqual(asyncio.run(scenario(three, userbot.STALL_NO_LIVE_SECONDS + 10)), 1)
        # Live events still arriving: misses are just a race, not a stall.
        self.assertEqual(asyncio.run(scenario(three, 30)), 0)
        # Too few channels affected.
        self.assertEqual(asyncio.run(scenario(["@a"], userbot.STALL_NO_LIVE_SECONDS + 10)), 0)

    def test_ensure_reconcile_loop_restarts_dead_task(self):
        async def scenario():
            async def dead():
                return None

            async def fake_loop():
                await asyncio.sleep(3600)

            userbot._reconcile_task = asyncio.create_task(dead())
            await asyncio.sleep(0)
            with patch.object(userbot, "reconcile_channels_loop", fake_loop):
                userbot.ensure_reconcile_loop()
                first = userbot._reconcile_task
                self.assertFalse(first.done())
                # Healthy task with a fresh heartbeat is left alone.
                userbot.ensure_reconcile_loop()
                self.assertIs(userbot._reconcile_task, first)
                # A stale heartbeat replaces it.
                userbot._reconcile_heartbeat = time.time() - userbot.RECONCILE_STALE_AFTER - 1
                userbot.ensure_reconcile_loop()
                self.assertIsNot(userbot._reconcile_task, first)
                await asyncio.sleep(0)
                self.assertTrue(first.cancelled())
                userbot._reconcile_task.cancel()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
