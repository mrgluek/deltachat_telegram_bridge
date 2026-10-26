"""Large attachments are downloaded only for bridged groups (download_limit 64 KB, see dc_events)."""
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
import database
import dc_events

TEST_DB = "test_attachment_download.db"
BRIDGED, OTHER, TG = 42, 43, -1001


def _msg(msg_id=7, chat_id=BRIDGED, state="Done", text="look at this", file=None):
    return SimpleNamespace(id=msg_id, chat_id=chat_id, from_id=12, text=text, file=file, download_state=state,
                           viewtype=None, override_sender_name=None, quote=None, is_info=False, is_bot=False)


class _Base(unittest.TestCase):
    def setUp(self):
        self.orig_db = database.DB_PATH
        database.DB_PATH = TEST_DB
        database.init_db()
        database.add_bridge(BRIDGED, TG) if hasattr(database, "add_bridge") else self._bridge()
        self.rpc = MagicMock()
        self.bot = MagicMock(rpc=self.rpc)
        self.bot.has_command.return_value = False
        self.rpc.get_basic_chat_info.return_value = {"type": 2}
        self.rpc.get_contact.return_value = SimpleNamespace(display_name="Alice", address="a@x.org", is_bot=False)
        self.patches = [patch.object(bot, "tg_app", MagicMock()), patch.object(bot, "main_loop", MagicMock()),
                        patch.object(dc_events, "_is_rate_limited", return_value=False),
                        patch.object(dc_events, "_schedule_pending_timeout")]
        for p in self.patches:
            p.start()

    def _bridge(self):
        conn = database._connect()
        conn.cursor().execute("INSERT INTO bridges (dc_chat_id, tg_chat_id) VALUES (?, ?)", (BRIDGED, TG))
        conn.commit()
        conn.close()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        database.DB_PATH = self.orig_db
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)

    def _incoming(self, msg):
        dc_events.handle_dc_message(self.bot, 1, SimpleNamespace(msg=msg, command=""))

    def _changed(self, msg_id=7, chat_id=BRIDGED):
        dc_events.handle_dc_message_changed(self.bot, 1, SimpleNamespace(msg_id=msg_id, chat_id=chat_id))


class TestPreMessageInBridgedChat(_Base):
    """The core reports a pre-message to bots only with MsgsChanged; the full message, once its
    post-message is downloaded, as a new message (IncomingMsg) - or MsgsChanged if not fresh."""

    def setUp(self):
        super().setUp()
        dc_events._seen_pre_messages.clear()

    def _pre_message_arrives(self, msg):
        self.rpc.get_message.return_value = msg
        self._changed(msg.id, msg.chat_id)

    def test_downloads_then_relays_the_full_message_once(self):
        with patch.object(dc_events, "relay_dc_message_to_tg") as relay:
            self._pre_message_arrives(_msg(state="Available"))
            self.rpc.download_full_message.assert_called_once_with(1, 7)
            relay.assert_not_called()
            self.assertTrue(database.is_pending_download(7))

            self.rpc.get_message.return_value = _msg(state="InProgress")
            self._changed()  # the core's own InProgress notice
            relay.assert_not_called()

            self._incoming(_msg(state="Done", file="/blobs/photo.jpg"))  # IncomingMsg with the file
            relay.assert_called_once()
            self.assertEqual(relay.call_args.args[2].file, "/blobs/photo.jpg")
            self.assertFalse(database.is_pending_download(7))

            # the timeout and later MsgsChanged (reactions, edits) do not relay it again
            self.assertFalse(dc_events.resolve_pending_download(self.bot, 1, 7, timed_out=True))
            self.rpc.get_message.return_value = _msg(state="Done", file="/blobs/photo.jpg")
            self._changed()
            relay.assert_called_once()

    def test_not_fresh_message_completes_with_msgs_changed(self):
        with patch.object(dc_events, "relay_dc_message_to_tg") as relay:
            self._pre_message_arrives(_msg(state="Available"))
            self.rpc.get_message.return_value = _msg(state="Done", file="/blobs/video.mp4")
            self._changed()
        relay.assert_called_once()
        self.assertIsNone(relay.call_args.kwargs["note"])

    def test_repeated_msgs_changed_requests_once(self):
        with patch.object(dc_events, "relay_dc_message_to_tg"):
            self._pre_message_arrives(_msg(state="Available"))
            database.pop_pending_download(7)  # e.g. resolved meanwhile
            self._pre_message_arrives(_msg(state="Available"))  # a reaction on the pre-message
        self.rpc.download_full_message.assert_called_once()

    def test_failure_relays_text_with_a_note(self):
        with patch.object(dc_events, "relay_dc_message_to_tg") as relay:
            self._pre_message_arrives(_msg(state="Available"))
            self.rpc.get_message.return_value = _msg(state="Failure")
            self._changed()
        self.assertEqual(relay.call_args.kwargs["note"], dc_events.ATTACHMENT_FAILED_NOTE)

    def test_timeout_relays_text_once(self):
        with patch.object(dc_events, "relay_dc_message_to_tg") as relay:
            self._pre_message_arrives(_msg(state="Available"))
            self.rpc.get_message.return_value = _msg(state="InProgress")
            self.assertTrue(dc_events.resolve_pending_download(self.bot, 1, 7, timed_out=True))
            self.assertFalse(dc_events.resolve_pending_download(self.bot, 1, 7, timed_out=True))
        relay.assert_called_once()
        self.assertEqual(relay.call_args.kwargs["note"], dc_events.ATTACHMENT_FAILED_NOTE)

    def test_request_error_relays_text_right_away(self):
        self.rpc.download_full_message.side_effect = RuntimeError("Download already in progress.")
        with patch.object(dc_events, "relay_dc_message_to_tg") as relay:
            self._pre_message_arrives(_msg(state="Available"))
        relay.assert_called_once()
        self.assertFalse(database.is_pending_download(7))

    def test_command_with_attachment_is_not_downloaded(self):
        with patch.object(dc_events, "relay_dc_message_to_tg"):
            self._pre_message_arrives(_msg(state="Available", text="/help"))
        self.rpc.download_full_message.assert_not_called()


class TestOtherMessages(_Base):
    def setUp(self):
        super().setUp()
        dc_events._seen_pre_messages.clear()

    def test_unbridged_pre_message_is_handled_without_download(self):
        self.rpc.get_message.return_value = _msg(chat_id=OTHER, state="Available", text="see t.me/chan/5")
        with patch.object(dc_events, "handle_dc_message") as handle:
            self._changed(7, OTHER)
            self._changed(7, OTHER)  # repeated MsgsChanged: handled once
        self.rpc.download_full_message.assert_not_called()
        handle.assert_called_once()
        self.assertEqual(handle.call_args.args[2].msg.text, "see t.me/chan/5")

    def test_complete_message_relays_at_once(self):
        with patch.object(dc_events, "relay_dc_message_to_tg") as relay:
            self._incoming(_msg(state="Done", file="/blobs/small.jpg"))
        relay.assert_called_once()
        self.rpc.download_full_message.assert_not_called()

    def test_ordinary_msgs_changed_is_not_a_pre_message(self):
        self.rpc.get_message.return_value = _msg(state="Done")
        self.assertFalse(dc_events.handle_pre_message(self.bot, 1, 7))
        self.rpc.download_full_message.assert_not_called()

    def test_note_is_added_to_the_relayed_text(self):
        with patch.object(dc_events, "async_relay_to_tg", MagicMock(return_value="coro")) as send, \
             patch.object(dc_events.asyncio, "run_coroutine_threadsafe"):
            dc_events.relay_dc_message_to_tg(self.bot, 1, _msg(text="hi"), [TG], note=dc_events.ATTACHMENT_FAILED_NOTE)
        self.assertIn("hi\n" + dc_events.ATTACHMENT_FAILED_NOTE, send.call_args.args[4])


class TestRestart(_Base):
    def test_waiting_message_survives_a_restart(self):
        database.add_pending_download(7, BRIDGED)
        self.rpc.get_message.return_value = _msg(state="Done", file="/blobs/video.mp4")
        with patch.object(dc_events, "relay_dc_message_to_tg") as relay:
            dc_events.resume_pending_downloads(self.bot, 1)
        relay.assert_called_once()

    def test_overdue_message_is_relayed_without_file(self):
        database.add_pending_download(8, BRIDGED)
        conn = database._connect()
        conn.cursor().execute("UPDATE pending_downloads SET created_at = ?", (int(time.time()) - 600,))
        conn.commit()
        conn.close()
        self.rpc.get_message.return_value = _msg(msg_id=8, state="InProgress")
        with patch.object(dc_events, "relay_dc_message_to_tg") as relay:
            dc_events.resume_pending_downloads(self.bot, 1)
        self.assertEqual(relay.call_args.kwargs["note"], dc_events.ATTACHMENT_FAILED_NOTE)

    def test_waits_while_telegram_is_not_up(self):
        database.add_pending_download(9, BRIDGED)
        with patch.object(bot, "main_loop", None), patch.object(dc_events, "relay_dc_message_to_tg") as relay:
            self.assertFalse(dc_events.resolve_pending_download(self.bot, 1, 9, timed_out=True))
        relay.assert_not_called()
        self.assertTrue(database.is_pending_download(9))


if __name__ == "__main__":
    unittest.main()
