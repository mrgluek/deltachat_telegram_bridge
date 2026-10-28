"""Photo/video albums in bridged groups are bundled into one WebXDC (tg_albums)."""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
import database
import tg_albums
import tg_events

TEST_DB = "test_albums.db"
TG, DC = -1001, 42


def _update(msg_id, group="g1", caption=None, photo=True, update_id=None):
    file_obj = SimpleNamespace(file_size=1000, get_file=AsyncMock())
    message = SimpleNamespace(
        message_id=msg_id, media_group_id=group, caption=caption, caption_entities=[],
        photo=[file_obj] if photo else None, video=None if photo else file_obj,
        from_user=SimpleNamespace(first_name="Alice", last_name="B", is_bot=False),
        chat=SimpleNamespace(username=None, title="Group"), reply_to_message=None, date=None)
    return SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=TG, type="supergroup"),
                           update_id=update_id or msg_id)


class AlbumTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_db = database.DB_PATH
        database.DB_PATH = TEST_DB
        database.init_db()
        conn = database._connect()
        conn.cursor().execute("INSERT INTO bridges (dc_chat_id, tg_chat_id) VALUES (?, ?)", (DC, TG))
        conn.commit()
        conn.close()
        tg_albums._albums.clear()
        self.sent = []

        def fake_send(inst, accid, chat_id, msg_data):
            self.sent.append(msg_data)
            return 100 + len(self.sent)

        self.package = AsyncMock(return_value=True)
        self.patches = [
            patch.object(tg_albums, "ALBUM_DEBOUNCE", 0.05),
            patch.object(tg_albums, "_is_rate_limited", return_value=False),
            patch.object(tg_albums, "_wait_for_global_dc_rate_limit", AsyncMock()),
            patch.object(tg_albums, "_download_item", AsyncMock(return_value=None)),
            patch.object(bot, "dc_bot_instance", MagicMock()),
            patch.object(bot, "dc_accid", 1),
            patch.object(bot, "_package_tg_post_webxdc", self.package),
            patch.object(bot, "_dc_send_msg_with_stats", side_effect=fake_send),
        ]
        for p in self.patches:
            p.start()
        tg_albums._download_item.side_effect = self._download

    async def _download(self, item, chat_id):
        return f"/nonexistent/{item['update'].message.message_id}.jpg"

    def tearDown(self):
        for p in self.patches:
            p.stop()
        database.DB_PATH = self.orig_db
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(TEST_DB + suffix):
                os.remove(TEST_DB + suffix)

    async def _feed(self, *updates):
        for u in updates:
            self.assertEqual(tg_albums.try_buffer(u, "webxdc"), ("relayed", ""))
        await asyncio.sleep(0.4)

    async def test_album_becomes_one_message(self):
        await self._feed(_update(1), _update(2, caption="Trip #1"), _update(3, photo=False))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].file.endswith(".xdc"))
        self.assertIn("Trip #1", self.sent[0].text)
        self.assertIn("2 photos, 1 video", self.sent[0].text)
        self.assertEqual(self.sent[0].override_sender_name, "Alice B")
        # all items map to the single DC message, so replies find it
        for tg_id in (1, 2, 3):
            self.assertEqual(database.get_dc_msg_id(tg_id, TG, DC), 101)

    async def test_filtered_caption_drops_whole_album(self):
        with patch.object(tg_albums, "is_text_filtered", return_value=(True, "#ad")):
            await self._feed(_update(1), _update(2, caption="#ad"))
        self.assertEqual(self.sent, [])
        self.package.assert_not_called()

    async def test_package_failure_falls_back_to_single_messages(self):
        self.package.return_value = False
        replayed = []

        async def fake_handle(update, context):
            replayed.append(update.message.message_id)
            self.assertIsNone(tg_albums.try_buffer(update, "webxdc"))  # not buffered again

        with patch.object(tg_events, "handle_tg_message", fake_handle):
            await self._feed(_update(1), _update(2))
        self.assertEqual(replayed, [1, 2])
        self.assertEqual(self.sent, [])

    async def test_single_item_is_relayed_normally(self):
        replayed = []

        async def fake_handle(update, context):
            replayed.append(update.message.message_id)

        with patch.object(tg_events, "handle_tg_message", fake_handle):
            await self._feed(_update(1))
        self.assertEqual(replayed, [1])
        self.package.assert_not_called()

    async def test_other_modes_and_non_albums_are_not_buffered(self):
        self.assertIsNone(tg_albums.try_buffer(_update(1), "split"))
        self.assertIsNone(tg_albums.try_buffer(_update(1), "off"))
        self.assertIsNone(tg_albums.try_buffer(_update(1, group=None), "webxdc"))
        self.assertEqual(tg_albums._albums, {})

    async def test_rate_limit_counts_album_once(self):
        with patch.object(tg_albums, "_is_rate_limited", return_value=True) as limited:
            self.assertEqual(tg_albums.try_buffer(_update(1), "webxdc"), ("skipped", "rate limit"))
            self.assertEqual(tg_albums.try_buffer(_update(2), "webxdc"), ("skipped", "rate limit"))
            self.assertEqual(limited.call_count, 1)
            await asyncio.sleep(0.4)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
