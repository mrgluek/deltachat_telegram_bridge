import asyncio
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import database
import security
import userbot
from rpc_proxy import RpcProxy

TEST_DB = "test_dedup_and_rpc.db"


class TestUserbotDoesNotClaimGroupMessages(unittest.TestCase):
    """The userbot must not mark messages of chats it does not bridge as processed:
    the mark is shared with the Bot API handlers, which then dropped the message."""

    def setUp(self):
        database.DB_PATH = TEST_DB
        database.init_db()
        security._processed_tg_msgs.clear()
        self._orig = (bot.userbot_client, bot.dc_bot_instance, bot.dc_accid)
        bot.userbot_client = MagicMock()
        bot.userbot_client.is_connected.return_value = True
        bot.dc_bot_instance = MagicMock()
        bot.dc_accid = 1

    def tearDown(self):
        bot.userbot_client, bot.dc_bot_instance, bot.dc_accid = self._orig
        security._processed_tg_msgs.clear()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)

    def _event(self, chat_id, msg_id, username=None):
        event = MagicMock()
        event.message.out = False
        event.message.sender_id = 42
        event.message.chat_id = chat_id
        event.message.id = msg_id
        event.message.chat.username = username
        return event

    def test_message_in_bot_api_group_is_left_to_bot_api(self):
        with patch.object(userbot, "_get_cached_dc_channel_chat_id", return_value=None):
            asyncio.run(userbot._process_userbot_event_internal(self._event(-1001041396328, 120409)))
        # The Bot API handler must still see it as new
        self.assertFalse(security._mark_processed(-1001041396328, 120409))

    def test_message_in_bridged_channel_is_marked(self):
        with patch.object(userbot, "_get_cached_dc_channel_chat_id", return_value=109), \
             patch("database.get_dc_msg_id", return_value=555):  # already relayed -> stops early
            asyncio.run(userbot._process_userbot_event_internal(self._event(-1001271343429, 7)))
        self.assertTrue(security._mark_processed(-1001271343429, 7))


class TestRpcProxyLongPoll(unittest.TestCase):
    def test_long_poll_does_not_block_other_calls(self):
        release = threading.Event()

        class FakeRpc:
            def get_next_event(self):
                release.wait(5)
                return "event"

            def send_msg(self, *args):
                return 1

        proxy = RpcProxy(FakeRpc())
        poller = threading.Thread(target=proxy.get_next_event)
        poller.start()
        time.sleep(0.05)  # poller is now waiting inside get_next_event
        start = time.monotonic()
        self.assertEqual(proxy.send_msg(1, 2), 1)
        self.assertLess(time.monotonic() - start, 1.0)
        release.set()
        poller.join()

    def test_regular_calls_are_still_serialized(self):
        active = []
        overlap = []

        class FakeRpc:
            def send_msg(self, *args):
                active.append(1)
                if len(active) > 1:
                    overlap.append(1)
                time.sleep(0.02)
                active.pop()

        proxy = RpcProxy(FakeRpc())
        threads = [threading.Thread(target=proxy.send_msg) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(overlap, [])


if __name__ == "__main__":
    unittest.main()
