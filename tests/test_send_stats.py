import asyncio
import os
import time
import unittest
from unittest.mock import MagicMock, patch

import bot  # noqa: F401
import database
import dc_commands
import security

TEST_DB = "test_send_stats.db"


class TestSendStats(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = TEST_DB
        database.init_db()
        for k in security._send_stats:
            security._send_stats[k] = 0 if isinstance(security._send_stats[k], int) else 0.0

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)

    def test_record_accumulates_per_hour(self):
        t = 1_800_000_000
        database.record_send_stats(3, 10, 5.0, 1.0, 40, now=t)
        database.record_send_stats(2, 5, 2.0, 0.0, 12, now=t + 60)
        database.record_send_stats(1, 1, 0.0, 0.0, 0, now=t + 3600)
        rows = database.get_send_stats(t - 3600)
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual((first['group_sent'], first['channel_sent'], first['max_backlog']), (5, 15, 40))
        self.assertAlmostEqual(first['rate_wait_sec'], 7.0)

    def test_flush_writes_and_resets_counters(self):
        security._send_stats["group"] = 4
        security._send_stats["channel"] = 6
        security.flush_send_stats()
        rows = database.get_send_stats(time.time() - 3600)
        self.assertEqual(rows[-1]['group_sent'], 4)
        self.assertEqual(rows[-1]['channel_sent'], 6)
        self.assertEqual(security._send_stats["group"], 0)

    def _section(self):
        dc = MagicMock()
        dc.rpc.get_basic_chat_info.return_value = MagicMock(name="x")
        return asyncio.run(dc_commands._build_send_load_section(False, dc, 1))

    def test_section_headroom(self):
        database.record_send_stats(10, 100, 0.0, 0.0, 0, now=time.time() - 7200)
        text = self._section()
        self.assertIn("Send load", text)
        self.assertIn("🟢", text)

    def test_section_at_capacity(self):
        per_hour = security.GLOBAL_DC_RATE_LIMIT * 60
        database.record_send_stats(0, per_hour, 3 * 3600, 0.0, 500, now=time.time() - 7200)
        text = self._section()
        self.assertIn("🔴", text)
        self.assertIn("largest backlog 500", text)


if __name__ == "__main__":
    unittest.main()
