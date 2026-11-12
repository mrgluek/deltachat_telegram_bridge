import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database

TEST_DB = "test_dc_subadmins.db"
FP1 = "A" * 40
FP2 = "B" * 40


class TestDcSubadmins(unittest.TestCase):
    def setUp(self):
        self.orig = database.DB_PATH
        database.DB_PATH = TEST_DB
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.orig
        for f in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
            if os.path.exists(f):
                os.remove(f)

    def test_add_remove_list(self):
        self.assertTrue(database.add_dc_subadmin(FP1, "Bob@Example.org"))
        self.assertFalse(database.add_dc_subadmin(FP1, "bob@example.org"))
        self.assertFalse(database.add_dc_subadmin("short", "x@example.org"))
        self.assertEqual(database.get_dc_subadmins(), [("bob@example.org", FP1, "")])
        self.assertEqual(database.remove_dc_subadmin("BOB@example.org"), 1)
        self.assertEqual(database.remove_dc_subadmin("bob@example.org"), 0)

    def test_update_info_and_remove_by_key_suffix(self):
        database.add_dc_subadmin(FP1, "old@example.org", "Bob")
        database.update_dc_subadmin_info(FP1, "New@Example.org", "")
        self.assertEqual(database.get_dc_subadmins(), [("new@example.org", FP1, "Bob")])
        database.update_dc_subadmin_info(FP1, "", "Robert")
        self.assertEqual(database.get_dc_subadmins(), [("new@example.org", FP1, "Robert")])
        self.assertEqual(database.remove_dc_subadmin(FP1[-8:]), 1)
        self.assertEqual(database.get_dc_subadmins(), [])

    def test_channel_creator(self):
        rid = database.add_channel_by_id(-1001, 20, "link", username="chan", created_by_dc_fp=FP1)
        rid2 = database.add_channel_by_id(-1002, 21, "link")
        self.assertEqual(database.get_channel_dc_creator(rid), FP1)
        self.assertIsNone(database.get_channel_dc_creator(rid2))

    def test_match_by_fingerprint(self):
        database.add_dc_subadmin(FP1, "bob@example.org")
        self.assertEqual(database.is_dc_subadmin_fp(f"{FP2},{FP1}"), FP1)
        self.assertIsNone(database.is_dc_subadmin_fp(FP2))
        self.assertIsNone(database.is_dc_subadmin_fp(""))
        self.assertIsNone(database.is_dc_subadmin_fp(None))

    def test_bridge_creator(self):
        database.add_bridge(10, -100, created_by_dc_fp=FP1)
        database.add_bridge(11, -101)
        self.assertEqual(database.get_bridge_dc_creator(10), FP1)
        self.assertIsNone(database.get_bridge_dc_creator(11))
        self.assertEqual(database.get_bridges_by_dc_creator(FP1), [(10, -100)])


class TestDcSubadminCommands(unittest.TestCase):
    def setUp(self):
        self.orig = database.DB_PATH
        database.DB_PATH = TEST_DB
        database.init_db()
        database.set_config("admin_dc_email", "owner@example.org")
        import dc_commands, bot
        self.dc, self.bot = dc_commands, bot
        self.mock_bot = MagicMock()
        self.mock_bot.rpc.get_basic_chat_info.return_value = {"type": 2}
        self.event = MagicMock()
        self.event.msg.from_id = 5
        self.event.msg.chat_id = 10
        self.event.payload = "-100"

    def tearDown(self):
        database.DB_PATH = self.orig
        for f in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
            if os.path.exists(f):
                os.remove(f)

    def _sent(self, m):
        return m.call_args[0][3].text

    def test_stranger_cannot_bridge(self):
        from unittest.mock import patch
        with patch.object(self.bot, "_is_dc_admin", return_value=False), \
             patch.object(self.bot, "_dc_subadmin_fp", return_value=None), \
             patch.object(self.bot, "_dc_send_msg_with_stats") as send:
            self.dc.bridge_command(self.mock_bot, 1, self.event)
        self.assertIn("Only", self._sent(send))
        self.assertEqual(database.get_tg_chats(10), [])

    def test_subadmin_bridges_and_only_unbridges_own(self):
        from unittest.mock import patch
        with patch.object(self.bot, "_is_dc_admin", return_value=False), \
             patch.object(self.bot, "_dc_subadmin_fp", return_value=FP1), \
             patch.object(self.bot, "_dc_send_msg_with_stats"):
            self.dc.bridge_command(self.mock_bot, 1, self.event)
            self.assertEqual(database.get_bridge_dc_creator(10), FP1)
            database.add_bridge(11, -200, created_by_dc_fp=FP2)
            self.event.msg.chat_id = 11
            self.dc.unbridge_command(self.mock_bot, 1, self.event)
            self.assertEqual(database.get_tg_chats(11), [-200])
            self.event.msg.chat_id = 10
            self.dc.unbridge_command(self.mock_bot, 1, self.event)
            self.assertEqual(database.get_tg_chats(10), [])


    def _vcard_setup(self, cards, ids, contacts, fp):
        from unittest.mock import patch
        self.mock_bot.rpc.parse_vcard.return_value = cards
        self.mock_bot.rpc.import_vcard.return_value = ids
        self.mock_bot.rpc.get_contact.side_effect = lambda a, c: contacts[c]
        self.event.msg.file = "/tmp/card.vcf"
        self.event.payload = ""
        return [patch.object(self.bot, "_is_dc_admin", return_value=True),
                patch.object(self.bot, "_is_private_chat", return_value=True),
                patch.object(self.bot, "_get_contact_fingerprint", return_value=fp),
                patch.object(self.bot, "_dc_send_msg_with_stats")]

    def test_subadminadd_from_vcard(self):
        card = {"addr": "Bob@Example.org", "key": "AAAA", "display_name": "Bobby"}
        contact = {"address": "bob@example.org", "is_key_contact": True}
        patches = self._vcard_setup([card], [7], {7: contact}, FP1)
        with patches[0], patches[1], patches[2], patches[3] as send:
            self.dc.dc_subadminadd_command(self.mock_bot, 1, self.event)
        self.assertEqual(database.get_dc_subadmins(), [("bob@example.org", FP1, "Bobby")])
        self.assertIn("is now a sub-admin", self._sent(send))

    def test_subadmins_list_shows_name_key_and_address(self):
        from unittest.mock import patch
        database.add_dc_subadmin(FP1, "bob@example.org", "Bobby")
        database.add_dc_subadmin(FP2, "eve@example.org")
        with patch.object(self.bot, "_is_dc_admin", return_value=True), \
             patch.object(self.bot, "_is_private_chat", return_value=True), \
             patch.object(self.bot, "_dc_send_msg_with_stats") as send:
            self.dc.dc_subadmins_command(self.mock_bot, 1, self.event)
        text = self._sent(send)
        self.assertIn(f"Bobby …{FP1[-8:]} (bob@example.org)", text)
        self.assertIn(f"eve@example.org …{FP2[-8:]}", text)

    def test_subadmin_channels_add_and_remove_only_own(self):
        from unittest.mock import patch
        rid_own = database.add_channel_by_id(-1001, 30, "l", username="own", created_by_dc_fp=FP1)
        rid_other = database.add_channel_by_id(-1002, 31, "l", username="other", created_by_dc_fp=FP2)
        self.event.payload = str(rid_other)
        with patch.object(self.bot, "_is_dc_admin", return_value=False), \
             patch.object(self.bot, "_dc_subadmin_fp", return_value=FP1), \
             patch.object(self.bot, "_dc_send_msg_with_stats") as send:
            self.dc.dc_channelremove_command(self.mock_bot, 1, self.event)
            self.assertIn("only remove channels they added", self._sent(send))
            self.assertIsNotNone(database.get_channel_by_id(rid_other))
            self.event.payload = str(rid_own)
            with patch.object(self.dc, "_notify_and_remove_channel_bridge", return_value=-1001):
                self.dc.dc_channelremove_command(self.mock_bot, 1, self.event)
            self.assertIn("removed", self._sent(send))

    def test_stranger_cannot_add_channel(self):
        from unittest.mock import patch
        self.event.payload = "@chan"
        with patch.object(self.bot, "_is_dc_admin", return_value=False), \
             patch.object(self.bot, "_dc_subadmin_fp", return_value=None), \
             patch.object(self.bot, "_dc_send_msg_with_stats") as send:
            self.dc.dc_channeladd_command(self.mock_bot, 1, self.event)
        self.assertIn("Only the bot administrator", self._sent(send))

    def test_subadminadd_by_email_uses_real_contact_lookup(self):
        from unittest.mock import patch
        self.event.payload = "bob@example.org"
        self.event.msg.file = None
        contact = {"address": "bob@example.org", "is_key_contact": True, "display_name": "Bobby"}
        self.mock_bot.rpc.get_contact_ids.return_value = [7]
        self.mock_bot.rpc.get_contact.side_effect = lambda a, c: contact
        with patch.object(self.bot, "_is_dc_admin", return_value=True), \
             patch.object(self.bot, "_is_private_chat", return_value=True), \
             patch.object(self.bot, "_get_contact_fingerprint", return_value=FP1), \
             patch.object(self.bot, "_dc_send_msg_with_stats") as send:
            self.dc.dc_subadminadd_command(self.mock_bot, 1, self.event)
        self.assertEqual(database.get_dc_subadmins(), [("bob@example.org", FP1, "Bobby")])
        self.assertIn("is now a sub-admin", self._sent(send))

    def test_vcard_without_key_or_wrong_address_refused(self):
        patches = self._vcard_setup([{"addr": "bob@example.org", "key": None}], [], {}, FP1)
        with patches[0], patches[1], patches[2], patches[3] as send:
            self.dc.dc_subadminadd_command(self.mock_bot, 1, self.event)
        self.assertIn("has no key", self._sent(send))
        other = {"address": "eve@example.org", "is_key_contact": True}
        patches = self._vcard_setup([{"addr": "bob@example.org", "key": "AAAA"}], [8], {8: other}, FP1)
        with patches[0], patches[1], patches[2], patches[3] as send:
            self.dc.dc_subadminadd_command(self.mock_bot, 1, self.event)
        self.assertEqual(database.get_dc_subadmins(), [])


if __name__ == "__main__":
    unittest.main()
