import asyncio
import os
import sys
import unittest
from collections import Counter
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: F401  (sets up the module graph the handlers import from)
import database
import group_watch
import security
import tg_events

TEST_DB = "test_group_watch.db"

MSG_ATTRS = (
    "paid_media", "photo", "video", "animation", "voice", "audio", "document",
    "sticker", "video_note", "story", "poll", "dice", "game", "contact", "venue",
    "location", "invoice", "giveaway", "giveaway_winners", "text", "caption",
    "entities", "caption_entities", "reply_to_message", "forward_origin",
)


def make_update(update_id=1, chat_id=-100123, msg_id=10, chat_type="supergroup", **fields):
    msg = MagicMock()
    for attr in MSG_ATTRS:
        setattr(msg, attr, None)
    for k, v in fields.items():
        setattr(msg, k, v)
    msg.message_id = msg_id
    msg.from_user.is_bot = False
    msg.from_user.first_name = "Alice"
    msg.from_user.last_name = None
    update = MagicMock()
    update.update_id = update_id
    update.message = msg
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    return update


def reset_watch():
    group_watch._inflight.clear()
    group_watch._stats.update(received=0, relayed=0, skipped=Counter(), stuck=0, unhandled=0, queue_stalls=0)
    group_watch._processed_updates = 0
    group_watch._queue_check.update(processed=0, waiting_since=None, warned=False)


class TestGroupWatch(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = TEST_DB
        database.init_db()
        reset_watch()
        security._processed_tg_msgs.clear()
        security._rate_limits.clear()

    def tearDown(self):
        reset_watch()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)

    def test_message_kind(self):
        self.assertEqual(group_watch.message_kind(make_update(photo=[1]).message), "photo")
        self.assertEqual(group_watch.message_kind(make_update(text="hi", forward_origin=object()).message),
                         "text (forwarded)")
        self.assertEqual(group_watch.message_kind(make_update().message), "other")

    @patch("database.get_dc_chats", return_value=[132])
    def test_only_bridged_groups_are_tracked(self, _):
        asyncio.run(group_watch.on_update_start(make_update(update_id=1, chat_type="private")))
        self.assertNotIn(1, group_watch._inflight)
        asyncio.run(group_watch.on_update_start(make_update(update_id=2)))
        self.assertIn(2, group_watch._inflight)

    @patch("database.get_dc_chats", return_value=[])
    def test_unbridged_group_not_tracked(self, _):
        asyncio.run(group_watch.on_update_start(make_update(update_id=3)))
        self.assertEqual(group_watch._inflight, {})

    @patch("database.get_dc_chats", return_value=[132])
    def test_outcomes_are_counted(self, _):
        for uid, outcome in ((1, "relayed"), (2, "skipped"), (3, None)):
            u = make_update(update_id=uid, msg_id=uid)
            asyncio.run(group_watch.on_update_start(u))
            if outcome:
                group_watch.note_outcome(u, outcome, "rate limit" if outcome == "skipped" else "")
            asyncio.run(group_watch.on_update_end(u))
        s = group_watch._stats
        self.assertEqual((s["received"], s["relayed"], s["unhandled"]), (3, 1, 1))
        self.assertEqual(s["skipped"]["rate limit"], 1)
        self.assertEqual(group_watch._inflight, {})
        line = group_watch.status_line()
        self.assertIn("3 received, 1 relayed, 1 skipped (rate limit: 1)", line)
        self.assertIn("1 not relayed", line)

    @patch("database.get_dc_chats", return_value=[132])
    def test_stuck_message_warned_once(self, _):
        u = make_update()
        asyncio.run(group_watch.on_update_start(u))
        start = group_watch._inflight[1]["since"]
        self.assertEqual(group_watch.check_stuck(0, now=start + 60), [])
        self.assertEqual(len(group_watch.check_stuck(0, now=start + 130)), 1)
        self.assertEqual(group_watch.check_stuck(0, now=start + 190), [])
        self.assertEqual(group_watch._stats["stuck"], 1)
        self.assertIn("1 stuck", group_watch.status_line())

    def test_blocked_queue_detected(self):
        group_watch._processed_updates = 5
        self.assertEqual(group_watch.check_stuck(3, now=1000), [])   # progress since last check
        self.assertEqual(group_watch.check_stuck(3, now=1060), [])   # no progress, starts timer
        self.assertEqual(len(group_watch.check_stuck(3, now=1190)), 1)
        self.assertEqual(group_watch.check_stuck(3, now=1250), [])   # warned only once
        group_watch._processed_updates = 6
        group_watch.check_stuck(3, now=1310)                         # progress resets
        self.assertIsNone(group_watch._queue_check["waiting_since"])

    def test_queue_with_progress_is_not_blocked(self):
        for i in range(5):
            group_watch._processed_updates += 1
            self.assertEqual(group_watch.check_stuck(4, now=1000 + i * 200), [])


class TestHandleTgMessageOutcomes(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = TEST_DB
        database.init_db()
        reset_watch()
        security._processed_tg_msgs.clear()
        security._rate_limits.clear()

    def tearDown(self):
        reset_watch()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)

    def _run(self, update):
        sender = MagicMock(return_value=555)
        with patch("database.get_dc_chats", return_value=[132]), \
             patch.object(bot, "dc_bot_instance", MagicMock()), \
             patch.object(bot, "dc_accid", 1), \
             patch.object(bot, "_dc_send_msg_with_stats", sender), \
             patch("tg_events._wait_for_global_dc_rate_limit", AsyncMock()), \
             patch("tg_events.track_dc_outgoing"):
            asyncio.run(group_watch.on_update_start(update))
            asyncio.run(tg_events.handle_tg_message(update, MagicMock()))
            asyncio.run(group_watch.on_update_end(update))
        return sender

    def test_text_message_relayed(self):
        sender = self._run(make_update(text="Сервер в Милане"))
        self.assertEqual(sender.call_args[0][3].text, "Сервер в Милане")
        self.assertEqual(group_watch._stats["relayed"], 1)

    def test_unsupported_type_arrives_as_note(self):
        sender = self._run(make_update(story=MagicMock()))
        self.assertIn("Story", sender.call_args[0][3].text)
        sender = self._run(make_update(update_id=2, msg_id=11))
        self.assertEqual(sender.call_args[0][3].text, "[Unsupported Telegram message: other]")
        self.assertEqual(group_watch._stats["relayed"], 2)

    def test_dice_is_relayed(self):
        dice = MagicMock(emoji="🎲", value=4)
        sender = self._run(make_update(dice=dice))
        self.assertEqual(sender.call_args[0][3].text, "🎲 4")

    def test_duplicate_is_logged_as_skipped(self):
        self._run(make_update(text="a"))
        with self.assertLogs("tg_dc_bridge", level="INFO") as logs:
            self._run(make_update(update_id=2, text="a"))
        self.assertTrue(any("duplicate update" in line for line in logs.output))
        self.assertEqual(group_watch._stats["skipped"]["duplicate update"], 1)

    def test_rate_limited_is_logged_as_skipped(self):
        with patch("tg_events._is_rate_limited", return_value=True):
            sender = self._run(make_update(text="a"))
        sender.assert_not_called()
        self.assertEqual(group_watch._stats["skipped"]["rate limit"], 1)

    def test_send_failure_is_counted(self):
        update = make_update(text="a")
        with patch("database.get_dc_chats", return_value=[132]), \
             patch.object(bot, "dc_bot_instance", MagicMock()), \
             patch.object(bot, "dc_accid", 1), \
             patch.object(bot, "_dc_send_msg_with_stats", MagicMock(side_effect=RuntimeError("rpc down"))), \
             patch("tg_events._wait_for_global_dc_rate_limit", AsyncMock()):
            asyncio.run(group_watch.on_update_start(update))
            asyncio.run(tg_events.handle_tg_message(update, MagicMock()))
            asyncio.run(group_watch.on_update_end(update))
        self.assertEqual(group_watch._stats["skipped"]["sending to Delta Chat failed"], 1)


if __name__ == "__main__":
    unittest.main()
