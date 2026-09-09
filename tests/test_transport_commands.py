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


if __name__ == "__main__":
    unittest.main()
