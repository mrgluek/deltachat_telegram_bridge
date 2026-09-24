import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import deltachat2
except ImportError:
    mock_deltachat2 = MagicMock()
    class MsgData:
        def __init__(self, text="", file="", override_sender_name=None):
            self.text = text
            self.file = file
            self.override_sender_name = override_sender_name
    mock_deltachat2.MsgData = MsgData
    sys.modules['deltachat2'] = mock_deltachat2

try:
    import deltabot_cli
except ImportError:
    class MockBotCli:
        def __init__(self, *args, **kwargs):
            pass
        def on(self, *args, **kwargs):
            return lambda func: func
        def on_init(self, func):
            return func
        def on_start(self, func):
            return func
        def start(self):
            pass
    mock_deltabot_cli = MagicMock()
    mock_deltabot_cli.BotCli = MockBotCli
    sys.modules['deltabot_cli'] = mock_deltabot_cli

import database
import bot
import dc_commands

TEST_DB_PATH = "test_bridge_transport_cmds.db"


class TestTransportCommands(unittest.TestCase):
    def setUp(self):
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = TEST_DB_PATH
        with database._transport_stats_lock:
            database._transport_stats_buffer.clear()
        database.init_db()

        self.mock_bot = MagicMock()
        self.mock_event = MagicMock()
        self.mock_msg = MagicMock()
        self.mock_msg.from_id = 100
        self.mock_msg.chat_id = 10
        self.mock_msg.file = None
        self.mock_event.msg = self.mock_msg
        self.mock_event.payload = ""
        self.accid = 1

        mock_contact = MagicMock()
        mock_contact.address = "admin@example.com"
        self.mock_bot.rpc.get_contact.return_value = mock_contact

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        with database._transport_stats_lock:
            database._transport_stats_buffer.clear()
        for f in (TEST_DB_PATH, f"{TEST_DB_PATH}-wal", f"{TEST_DB_PATH}-shm"):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

    # ── /initadmin ─────────────────────────────────────────────────────────

    @patch("bot._is_private_chat")
    def test_initadmin_rejected_in_group_chat(self, mock_is_private):
        mock_is_private.return_value = False
        bot.initadmin_command(self.mock_bot, self.accid, self.mock_event)
        self.mock_bot.rpc.send_msg.assert_called_once()
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("only be used in a private 1:1 chat", msg_text)

    @patch("bot._get_contact_fingerprint")
    @patch("bot._is_private_chat")
    def test_initadmin_success(self, mock_is_private, mock_get_fp):
        mock_is_private.return_value = True
        mock_get_fp.return_value = "AABBCCDDAABBCCDDAABBCCDDAABBCCDD"
        bot.initadmin_command(self.mock_bot, self.accid, self.mock_event)
        self.assertEqual(database.get_admin_email(), "admin@example.com")
        self.assertEqual(database.get_admin_fingerprint(), "AABBCCDDAABBCCDDAABBCCDDAABBCCDD")
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("You are now the admin", msg_text)

    @patch("bot._is_private_chat")
    def test_initadmin_already_set(self, mock_is_private):
        mock_is_private.return_value = True
        database.set_admin_email("other@example.com")
        bot.initadmin_command(self.mock_bot, self.accid, self.mock_event)
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Admin is already set", msg_text)

    # ── /addtransport ───────────────────────────────────────────────────────

    @patch("bot._is_dc_admin")
    def test_addtransport_non_admin_rejected(self, mock_admin):
        mock_admin.return_value = False
        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Only the bot administrator", msg_text)

    @patch("bot._is_dc_admin")
    @patch("bot._is_private_chat")
    def test_addtransport_rejected_in_group(self, mock_is_private, mock_admin):
        mock_admin.return_value = True
        mock_is_private.return_value = False
        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("only be used in a private 1:1 chat", msg_text)

    @patch("bot._is_dc_admin")
    @patch("bot._is_private_chat")
    def test_addtransport_empty_payload(self, mock_is_private, mock_admin):
        mock_admin.return_value = True
        mock_is_private.return_value = True
        self.mock_event.payload = ""
        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Usage:", msg_text)

    @patch("bot._is_dc_admin")
    @patch("bot._is_private_chat")
    def test_addtransport_success_qr(self, mock_is_private, mock_admin):
        mock_admin.return_value = True
        mock_is_private.return_value = True
        self.mock_event.payload = "DCACCOUNT:chatmail.example.org"
        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)
        self.mock_bot.rpc.add_transport_from_qr.assert_called_once_with(self.accid, "DCACCOUNT:chatmail.example.org")
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Backup transport added via chatmail URI", msg_text)

    @patch("bot._is_dc_admin")
    @patch("bot._is_private_chat")
    def test_addtransport_sanitized_error(self, mock_is_private, mock_admin):
        mock_admin.return_value = True
        mock_is_private.return_value = True
        self.mock_event.payload = "DCACCOUNT:bad"
        self.mock_bot.rpc.add_transport_from_qr.side_effect = RuntimeError("Internal secret connection failure")
        bot.addtransport_command(self.mock_bot, self.accid, self.mock_event)
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertEqual(msg_text, "❌ Failed to add transport.")

    # ── /rmtransport ───────────────────────────────────────────────────────

    @patch("bot._is_dc_admin")
    def test_rmtransport_non_admin_rejected(self, mock_admin):
        mock_admin.return_value = False
        bot.rmtransport_command(self.mock_bot, self.accid, self.mock_event)
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Only the bot administrator", msg_text)

    @patch("bot._is_dc_admin")
    def test_rmtransport_cannot_remove_last(self, mock_admin):
        mock_admin.return_value = True
        self.mock_event.payload = "relay@example.com"
        self.mock_bot.rpc.list_transports.return_value = [{"addr": "relay@example.com"}]
        bot.rmtransport_command(self.mock_bot, self.accid, self.mock_event)
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Cannot remove the last transport", msg_text)

    @patch("bot._is_dc_admin")
    def test_rmtransport_success(self, mock_admin):
        mock_admin.return_value = True
        self.mock_event.payload = "relay2@example.com"
        self.mock_bot.rpc.list_transports.return_value = [
            {"addr": "relay1@example.com"},
            {"addr": "relay2@example.com"},
        ]
        bot.rmtransport_command(self.mock_bot, self.accid, self.mock_event)
        self.mock_bot.rpc.delete_transport.assert_called_once_with(self.accid, "relay2@example.com")
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("removed", msg_text)

    @patch("bot._is_dc_admin")
    def test_rmtransport_sanitized_error(self, mock_admin):
        mock_admin.return_value = True
        self.mock_event.payload = "relay2@example.com"
        self.mock_bot.rpc.list_transports.side_effect = RuntimeError("DB locked")
        bot.rmtransport_command(self.mock_bot, self.accid, self.mock_event)
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertEqual(msg_text, "❌ Failed to check transports.")

    # ── /setprimary ────────────────────────────────────────────────────────

    @patch("bot._is_dc_admin")
    def test_setprimary_success(self, mock_admin):
        mock_admin.return_value = True
        self.mock_event.payload = "primary@example.com"
        bot.setprimary_command(self.mock_bot, self.accid, self.mock_event)
        self.mock_bot.rpc.set_config.assert_called_once_with(self.accid, "configured_addr", "primary@example.com")
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Primary address", msg_text)

    @patch("bot._is_dc_admin")
    def test_setprimary_sanitized_error(self, mock_admin):
        mock_admin.return_value = True
        self.mock_event.payload = "primary@example.com"
        self.mock_bot.rpc.set_config.side_effect = RuntimeError("Config write failed")
        bot.setprimary_command(self.mock_bot, self.accid, self.mock_event)
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertEqual(msg_text, "❌ Failed to set primary address.")

    # ── /resilient ─────────────────────────────────────────────────────────

    @patch("bot._is_dc_admin")
    def test_resilient_toggle(self, mock_admin):
        mock_admin.return_value = True

        # Query status when off
        self.mock_event.payload = ""
        bot.resilient_command(self.mock_bot, self.accid, self.mock_event)
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("disabled", msg_text)

        # Enable
        self.mock_event.payload = "on"
        bot.resilient_command(self.mock_bot, self.accid, self.mock_event)
        self.assertEqual(database.get_config("resilient"), "1")
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("enabled", msg_text)

        # Disable
        self.mock_event.payload = "off"
        bot.resilient_command(self.mock_bot, self.accid, self.mock_event)
        self.assertEqual(database.get_config("resilient"), "0")
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("disabled", msg_text)

    # ── /transports ────────────────────────────────────────────────────────

    @patch("bot._is_dc_admin")
    def test_transports_list_success(self, mock_admin):
        mock_admin.return_value = True
        self.mock_bot.rpc.list_transports.return_value = [{"addr": "relay1@example.com"}]
        self.mock_bot.rpc.get_connectivity.return_value = 4000
        self.mock_bot.rpc.get_connectivity_html.return_value = ""
        self.mock_bot.rpc.get_config.return_value = "relay1@example.com"

        bot.transports_command(self.mock_bot, self.accid, self.mock_event)
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertIn("Mail Relays", msg_text)
        self.assertIn("relay1@example.com", msg_text)

    @patch("bot._is_dc_admin")
    def test_transports_sanitized_error(self, mock_admin):
        mock_admin.return_value = True
        self.mock_bot.rpc.list_transports.side_effect = RuntimeError("Internal RPC error")
        bot.transports_command(self.mock_bot, self.accid, self.mock_event)
        msg_text = self.mock_bot.rpc.send_msg.call_args[0][2].text
        self.assertEqual(msg_text, "❌ Failed to list transports.")


class TestHelpPrivateReply(unittest.TestCase):
    """Plain /help in a group goes to the sender privately; /help@<bot> stays in the group."""

    def _msg(self, text):
        msg = MagicMock()
        msg.text = text
        msg.chat_id = 42
        msg.from_id = 7
        return msg

    def _bot(self):
        mock_bot = MagicMock()
        mock_bot.rpc.create_chat_by_contact_id.return_value = 555
        return mock_bot

    @patch("bot._is_private_chat", return_value=False)
    def test_plain_help_in_group_goes_private(self, _mock_chat):
        mock_bot = self._bot()
        self.assertEqual(dc_commands._get_help_chat_id(mock_bot, 1, self._msg("/help")), 555)
        mock_bot.rpc.create_chat_by_contact_id.assert_called_once_with(1, 7)

    @patch("bot._is_private_chat", return_value=False)
    def test_addressed_help_in_group_stays_in_group(self, _mock_chat):
        mock_bot = self._bot()
        self.assertEqual(dc_commands._get_help_chat_id(mock_bot, 1, self._msg("/help@tg extra")), 42)
        mock_bot.rpc.create_chat_by_contact_id.assert_not_called()

    @patch("bot._is_private_chat", return_value=True)
    def test_plain_help_in_private_chat_stays(self, _mock_chat):
        mock_bot = self._bot()
        self.assertEqual(dc_commands._get_help_chat_id(mock_bot, 1, self._msg("/help")), 42)
        mock_bot.rpc.create_chat_by_contact_id.assert_not_called()

    @patch("bot._is_private_chat", return_value=False)
    @patch("bot._dc_send_msg_with_stats")
    @patch("dc_commands.MsgData", side_effect=lambda text: text)
    @patch("dc_commands.get_dc_help_text", return_value="HELP")
    def test_help_command_in_group_sends_private_with_note(self, _mock_text, _mock_msgdata, mock_send, _mock_chat):
        mock_bot = self._bot()
        event = MagicMock()
        event.msg = self._msg("/help")
        dc_commands.help_command(mock_bot, 1, event)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][2], 555)
        self.assertIn("/help@tg", mock_send.call_args[0][3])


if __name__ == "__main__":
    unittest.main()
