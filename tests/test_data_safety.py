import asyncio
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for module in ('deltachat2', 'deltabot_cli'):
    try:
        __import__(module)
    except ImportError:
        sys.modules[module] = MagicMock()

import bot
import database
import dc_commands
import restore_channels


class DataSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.close_db()
        database.DB_PATH = os.path.join(self.tmp.name, 'bridge.db')
        database.init_db()
        getattr(bot, '_cleanup_absent_chats', {}).clear()
        self.dc = MagicMock()
        self.dc.rpc.get_contact.return_value = {'id': 1}
        self.dc.rpc.get_chat_contacts.return_value = [1, 2]

    def tearDown(self):
        database.close_db()
        database.DB_PATH = self.original_path
        getattr(bot, '_cleanup_absent_chats', {}).clear()
        self.tmp.cleanup()

    def cleanup(self, now=1000, **kwargs):
        with patch.object(bot.time, 'time', return_value=now):
            return asyncio.run(bot.cleanup_stale_bridges(
                dc_bot=self.dc, accid=1, tg_app_instance=False, ub_client=False, **kwargs))

    def add_pair(self, cid=10):
        database.add_bridge(cid, -cid)
        database.save_message_map(1, cid, 2, -cid)

    def assert_pair_preserved(self, cid=10):
        self.assertEqual(database.get_tg_chats(cid), [-cid])
        self.assertEqual(database.get_bridge_message_count(cid, -cid), 1)

    def test_rpc_exceptions_and_invalid_responses_preserve_data(self):
        self.add_pair()
        channel = database.add_channel_by_id(-100, 20, username='channel')
        for error in (RuntimeError('chat not found'), TimeoutError('timeout'),
                      ConnectionError('offline'), Exception('RPC unavailable')):
            self.dc.rpc.get_basic_chat_info.side_effect = error
            with self.assertLogs(bot.logger, level='WARNING'):
                self.cleanup(manual=True)
            self.assert_pair_preserved()
            self.assertEqual(database.get_all_channels()[0]['id'], channel)
        self.dc.rpc.get_basic_chat_info.side_effect = None
        for response in (None, {}, [], {'name': ''}):
            self.dc.rpc.get_basic_chat_info.return_value = response
            with self.assertLogs(bot.logger, level='WARNING'):
                self.cleanup(manual=True)
            self.assert_pair_preserved()

    def test_automatic_requires_two_passes_ten_minutes_apart(self):
        self.add_pair()
        database.add_channel_by_id(-100, 20, username='channel')
        self.dc.rpc.get_basic_chat_info.side_effect = Exception('Chat does not exist')
        self.cleanup()
        self.assert_pair_preserved()
        self.assertEqual(len(database.get_all_channels()), 1)
        self.cleanup(now=1599)
        self.assert_pair_preserved()
        result = self.cleanup(now=1600)
        self.assertEqual(result['orphaned_bridges_removed'], 1)
        self.assertEqual(result['orphaned_channels_removed'], 1)
        self.assertEqual(database.get_all_bridges(), [])
        self.assertEqual(database.get_bridge_message_count(10, -10), 0)

    def test_present_chat_resets_absence(self):
        self.add_pair()
        self.dc.rpc.get_basic_chat_info.side_effect = Exception('No such chat')
        self.cleanup()
        self.dc.rpc.get_basic_chat_info.side_effect = None
        self.dc.rpc.get_basic_chat_info.return_value = {'name': 'Active'}
        self.cleanup(now=1300)
        self.dc.rpc.get_basic_chat_info.side_effect = Exception('No such chat')
        self.cleanup(now=1600)
        self.assert_pair_preserved()

    def test_manual_explicit_absence_removes_immediately(self):
        self.add_pair()
        database.add_channel_by_id(-100, 20, username='channel')
        self.dc.rpc.get_basic_chat_info.side_effect = Exception('Chat not found')
        result = self.cleanup(manual=True)
        self.assertEqual(result['orphaned_bridges_removed'], 1)
        self.assertEqual(result['orphaned_channels_removed'], 1)

    def test_deleted_marker_and_same_chat_in_one_pass(self):
        self.add_pair()
        database.add_bridge(10, -100)
        self.dc.rpc.get_basic_chat_info.return_value = {'deleted': True}
        self.cleanup()
        self.assertEqual(len(database.get_all_bridges()), 2)
        self.dc.rpc.get_basic_chat_info.assert_called_once_with(1, 10)
        self.cleanup(now=1600)
        self.assertEqual(database.get_all_bridges(), [])

    def test_three_unknowns_abort_before_later_items(self):
        for cid in range(10, 15):
            self.add_pair(cid)
        database.add_channel_by_id(-100, 20, username='channel')
        self.dc.rpc.get_basic_chat_info.side_effect = RuntimeError('unavailable')
        with self.assertLogs(bot.logger, level='WARNING') as logs:
            self.cleanup()
        self.assertEqual(self.dc.rpc.get_basic_chat_info.call_count, 3)
        self.assertIn('Aborting', logs.output[-1])
        self.assertEqual(len(database.get_all_bridges()), 5)
        self.assertEqual(len(database.get_all_channels()), 1)

    def test_contact_lookup_failure_skips_duplicate_removal(self):
        database.add_bridge(10, -100)
        database.add_bridge(11, -100)
        self.dc.rpc.get_basic_chat_info.return_value = {'name': 'Active'}
        self.dc.rpc.get_chat_contacts.side_effect = [RuntimeError('offline'), [1]]
        with self.assertLogs(bot.logger, level='WARNING'):
            self.cleanup()
        self.assertEqual(len(database.get_all_bridges()), 2)
        self.dc.rpc.delete_chat.assert_not_called()

    def test_dc_cleanup_command_requests_manual_pass(self):
        event = MagicMock()
        loop = MagicMock()
        coroutines = []
        def run_now(coro, target_loop):
            coroutines.append(coro)
            asyncio.run(coro)
        with patch.object(bot, '_is_dc_admin', return_value=True), \
             patch.object(bot, 'main_loop', loop), \
             patch.object(bot, '_dc_send_msg_with_stats'), \
             patch.object(bot, 'cleanup_stale_bridges', return_value={
                 'orphaned_bridges_removed': 0, 'duplicate_bridges_removed': 0,
                 'dead_bridges_removed': 0, 'orphaned_channels_removed': 0,
                 'dc_chats_deleted': 0}) as cleanup, \
             patch('asyncio.run_coroutine_threadsafe', side_effect=run_now):
            dc_commands.dc_cleanup_command(self.dc, 1, event)
        cleanup.assert_awaited_once_with(dc_bot=self.dc, accid=1, manual=True)

    def test_bridge_removal_returns_bridge_result_without_messages(self):
        database.add_bridge(10, -100)
        self.assertTrue(database.remove_bridge_by_tg(-100))
        self.assertFalse(database.remove_bridge_by_tg(-100))
        database.add_bridge(11, -101)
        self.assertTrue(database.remove_bridge_pair(11, -101))
        self.assertFalse(database.remove_bridge_pair(11, -101))
        database.add_bridge(12, -102)
        self.assertEqual(database.remove_bridge(12), [-102])
        self.assertEqual(database.remove_bridge(12), [])

    def legacy_db(self):
        database.close_db()
        with contextlib.closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute('DROP TABLE message_map')
            conn.execute('CREATE TABLE message_map (dc_msg_id INTEGER, dc_chat_id INTEGER, '
                         'tg_msg_id INTEGER, tg_chat_id INTEGER, PRIMARY KEY (dc_msg_id, dc_chat_id))')
            conn.execute('INSERT INTO message_map VALUES (1, 10, 2, -100)')
            conn.commit()

    def test_legacy_migration_supplies_missing_columns(self):
        self.legacy_db()
        database.init_db()
        with contextlib.closing(sqlite3.connect(database.DB_PATH)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM message_map').fetchall(),
                             [(1, 10, 2, -100, None, 0)])
            pk = [c[1] for c in conn.execute('PRAGMA table_info(message_map)') if c[5]]
            self.assertIn('tg_msg_id', pk)

    def test_failed_legacy_migration_rolls_back_and_raises(self):
        self.legacy_db()
        conn = database._connect()
        conn.set_authorizer(lambda action, arg1, arg2, db, source:
                            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and arg1 == 'message_map'
                            else sqlite3.SQLITE_OK)
        try:
            with self.assertRaisesRegex(RuntimeError, 'Failed to migrate message_map'):
                database.init_db()
        finally:
            conn.set_authorizer(None)
        with contextlib.closing(sqlite3.connect(database.DB_PATH)) as check:
            self.assertEqual(check.execute('SELECT * FROM message_map').fetchall(), [(1, 10, 2, -100)])
            self.assertEqual(check.execute("SELECT name FROM sqlite_master WHERE name='message_map_old'").fetchall(), [])
        database.init_db()
        self.assertEqual(database.get_bridge_message_count(10, -100), 1)

    def test_restore_preserves_both_creators_and_creation_time(self):
        database.add_bridge(10, -100, created_by_tg_id=42, created_by_dc_fp='A' * 40)
        database.add_channel_by_id(-200, 20, username='channel', created_by_tg_id=43, created_by_dc_fp='B' * 40)
        with contextlib.closing(sqlite3.connect(database.DB_PATH)) as conn:
            conn.execute('UPDATE bridges SET created_at=1234')
            conn.execute('UPDATE channels SET created_at=1235')
            conn.commit()
        rpc = MagicMock()
        rpc.get_all_account_ids.return_value = [1]
        rpc.create_broadcast.return_value = 120
        rpc.create_group_chat.return_value = 110
        rpc.get_chat_securejoin_qr_code.return_value = 'OPEN:invite'
        with patch.dict(os.environ, {'DB_PATH': database.DB_PATH, 'DC_DB_DIR': self.tmp.name}), \
             patch.object(restore_channels, 'IOTransport'), \
             patch.object(restore_channels, 'Rpc', return_value=rpc), \
             patch.object(restore_channels, 'get_telegram_chat_title', return_value='Restored'), \
             patch.object(restore_channels, 'download_telegram_chat_avatar', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            restore_channels.main()
        with contextlib.closing(sqlite3.connect(database.DB_PATH)) as conn:
            self.assertEqual(conn.execute('SELECT dc_chat_id, created_by_tg_id, created_by_dc_fp, created_at FROM bridges').fetchall(),
                             [(110, 42, 'A' * 40, 1234)])
            self.assertEqual(conn.execute('SELECT dc_chat_id, created_by_tg_id, created_by_dc_fp, created_at FROM channels').fetchall(),
                             [(120, 43, 'B' * 40, 1235)])


if __name__ == '__main__':
    unittest.main()
