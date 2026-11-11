import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import database
import tg_commands

TEST_DB = "test_tg_adminadd.db"


class User:
    def __init__(self, id, bot=False):
        self.id = id
        self.bot = bot


class Channel:
    id = 5


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestResolve(unittest.TestCase):
    def _client(self, entity=None, exc=None):
        c = MagicMock()
        c.is_connected.return_value = True
        c.get_entity = AsyncMock(return_value=entity, side_effect=exc)
        return c

    def test_numeric(self):
        self.assertEqual(run(tg_commands._resolve_tg_user_arg("123")), (123, None))

    def test_username_resolved(self):
        with patch.object(bot, "userbot_client", self._client(User(42))):
            self.assertEqual(run(tg_commands._resolve_tg_user_arg("@alice_x")), (42, None))
            self.assertEqual(run(tg_commands._resolve_tg_user_arg("alice_x")), (42, None))

    def test_rejects_channel_bot_unknown_and_bad_names(self):
        with patch.object(bot, "userbot_client", self._client(Channel())):
            self.assertIsNone(run(tg_commands._resolve_tg_user_arg("@somechan"))[0])
        with patch.object(bot, "userbot_client", self._client(User(7, bot=True))):
            self.assertIsNone(run(tg_commands._resolve_tg_user_arg("@some_bot"))[0])
        with patch.object(bot, "userbot_client", self._client(exc=ValueError("no"))):
            self.assertIn("Could not find", run(tg_commands._resolve_tg_user_arg("@nobody1"))[1])
        self.assertIn("numeric", run(tg_commands._resolve_tg_user_arg("@a!"))[1])

    def test_userbot_offline(self):
        with patch.object(bot, "userbot_client", None):
            self.assertIn("userbot is not connected", run(tg_commands._resolve_tg_user_arg("@alice_x"))[1])


class TestAdminAdd(unittest.TestCase):
    def setUp(self):
        self.orig = database.DB_PATH
        database.DB_PATH = TEST_DB
        database.init_db()
        database.set_config("admin_tg_id", "1")

    def tearDown(self):
        database.DB_PATH = self.orig
        for f in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
            if os.path.exists(f):
                os.remove(f)

    def test_add_and_remove_by_username(self):
        upd = MagicMock()
        upd.effective_chat.type = "private"
        upd.effective_user.id = 1
        upd.effective_message.reply_text = AsyncMock()
        ctx = MagicMock()
        client = MagicMock()
        client.is_connected.return_value = True
        client.get_entity = AsyncMock(return_value=User(42))
        with patch.object(bot, "userbot_client", client):
            ctx.args = ["@alice_x"]
            run(tg_commands.tg_adminadd_command(upd, ctx))
            self.assertTrue(database.is_admin(42))
            run(tg_commands.tg_adminremove_command(upd, ctx))
            self.assertFalse(database.is_admin(42))


if __name__ == "__main__":
    unittest.main()


class TestReplyOrSend(unittest.TestCase):
    def _upd(self, reply_exc):
        upd = MagicMock()
        upd.effective_chat.id = -100
        upd.effective_message.is_topic_message = False
        upd.effective_message.reply_text = AsyncMock(side_effect=reply_exc)
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        return upd, ctx

    def test_falls_back_when_command_message_was_deleted(self):
        from telegram.error import BadRequest
        upd, ctx = self._upd(BadRequest("Message to be replied not found"))
        run(tg_commands._reply_or_send(upd, ctx, "hi", parse_mode="HTML"))
        ctx.bot.send_message.assert_awaited_once_with(-100, "hi", message_thread_id=None, parse_mode="HTML")

    def test_other_errors_propagate(self):
        from telegram.error import BadRequest
        upd, ctx = self._upd(BadRequest("Chat is too big"))
        with self.assertRaises(BadRequest):
            run(tg_commands._reply_or_send(upd, ctx, "hi"))
        ctx.bot.send_message.assert_not_awaited()
