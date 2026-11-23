"""2.38.1: the own fingerprint is read once per account, not on every message or contact."""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import deltachat2
    import deltabot_cli
except ImportError:
    sys.modules.setdefault('deltachat2', MagicMock())
    sys.modules.setdefault('deltabot_cli', MagicMock())

import bot
import database
import dc_commands
import dc_events
import dc_fingerprint

SELF_FP = "D58B5E6C55482F94F9F5B84B6FB9FE3AD2658082"
PEER_FP = "DEC0E8D11FC4686F2C715C56F8C9AD699BC19AD8"
ADMIN_FP = "9E1B0E5B8CB9CAE6A9D4F0D558C6D85CE9BD3041"
INVITE = f"https://i.delta.chat/#{SELF_FP}&v=3&i=abc&s=def&a=bot%40example.org&n=Bot"
INFO = (f"Messages are end-to-end encrypted.\nFingerprints:\n\nMe:\n{SELF_FP}\n\n"
        f"Peer:\n{PEER_FP}\n\nRelays:\npeer@example.org")


def make_bot():
    b = MagicMock()
    b.has_command.return_value = False
    b.rpc.get_chat_securejoin_qr_code.return_value = INVITE
    b.rpc.get_contact_encryption_info.return_value = INFO
    b.rpc.get_basic_chat_info.return_value = {"type": 2, "name": "Group"}
    b.rpc.get_contact.return_value = {"address": "peer@example.org", "is_key_contact": True,
                                      "display_name": "Peer"}
    return b


class TestOwnFingerprintCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "fp.db")
        database.init_db()
        dc_fingerprint.forget_self_fingerprint()

    def tearDown(self):
        database.DB_PATH = self.old_db
        self.tmp.cleanup()
        dc_fingerprint.forget_self_fingerprint()

    def test_helper_asks_the_core_once(self):
        rpc = make_bot().rpc
        for _ in range(1000):
            self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)
        self.assertEqual(rpc.get_chat_securejoin_qr_code.call_count, 1)

    def test_failures_are_not_cached_and_accounts_are_separate(self):
        rpc = MagicMock()
        rpc.get_chat_securejoin_qr_code.side_effect = [Exception("down"), INVITE]
        self.assertIsNone(dc_fingerprint.get_self_fingerprint(rpc, 1))
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)
        rpc.get_chat_securejoin_qr_code.side_effect = None
        rpc.get_chat_securejoin_qr_code.return_value = f"https://i.delta.chat/#{PEER_FP}&v=3"
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 2), PEER_FP)
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), SELF_FP)
        dc_fingerprint.forget_self_fingerprint(rpc, 1)
        self.assertEqual(dc_fingerprint.get_self_fingerprint(rpc, 1), PEER_FP)

    def test_1000_messages_through_the_handler_cost_at_most_one_qr_call(self):
        database.set_config("admin_dc_email", "owner@example.org")
        database.set_admin_fingerprint(ADMIN_FP)
        cid = database.add_channel_by_id(-100, 20, "https://i.delta.chat/#secret")
        b = make_bot()
        event = MagicMock()
        event.msg.chat_id = 10
        event.msg.from_id = 5
        event.msg.text = f"/channel{cid}"
        with patch.object(bot, "_dc_send_msg_with_stats") as send:
            for _ in range(1000):
                dc_events.handle_dc_message(b, 1, event)
        self.assertEqual(send.call_count, 1000)
        self.assertIn("not found", send.call_args.args[3].text)
        self.assertLessEqual(b.rpc.get_chat_securejoin_qr_code.call_count, 1)

    def test_admin_listing_over_200_contacts_costs_at_most_one_qr_call(self):
        b = make_bot()
        ids = list(range(100, 300))
        b.rpc.get_contact_ids.return_value = ids
        fps, name = dc_commands._dc_contact_key_fingerprints(b, 1, "peer@example.org")
        self.assertEqual(fps, [PEER_FP])
        self.assertEqual(b.rpc.get_contact_encryption_info.call_count, 200)
        self.assertLessEqual(b.rpc.get_chat_securejoin_qr_code.call_count, 1)

    def test_admin_contact_lookup_over_200_contacts_costs_at_most_one_qr_call(self):
        b = make_bot()
        b.rpc.get_contact_ids.return_value = list(range(100, 300))
        for _ in range(5):
            self.assertEqual(bot._get_contact_fingerprint(b, 1, 100), PEER_FP)
        self.assertLessEqual(b.rpc.get_chat_securejoin_qr_code.call_count, 1)


if __name__ == "__main__":
    unittest.main()
