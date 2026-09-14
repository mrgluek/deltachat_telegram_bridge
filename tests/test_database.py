import os
import sys
import unittest
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database

TEST_DB = "test_telegram_bridge.db"

class TestDatabase(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = TEST_DB
        database.init_db()
        with database._transport_stats_lock:
            database._transport_stats_buffer.clear()

    def tearDown(self):
        with database._transport_stats_lock:
            database._transport_stats_buffer.clear()
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except OSError:
                pass
        for suffix in ["-wal", "-shm"]:
            fpath = TEST_DB + suffix
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

    def test_config_roundtrip(self):
        self.assertIsNone(database.get_config("nonexistent"))
        database.set_config("key1", "val1")
        self.assertEqual(database.get_config("key1"), "val1")
        database.set_config("key1", "val2")
        self.assertEqual(database.get_config("key1"), "val2")

    def test_admin_email_normalization(self):
        self.assertIsNone(database.get_admin_email())
        database.set_admin_email("  ADMIN@Example.COM  ")
        self.assertEqual(database.get_admin_email(), "admin@example.com")

    def test_admin_fingerprint_handling(self):
        self.assertIsNone(database.get_admin_fingerprint())
        database.set_admin_fingerprint("aa:bb:cc:dd:11:22:33:44:55:66:77:88:99:00:11:22")
        self.assertEqual(database.get_admin_fingerprint(), "AABBCCDD112233445566778899001122")
        
        # Invalid fingerprint format rejected
        database.set_admin_fingerprint("invalid_fp")
        self.assertIsNone(database.get_admin_fingerprint())
        
        # Reset fingerprint
        database.set_admin_fingerprint("")
        self.assertIsNone(database.get_admin_fingerprint())

    def test_is_authorized_sender(self):
        # No admin configured
        self.assertFalse(database.is_authorized_sender("user@example.com"))

        # Email only configured
        database.set_admin_email("admin@example.com")
        self.assertTrue(database.is_authorized_sender("admin@example.com"))
        self.assertTrue(database.is_authorized_sender(" ADMIN@example.COM "))
        self.assertFalse(database.is_authorized_sender("intruder@example.com"))

        # Fingerprint configured as well
        fp = "AABBCCDD112233445566778899001122"
        database.set_admin_fingerprint(fp)
        self.assertTrue(database.is_authorized_sender("admin@example.com", fp))
        self.assertTrue(database.is_authorized_sender("admin@example.com", "aa:bb:cc:dd:11:22:33:44:55:66:77:88:99:00:11:22"))
        self.assertFalse(database.is_authorized_sender("admin@example.com", "WRONGFP112233445566778899001122"))
        # Email alone fails when fingerprint is required
        self.assertFalse(database.is_authorized_sender("admin@example.com"))

    def test_transport_stats_buffering_and_flush(self):
        addr1 = "relay1@example.com"
        addr2 = "relay2@example.com"

        database.increment_transport_sent(addr1)
        database.increment_transport_sent(addr1)
        database.increment_transport_received(addr1)
        database.increment_transport_received(addr2)

        # Before explicit flush, get_all_transport_stats automatically flushes
        stats = database.get_all_transport_stats()
        stats_map = {s["addr"]: s for s in stats}

        self.assertIn(addr1, stats_map)
        self.assertIn(addr2, stats_map)
        self.assertEqual(stats_map[addr1]["msgs_sent"], 2)
        self.assertEqual(stats_map[addr1]["msgs_received"], 1)
        self.assertIsNotNone(stats_map[addr1]["last_sent_at"])
        self.assertIsNotNone(stats_map[addr1]["last_received_at"])

        self.assertEqual(stats_map[addr2]["msgs_sent"], 0)
        self.assertEqual(stats_map[addr2]["msgs_received"], 1)

    def test_resilient_flag(self):
        self.assertFalse(database.get_config("resilient") == "1")
        database.set_config("resilient", "1")
        self.assertEqual(database.get_config("resilient"), "1")
        database.set_config("resilient", "0")
        self.assertEqual(database.get_config("resilient"), "0")

    def test_cleanup_old_records(self):
        # Insert test message mappings
        for i in range(15):
            database.save_message_map(i, 100, i + 1000, 200)

        # Cleanup keeping last 5
        res = database.cleanup_old_records(limit=5)
        self.assertEqual(res["message_map"], 10)

    def test_processed_media_groups(self):
        self.assertFalse(database.is_media_group_processed("group_123"))
        database.mark_media_group_processed("group_123", -10012345, 999)
        self.assertTrue(database.is_media_group_processed("group_123"))

        # Test integer group_id conversion
        database.mark_media_group_processed(456789, -10012345, 1000)
        self.assertTrue(database.is_media_group_processed(456789))
        self.assertTrue(database.is_media_group_processed("456789"))

        # Test cleanup with limit
        for i in range(10):
            database.mark_media_group_processed(f"bulk_grp_{i}", -10012345, i)
        res = database.cleanup_old_records(limit=5)
        self.assertGreaterEqual(res["processed_media_groups"], 5)

    def test_monotonic_update_channel_last_msg_id(self):
        ch_id = -100999888
        database.add_channel_by_id(ch_id, 50, username="testchan")
        self.assertEqual(database.get_channel_last_msg_id(ch_id), 0)

        # Update to 100
        database.update_channel_last_msg_id(ch_id, 100)
        self.assertEqual(database.get_channel_last_msg_id(ch_id), 100)

        # Attempt to downgrade to 80 (e.g. out-of-order album part)
        database.update_channel_last_msg_id(ch_id, 80)
        self.assertEqual(database.get_channel_last_msg_id(ch_id), 100)

        # Update to 150
        database.update_channel_last_msg_id(ch_id, 150)
        self.assertEqual(database.get_channel_last_msg_id(ch_id), 150)

    def test_normalize_tg_id_variants(self):
        # -100 prefix int
        self.assertEqual(database._normalize_tg_id_variants(-1001234567890), (-1001234567890, 1234567890))
        # Positive int
        self.assertEqual(database._normalize_tg_id_variants(1234567890), (-1001234567890, 1234567890))
        # String with -100
        self.assertEqual(database._normalize_tg_id_variants("-1001234567890"), (-1001234567890, 1234567890))
        # String without -100
        self.assertEqual(database._normalize_tg_id_variants("1234567890"), (-1001234567890, 1234567890))
        # Legacy group (negative without 100)
        self.assertEqual(database._normalize_tg_id_variants(-123456), (-123456, -123456))
        # Invalid
        self.assertEqual(database._normalize_tg_id_variants("invalid"), (0, 0))

    def test_cross_format_id_lookups(self):
        # Add channel with -100 prefix
        database.add_channel_by_id(-100999888, 50, username="testchan")
        
        # Look up channel by positive ID
        chan = database.get_channel_by_tg_id(999888)
        self.assertIsNotNone(chan)
        self.assertEqual(chan["dc_chat_id"], 50)

        # get_dc_channel_chat_id cross-lookup
        self.assertEqual(database.get_dc_channel_chat_id(999888), 50)
        self.assertEqual(database.get_dc_channel_chat_id(-100999888), 50)

        # Update last_msg_id using positive ID
        database.update_channel_last_msg_id(999888, 42)
        self.assertEqual(database.get_channel_last_msg_id(-100999888), 42)
        self.assertEqual(database.get_channel_last_msg_id(999888), 42)

        # Save message_map with positive ID, lookup with -100 prefix
        database.save_message_map(dc_msg_id=101, dc_chat_id=50, tg_msg_id=77, tg_chat_id=999888, content_hash="abc")
        self.assertEqual(database.get_dc_msg_id(77, -100999888, 50), 101)
        self.assertEqual(database.get_dc_msg_id(77, 999888, 50), 101)
        self.assertEqual(database.get_message_content_hash(77, -100999888, 50), "abc")
        self.assertEqual(database.get_message_content_hash(77, 999888, 50), "abc")

        # Save message_map with -100 prefix, lookup with positive ID
        database.save_message_map(dc_msg_id=102, dc_chat_id=50, tg_msg_id=78, tg_chat_id=-100999888, content_hash="def")
        self.assertEqual(database.get_dc_msg_id(78, 999888, 50), 102)
        self.assertEqual(database.get_dc_msg_id(78, -100999888, 50), 102)


if __name__ == "__main__":
    unittest.main()
