import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
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
import formatting
import tg_commands
import tg_events

FP1 = "A" * 40
FP2 = "B" * 40


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class AuditFollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "followup.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_subadminremove_wildcards_do_not_match_other_keys(self):
        database.add_dc_subadmin(FP1, "a@example.org")
        database.add_dc_subadmin(FP2, "b@example.org")
        for ident in ("________", "%%%%%%%%", "ZZZZZZZZ"):
            self.assertEqual(database.remove_dc_subadmin(ident), 0)
        self.assertEqual(len(database.get_dc_subadmins()), 2)
        self.assertEqual(database.remove_dc_subadmin(FP1[-8:]), 1)
        self.assertEqual([r[1] for r in database.get_dc_subadmins()], [FP2])

    def test_telegram_filters_are_owner_only(self):
        database.set_config("admin_tg_id", "1")
        database.add_admin(2)
        for command in (tg_commands.tg_filters_command, tg_commands.tg_filteradd_command,
                        tg_commands.tg_filterdel_command):
            for user_id, allowed in ((1, True), (2, False)):
                with self.subTest(command=command.__name__, user=user_id):
                    update = MagicMock()
                    update.effective_chat.type = "private"
                    update.effective_user.id = user_id
                    update.effective_message.reply_text = AsyncMock()
                    context = MagicMock()
                    context.args = ["word"]
                    run(command(update, context))
                    texts = " ".join(str(c.args[0]) for c in update.effective_message.reply_text.call_args_list)
                    self.assertEqual("owner can manage" in texts, not allowed)

    def test_safe_href_blocks_scripts_and_entity_tricks(self):
        for bad in ("javascript:alert(1)", " JaVa\tScript:alert(1)", "&#106;avascript:alert(1)",
                    "data:text/html,x", "vbscript:x", "file:///etc/passwd"):
            self.assertFalse(formatting._is_safe_href(bad), bad)
        for good in ("https://example.org/a", "http://example.org", "mailto:a@example.org", "tg://resolve?domain=x"):
            self.assertTrue(formatting._is_safe_href(good), good)

    def test_rich_text_url_and_code_language_are_sanitised(self):
        rt = type("TextUrl", (), {"url": "javascript:alert(1)", "text": None})()
        self.assertNotIn("<a", formatting._rich_text_to_html(rt))
        ok = type("TextUrl", (), {"url": 'https://example.org/?a="b"', "text": None})()
        self.assertNotIn('"b"', formatting._rich_text_to_html(ok))
        block = type("PageBlockPreformatted", (), {"language": 'x" onmouseover="alert(1)', "text": None})()
        _md, htm = run(formatting._process_page_blocks([block], {}, {}, None, 1, [], [], set(), set()))
        self.assertNotIn('" onmouseover="', " ".join(htm))
        self.assertIn("&quot;", " ".join(htm))

    def test_legacy_group_without_username_attribute_stays_private(self):
        class LegacyChat:  # Telethon Chat has no username attribute
            id = 4242
            title = "Legacy group"
            left = False
            bot = False
        client = MagicMock()
        client.is_connected.return_value = True
        client.get_entity = AsyncMock(return_value=LegacyChat())
        client.download_profile_photo = AsyncMock(return_value=None)
        app = MagicMock()
        app.bot.get_chat = AsyncMock(side_effect=Exception("bot is not a member"))
        rpc_bot = MagicMock()
        rpc_bot.rpc.create_broadcast.return_value = 77
        rpc_bot.rpc.get_chat_securejoin_qr_code.return_value = "https://i.delta.chat/#x"
        with patch.object(bot, "userbot_client", client), patch.object(bot, "tg_app", app), \
             patch.object(bot, "dc_bot_instance", rpc_bot), patch.object(bot, "dc_accid", 1), \
             patch.object(tg_events, "_relay_channel_history", AsyncMock()):
            run(tg_events._add_channel_bridge("-4242"))
        rows = database.get_all_channels()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].get("tg_channel_username"))


if __name__ == "__main__":
    unittest.main()
