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
        self.assertEqual(database.get_dc_subadmins(), [("bob@example.org", FP1)])
        self.assertEqual(database.remove_dc_subadmin("BOB@example.org"), 1)
        self.assertEqual(database.remove_dc_subadmin("bob@example.org"), 0)

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


if __name__ == "__main__":
    unittest.main()
