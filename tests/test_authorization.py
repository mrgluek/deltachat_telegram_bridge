import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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
import tg_commands
import tg_events

FP = 'A' * 40
OTHER_FP = 'B' * 40
LINK = 'https://i.delta.chat/#private-secret'


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, 'authorization.db')
        database.init_db()
        database.set_config('admin_tg_id', '1')
        database.set_config('admin_dc_email', 'owner@example.org')
        database.add_admin(2)
        database.add_admin(3)
        self.rpc_bot = MagicMock()
        self.rpc_bot.rpc.get_basic_chat_info.return_value = {'type': 2, 'name': 'Group'}
        self.rpc_bot.has_command.return_value = False
        self.event = MagicMock()
        self.event.msg.chat_id = 10
        self.event.msg.from_id = 5
        self.event.payload = '-100'
        self.update = MagicMock()
        self.update.effective_chat.id = -100
        self.update.effective_chat.type = 'supergroup'
        self.update.effective_chat.get_member = AsyncMock(return_value=MagicMock(status='administrator'))
        self.update.effective_user.id = 2
        self.update.effective_message.reply_text = AsyncMock()
        self.update.effective_message.reply_photo = AsyncMock()

    def tearDown(self):
        database.DB_PATH = self.old_db
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_remove_idle_pair_reports_bridge_delete(self):
        database.add_bridge(10, -100)
        self.assertTrue(database.remove_bridge_pair(10, -100))
        self.assertFalse(database.remove_bridge_pair(10, -100))

    def test_remove_pair_preserves_other_message_maps(self):
        database.add_bridge(10, -100)
        database.add_bridge(10, -200)
        database.save_message_map(1, 10, 2, -100)
        database.save_message_map(3, 10, 4, -200)
        self.assertTrue(database.remove_bridge_pair(10, -100))
        self.assertEqual(database.get_bridge_message_count(10, -100), 0)
        self.assertEqual(database.get_bridge_message_count(10, -200), 1)

    def test_dc_unbridge_only_own_pairs_in_either_row_order(self):
        for own_first in (True, False):
            with self.subTest(own_first=own_first):
                pairs = [(-100, FP), (-200, OTHER_FP)]
                for tg, fp in pairs if own_first else reversed(pairs):
                    database.add_bridge(10, tg, created_by_dc_fp=fp)
                with patch.object(bot, '_is_dc_admin', return_value=False), \
                     patch.object(bot, '_dc_subadmin_fp', return_value=FP), \
                     patch.object(bot, 'main_loop', None), \
                     patch.object(bot, '_dc_send_msg_with_stats') as send, \
                     patch.object(dc_commands, '_clear_dc_caches') as clear:
                    dc_commands.unbridge_command(self.rpc_bot, 1, self.event)
                    self.assertEqual(database.get_tg_chats(10), [-200])
                    clear.assert_called_once_with(10)
                    dc_commands.unbridge_command(self.rpc_bot, 1, self.event)
                    self.assertIn('only remove bridges they created', send.call_args.args[3].text)
                database.remove_bridge(10)

    def test_dc_owner_unbridges_all_pairs(self):
        database.add_bridge(10, -100, created_by_dc_fp=FP)
        database.add_bridge(10, -200, created_by_dc_fp=OTHER_FP)
        with patch.object(bot, '_is_dc_admin', return_value=True), \
             patch.object(bot, 'main_loop', None), \
             patch.object(bot, '_dc_send_msg_with_stats'):
            dc_commands.unbridge_command(self.rpc_bot, 1, self.event)
        self.assertEqual(database.get_tg_chats(10), [])

    def test_tg_unbridge_own_pairs_and_caches_in_either_order(self):
        for own_first in (True, False):
            with self.subTest(own_first=own_first):
                pairs = [(10, 2), (20, 1)]
                for dc, creator in pairs if own_first else reversed(pairs):
                    database.add_bridge(dc, -100, created_by_tg_id=creator)
                with patch.object(tg_commands, '_clear_dc_caches') as clear, \
                     patch.object(tg_commands, '_userbot_leave_chat', new_callable=AsyncMock) as leave:
                    self.run_async(tg_commands.tg_unbridge_command(self.update, MagicMock()))
                    self.assertEqual(database.get_dc_chats(-100), [20])
                    clear.assert_called_once_with(10)
                    leave.assert_not_called()
                    self.run_async(tg_commands.tg_unbridge_command(self.update, MagicMock()))
                    self.assertIn('only remove bridges they created', self.update.effective_message.reply_text.call_args.args[0])
                database.remove_bridge(20)

    def test_tg_owner_and_public_admin_unbridge_all_idle_pairs(self):
        for public in (False, True):
            with self.subTest(public=public):
                database.set_config('admin_tg_id', '' if public else '1')
                self.update.effective_user.id = 1 if not public else 3
                database.add_bridge(10, -100, created_by_tg_id=2)
                database.add_bridge(20, -100, created_by_tg_id=3)
                with patch.object(tg_commands, '_clear_dc_caches') as clear, \
                     patch.object(tg_commands, '_userbot_leave_chat', new_callable=AsyncMock) as leave:
                    self.run_async(tg_commands.tg_unbridge_command(self.update, MagicMock()))
                    self.assertEqual(database.get_dc_chats(-100), [])
                    self.assertEqual({c.args[0] for c in clear.call_args_list}, {10, 20})
                    leave.assert_awaited_once_with(-100)

    def test_tg_invites_owner_own_other_and_public_for_link_and_qr(self):
        database.add_bridge(10, -100, created_by_tg_id=2)
        database.add_bridge(20, -100, created_by_tg_id=1)
        database.add_bridge(30, -200, created_by_tg_id=3)
        for command in (tg_commands.tg_invite_command, tg_commands.tg_inviteqr_command):
            for user, public, expected in ((1, False, [10, 20]), (2, False, [10]),
                                           (3, False, []), (3, True, [10, 20])):
                with self.subTest(command=command.__name__, user=user, public=public):
                    database.set_config('admin_tg_id', '' if public else '1')
                    self.update.effective_user.id = user
                    rpc = MagicMock()
                    rpc.get_basic_chat_info.return_value = {'name': 'Group'}
                    rpc.get_chat_securejoin_qr_code.return_value = LINK
                    with patch.object(bot, 'dc_bot_instance', MagicMock(rpc=rpc)), \
                         patch.object(bot, 'dc_accid', 1):
                        self.run_async(command(self.update, MagicMock()))
                    self.assertEqual([c.args[1] for c in rpc.get_chat_securejoin_qr_code.call_args_list], expected)

    def test_public_invites_require_group_admin(self):
        database.set_config('admin_tg_id', '')
        self.update.effective_chat.get_member.return_value.status = 'member'
        self.assertFalse(self.run_async(tg_events._check_invite_permissions(self.update)))

    def test_tg_status_owner_only_including_public_mode(self):
        self.update.effective_chat.type = 'private'
        for user, public, allowed in ((1, False, True), (2, False, False), (3, True, False)):
            with self.subTest(user=user, public=public):
                database.set_config('admin_tg_id', '' if public else '1')
                self.update.effective_user.id = user
                with patch.object(bot, 'generate_status_report', new_callable=AsyncMock, return_value='report') as report:
                    self.run_async(tg_commands.tg_status_command(self.update, MagicMock()))
                    self.assertEqual(report.await_count, int(allowed))
                if not allowed:
                    self.assertIn('owner', self.update.effective_message.reply_text.call_args.args[0])

    def test_dc_status_denies_subadmin_and_public_mode(self):
        for public in (False, True):
            with self.subTest(public=public):
                database.set_config('admin_dc_email', '' if public else 'owner@example.org')
                with patch.object(bot, '_is_dc_admin', return_value=False), \
                     patch.object(bot, '_dc_send_msg_with_stats') as send, \
                     patch.object(dc_commands, 'generate_status_report', new_callable=AsyncMock) as report:
                    dc_commands.dc_status_command(self.rpc_bot, 1, self.event)
                report.assert_not_called()
                self.assertIn('administrator', send.call_args.args[3].text)

    def test_dc_owner_status_builds_full_report(self):
        def schedule(coro, loop):
            return self.run_async(coro)
        with patch.object(bot, '_is_dc_admin', return_value=True), \
             patch.object(bot, 'main_loop', object()), \
             patch.object(bot, '_dc_send_msg_with_stats') as send, \
             patch.object(dc_commands, 'generate_status_report', new_callable=AsyncMock, return_value='full report') as report, \
             patch.object(dc_commands.asyncio, 'run_coroutine_threadsafe', side_effect=schedule):
            dc_commands.dc_status_command(self.rpc_bot, 1, self.event)
        report.assert_awaited_once_with(is_html=False, bot_ref=self.rpc_bot, accid=1)
        self.assertEqual(send.call_args.args[3].text, 'full report')

    def test_dc_channel_invites_private_vs_group_public_vs_private(self):
        for username in (None, 'public_channel'):
            cid = database.add_channel_by_id(-100 if username is None else -200, 20, LINK, username=username)
            for private_chat in (False, True):
                for qr in (False, True):
                    with self.subTest(username=username, private_chat=private_chat, qr=qr):
                        self.event.msg.text = f'/channel{cid}' + ('qr' if qr else '')
                        with patch.object(bot, '_is_dc_admin', return_value=True), \
                             patch.object(bot, '_is_private_chat', return_value=private_chat), \
                             patch.object(bot, '_dc_send_msg_with_stats') as send:
                            dc_events.handle_dc_message(self.rpc_bot, 1, self.event)
                        data = send.call_args.args[3]
                        if username is None and not private_chat:
                            self.assertIn('private chat', data.text)
                            self.assertNotIn(LINK, data.text)
                            self.assertFalse(getattr(data, 'file', None))
                        elif qr:
                            self.assertTrue(data.file)
                        else:
                            self.assertIn(LINK, data.text)

    def test_dc_private_invite_denies_other_creator(self):
        cid = database.add_channel_by_id(-100, 20, LINK, created_by_dc_fp=OTHER_FP)
        self.event.msg.text = f'/channel{cid}'
        with patch.object(bot, '_is_dc_admin', return_value=False), \
             patch.object(bot, '_dc_subadmin_fp', return_value=FP), \
             patch.object(bot, '_dc_send_msg_with_stats') as send:
            dc_events.handle_dc_message(self.rpc_bot, 1, self.event)
        self.assertIn('not found', send.call_args.args[3].text)

    def test_dc_channeladd_delivery_by_resolved_visibility(self):
        for username, private_chat, failure in ((None, False, False), (None, False, True),
                                                (None, False, 'send'), (None, True, False),
                                                ('public_channel', False, False)):
            with self.subTest(username=username, private_chat=private_chat, failure=failure):
                # Exercise the real creation helper so the routing uses resolved metadata,
                # including a numeric source which is actually public.
                tg_chat = MagicMock(type='channel', title='Channel', id=-1001234567, username=username, photo=None)
                app = MagicMock()
                app.bot.get_chat = AsyncMock(return_value=tg_chat)
                self.rpc_bot.rpc.create_broadcast.return_value = 20
                self.rpc_bot.rpc.get_chat_securejoin_qr_code.return_value = LINK
                self.rpc_bot.rpc.create_chat_by_contact_id.side_effect = ValueError('unavailable') if failure is True else None
                self.rpc_bot.rpc.create_chat_by_contact_id.return_value = 50
                def is_private(b, a, cid):
                    return private_chat or cid == 50
                def schedule(coro, loop):
                    return self.run_async(coro)
                def send_result(b, a, cid, data):
                    return None if failure == 'send' and cid == 50 else 123
                with patch.object(bot, '_is_dc_admin', return_value=True), \
                     patch.object(bot, '_is_private_chat', side_effect=is_private), \
                     patch.object(bot, '_dc_send_msg_with_stats', side_effect=send_result) as send, \
                     patch.object(bot, 'main_loop', object()), \
                     patch.object(bot, 'dc_bot_instance', self.rpc_bot), \
                     patch.object(bot, 'dc_accid', 1), \
                     patch.object(bot, 'tg_app', app), \
                     patch.object(bot, 'userbot_client', None), \
                     patch.object(tg_events, '_relay_channel_history', new_callable=AsyncMock), \
                     patch.object(dc_commands.asyncio, 'run_coroutine_threadsafe', side_effect=schedule):
                    dc_commands.dc_channeladd_command(self.rpc_bot, 1, self.event)
                self.assertIsNotNone(database.get_channel_by_tg_id(-1001234567), [c.args[3].text for c in send.call_args_list])
                group_texts = [c.args[3].text for c in send.call_args_list if c.args[2] == 10]
                private_texts = [c.args[3].text for c in send.call_args_list if c.args[2] == 50]
                if username is None and not private_chat:
                    self.assertTrue(all(LINK not in text for text in group_texts))
                    if failure:
                        self.assertIn('could not be sent', group_texts[-1])
                        if failure is True:
                            self.assertEqual(private_texts, [])
                    else:
                        self.assertIn('sent privately', group_texts[-1])
                        self.assertIn(LINK, private_texts[-1])
                else:
                    self.assertIn(LINK, group_texts[-1])
                database.remove_channel(database.get_channel_by_tg_id(-1001234567)['id'])


if __name__ == '__main__':
    unittest.main()
