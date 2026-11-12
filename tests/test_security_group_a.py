import asyncio
import dataclasses
import fnmatch
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import deltachat2
except ImportError:
    mock_dc = MagicMock()

    @dataclasses.dataclass
    class MsgData:
        text: str = ""
        file: str = ""
        override_sender_name: str = None

    mock_dc.MsgData = MsgData
    sys.modules['deltachat2'] = mock_dc

try:
    import deltabot_cli
except ImportError:
    mock_cli = MagicMock()
    mock_cli.BotCli.return_value.on.side_effect = lambda *a, **k: lambda f: f
    mock_cli.BotCli.return_value.on_init.side_effect = lambda f: f
    mock_cli.BotCli.return_value.on_start.side_effect = lambda f: f
    sys.modules['deltabot_cli'] = mock_cli

import database
import bot
import dc_commands
import dc_events
import dc_fingerprint
import dc_helpers
import logging_setup

FP40 = "A" * 40
FP64 = "B" * 64
SELF_FP = "C" * 40
EMAIL = "owner@example.org"


class TempDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.orig_db = database.DB_PATH
        database.close_db()
        database.DB_PATH = os.path.join(self.temp.name, "security.db")
        database.init_db()
        self.env = patch.dict(os.environ, {"ADMIN_DC_FINGERPRINT": "", "ADMIN_DC_EMAIL": ""})
        self.env.start()

    def tearDown(self):
        database.close_db()
        database.DB_PATH = self.orig_db
        self.env.stop()
        self.temp.cleanup()


class TestPinnedOwner(TempDatabaseTest):
    def setUp(self):
        super().setUp()
        database.set_config("admin_dc_email", EMAIL)
        database.set_config("admin_dc_fingerprint", FP40)
        self.dc = MagicMock()
        self.dc.rpc.get_contact.return_value = SimpleNamespace(address=EMAIL)
        self.dc.rpc.get_chat_securejoin_qr_code.return_value = f"https://i.delta.chat/#{SELF_FP}&v=3"

    def test_missing_key_and_lookup_failure_reject_owner_email(self):
        for info, error in (("No encryption.", None), (None, RuntimeError("RPC failed"))):
            with self.subTest(error=error):
                self.dc.rpc.get_contact_encryption_info.return_value = info
                self.dc.rpc.get_contact_encryption_info.side_effect = error
                self.assertFalse(dc_helpers._is_dc_admin(self.dc, 1, 7))
                self.assertFalse(database.is_authorized_sender(EMAIL))

    def test_matching_and_wrong_fingerprint(self):
        for fp in (FP40, FP64):
            with self.subTest(fp=fp):
                self.dc.rpc.get_contact_encryption_info.return_value = f"Me:\n{SELF_FP}\n\nPeer:\n{fp}"
                self.assertEqual(dc_helpers._is_dc_admin(self.dc, 1, 7), fp == FP40)
                self.assertEqual(database.is_authorized_sender(EMAIL, fp), fp == FP40)

    def test_no_pin_keeps_legacy_email_owner(self):
        database.set_config("admin_dc_fingerprint", "")
        self.assertTrue(dc_helpers._is_dc_admin(self.dc, 1, 7))
        self.assertTrue(database.is_authorized_sender(EMAIL))

    def test_authorization_requires_exact_fingerprint_token(self):
        for value in ("0" + FP40, FP40 + "0", "not-a-key" + FP40):
            with self.subTest(value=value):
                self.assertFalse(database.is_authorized_sender(EMAIL, value))
        self.assertTrue(database.is_authorized_sender("other@example.org", FP64 + "," + FP40))

    def test_invalid_configured_pin_does_not_enable_email_fallback(self):
        database.set_config("admin_dc_fingerprint", "invalid")
        self.assertFalse(database.is_authorized_sender(EMAIL))
        self.assertFalse(database.is_authorized_sender(EMAIL, "invalid"))
        self.assertFalse(dc_helpers._is_dc_admin(self.dc, 1, 7))


class TestAdminContactSelection(TempDatabaseTest):
    def setUp(self):
        super().setUp()
        database.set_config("admin_dc_email", EMAIL)
        database.set_config("admin_dc_fingerprint", FP40)
        self.dc = MagicMock()
        self.dc.rpc.get_contact_ids.return_value = [10, 11, 12]
        self.contacts = {
            10: {"address": EMAIL, "is_key_contact": False},
            11: {"address": EMAIL, "is_key_contact": True, "last_seen": 900},
            12: {"address": EMAIL, "is_key_contact": True, "last_seen": 1},
        }
        self.dc.rpc.get_contact.side_effect = lambda a, c: self.contacts[c]
        self.dc.rpc.lookup_contact_id_by_addr.return_value = 10
        self.fps = patch.object(dc_helpers, "_get_contact_fingerprint", side_effect=lambda b, a, c, *args: {11: FP64, 12: FP40}[c])
        self.fps.start()
        self.addCleanup(self.fps.stop)

    def test_pinned_contact_precedes_newer_key(self):
        self.assertEqual(dc_helpers.admin_key_contact_id(self.dc, 1), 12)

    def test_missing_pin_never_uses_other_key_or_address(self):
        self.dc.rpc.get_contact_ids.return_value = [10, 11]
        with self.assertLogs("tg_dc_bridge", level="WARNING"):
            self.assertIsNone(dc_helpers.admin_key_contact_id(self.dc, 1))
        self.dc.rpc.lookup_contact_id_by_addr.assert_not_called()

    def test_lookup_failures_fail_closed(self):
        for method in ("get_contact_ids", "get_contact"):
            with self.subTest(method=method), patch.object(self.dc.rpc, method, side_effect=RuntimeError("RPC failed")):
                self.assertIsNone(dc_helpers.admin_key_contact_id(self.dc, 1))
        self.dc.rpc.lookup_contact_id_by_addr.assert_not_called()
        with patch.object(dc_helpers, "_get_contact_fingerprint", return_value=None):
            self.assertIsNone(dc_helpers.admin_key_contact_id(self.dc, 1))

    def test_unpinned_legacy_fallbacks(self):
        database.set_config("admin_dc_fingerprint", "")
        self.assertEqual(dc_helpers.admin_key_contact_id(self.dc, 1), 11)
        self.dc.rpc.get_contact_ids.return_value = [10]
        self.assertEqual(dc_helpers.admin_key_contact_id(self.dc, 1), 10)

    def test_admin_notification_skips_missing_pinned_contact(self):
        self.dc.rpc.get_contact_ids.return_value = [10, 11]
        with patch.object(bot, "dc_bot_instance", self.dc), patch.object(bot, "dc_accid", 1), \
             patch.object(logging_setup, "_admin_dc_chat_id_cache", None), \
             patch.object(logging_setup, "_get_cached_admin_targets", return_value=(None, EMAIL)), \
             self.assertLogs("tg_dc_bridge", level="WARNING"):
            logging_setup._send_admin_dc_message_bg("Login code")
        self.dc.rpc.create_chat_by_contact_id.assert_not_called()
        self.dc.rpc.send_msg.assert_not_called()


class Channel:
    def __init__(self, broadcast=False, megagroup=False):
        self.id = 42
        self.broadcast = broadcast
        self.megagroup = megagroup


class User:
    def __init__(self, bot=False):
        self.id = 43
        self.bot = bot


class TestDirectPostEntities(TempDatabaseTest):
    def test_only_channels_and_bridged_bots_are_read(self):
        import telethon.tl.types as types
        # (entity, bridged, requested by the owner, allowed): bridged bots are read for the owner only
        cases = [(Channel(broadcast=True), False, False, True),
                 (Channel(megagroup=True), False, False, True),
                 (User(), False, True, False), (User(), True, True, False),
                 (User(bot=True), False, True, False), (User(bot=True), True, False, False),
                 (User(bot=True), True, True, True)]
        for entity, bridged, owner, allowed in cases:
            with self.subTest(entity=type(entity).__name__, bridged=bridged, owner=owner, allowed=allowed):
                client = MagicMock()
                client.get_entity = AsyncMock(return_value=entity)
                client.get_messages = AsyncMock(return_value=None)
                with patch.object(types, "Channel", Channel), patch.object(types, "User", User), \
                     patch.object(bot, "userbot_client", client), \
                     patch.object(database, "get_channel_by_tg_id", return_value={"id": 1} if bridged else None), \
                     patch.object(bot, "_extract_public_tg_post_rich", new_callable=AsyncMock, return_value=None) as public, \
                     patch.object(dc_events.logger, "info") as info:
                    asyncio.run(dc_events._async_handle_direct_tg_post(MagicMock(), 1, 2, "example", 5, owner_requested=owner))
                if allowed:
                    client.get_messages.assert_awaited_once_with(entity, ids=5)
                else:
                    client.get_messages.assert_not_awaited()
                    self.assertTrue(any("refus" in str(c).lower() for c in info.call_args_list))
                public.assert_awaited_once_with("example", 5)


class TestDockerIgnore(unittest.TestCase):
    def test_runtime_secrets_excluded_and_sources_kept(self):
        patterns = [s.strip() for s in (Path(__file__).resolve().parents[1] / ".dockerignore").read_text().splitlines()
                    if s.strip() and not s.startswith("#")]
        for pattern in (".env*", "*.db", "*.db-*", "*.sqlite*", "*.session", "*.session-journal", "*.session*", "data/", "*.pem", "*.key"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, patterns)
        for name in ("requirements.txt", "bot.py"):
            self.assertFalse(any(fnmatch.fnmatch(name, p) for p in patterns))


class TestSubadminFingerprints(TempDatabaseTest):
    def test_v4_and_v6_add_match_and_duplicate(self):
        for fp in (FP40, FP64):
            with self.subTest(fp=fp):
                self.assertTrue(database.add_dc_subadmin(fp.lower(), "bob@example.org"))
                self.assertEqual(database.is_dc_subadmin_fp(fp.lower()), fp)
                self.assertEqual(database.is_dc_subadmin_fp(SELF_FP + "," + fp), fp)
                self.assertFalse(database.add_dc_subadmin(fp, "bob@example.org"))
                self.assertEqual(dc_fingerprint.parse_key_fingerprints(f"Peer:\n{fp}"), [fp])

    def test_validity_helper_and_invalid_keys(self):
        for fp in (FP40, FP64, ":".join(["aaaa"] * 10)):
            self.assertTrue(database.is_valid_dc_fingerprint(fp))
        for fp in (None, "", "short", "A" * 39, "B" * 63, "C" * 65, FP40 + "garbage"):
            with self.subTest(fp=fp):
                self.assertFalse(database.is_valid_dc_fingerprint(fp))
                self.assertFalse(database.add_dc_subadmin(fp, "bob@example.org"))
                self.assertIsNone(database.is_dc_subadmin_fp(fp))

    def test_command_replies_for_valid_invalid_and_duplicate(self):
        dc = MagicMock()
        event = SimpleNamespace(msg=SimpleNamespace(chat_id=2, from_id=3, file=None), payload="bob@example.org")
        for fps, expected in (([FP40], "is now a sub-admin"), ([FP64], "is now a sub-admin"),
                              ([FP64], "already a sub-admin"), (["invalid"], "Invalid key")):
            with self.subTest(fps=fps, expected=expected), \
                 patch.object(bot, "_is_private_chat", return_value=True), \
                 patch.object(bot, "_is_dc_admin", return_value=True), \
                 patch.object(dc_commands, "_dc_contact_key_fingerprints", return_value=(fps, "Bob")), \
                 patch.object(bot, "_dc_send_msg_with_stats") as send:
                dc_commands.dc_subadminadd_command(dc, 1, event)
                self.assertIn(expected, send.call_args.args[3].text)


if __name__ == "__main__":
    unittest.main()
