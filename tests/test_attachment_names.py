import os
import sys
import asyncio
import dataclasses
import tempfile
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
import relay
import tg_events
from deltachat2 import MsgData, Rpc
from dc_helpers import FileMsgData


class TestDcFileNameToTg(unittest.TestCase):
    """DC keeps blobs under hashed names; TG must get the original file name."""

    def setUp(self):
        fd, self.blob = tempfile.mkstemp(suffix=".zip")
        os.write(fd, b"PK")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.blob)

    def _send(self, file_name):
        tg_app = MagicMock()
        sent = MagicMock()
        sent.message_id = 50
        tg_app.bot.send_document = AsyncMock(return_value=sent)
        with patch.object(bot, "tg_app", tg_app), \
             patch.object(bot, "_get_tg_chat_desc", lambda cid: str(cid)), \
             patch("relay.database.save_message_map", lambda *a, **k: None):
            relay._tg_send_locks.clear()
            asyncio.run(relay.async_relay_to_tg(-100, 1, 10, self.blob, "<b>Gluek</b>", None,
                                                False, False, False, "File", file_name))
        return tg_app.bot.send_document.call_args.kwargs["filename"]

    def test_original_name_is_used(self):
        self.assertEqual(self._send("passage-iroh.zip"), "passage-iroh.zip")

    def test_falls_back_to_blob_name(self):
        self.assertEqual(self._send(None), os.path.basename(self.blob))

    def test_name_of_a_file_that_was_still_downloading(self):
        tg_app = MagicMock()
        sent = MagicMock()
        sent.message_id = 50
        tg_app.bot.send_document = AsyncMock(return_value=sent)
        dc_bot = MagicMock()
        dc_bot.rpc.get_message.return_value = MagicMock(file=self.blob, file_name="passage-iroh.zip")
        with patch.object(bot, "tg_app", tg_app), \
             patch.object(bot, "dc_bot_instance", dc_bot), \
             patch.object(bot, "_get_tg_chat_desc", lambda cid: str(cid)), \
             patch("relay.asyncio.sleep", AsyncMock()), \
             patch("relay.database.save_message_map", lambda *a, **k: None):
            relay._tg_send_locks.clear()
            asyncio.run(relay.async_relay_to_tg(-100, 1, 10, None, "<b>Gluek</b>", None,
                                                False, False, False, "File", None))
        self.assertEqual(tg_app.bot.send_document.call_args.kwargs["filename"], "passage-iroh.zip")


@unittest.skipUnless(dataclasses.is_dataclass(MsgData), "needs the real deltachat2")
class TestFileMsgData(unittest.TestCase):
    def _json(self, data):
        transport = MagicMock()
        Rpc(transport).send_msg(1, 2, data)
        return transport.call.call_args.args[3]

    def test_filename_reaches_the_core(self):
        data = FileMsgData(text="hi", file="/tmp/tmp6_dbmamo.json")
        data.filename = "log-windows-2.json"
        self.assertEqual(self._json(data)["filename"], "log-windows-2.json")

    def test_no_filename_is_null(self):
        self.assertIsNone(self._json(FileMsgData(text="hi"))["filename"])


class TestTgFileNameToDc(unittest.TestCase):
    """Only the media downloaded from Telegram goes under its Telegram name."""

    MEDIA = ("photo", "video", "animation", "voice", "audio", "document", "sticker", "video_note")

    def setUp(self):
        fd, self.page_image = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)

    def tearDown(self):
        if os.path.exists(self.page_image):
            os.unlink(self.page_image)

    def _post(self, **media):
        update = MagicMock()
        post = update.channel_post
        post.chat.id = -100777
        post.chat.username = "chan"
        post.message_id = 5
        post.text = "text"
        post.caption = None
        post.entities = post.caption_entities = []
        post.author_signature = None
        post.media_group_id = None
        for attr in ("paid_media", "story", "giveaway", "poll", "contact", "invoice", "venue", "location"):
            setattr(post, attr, None)
        for attr in self.MEDIA:
            setattr(post, attr, media.get(attr))
        return update

    def _tg_file(self, **attrs):
        tg_file = MagicMock(**attrs)
        tg_file.get_file = AsyncMock(return_value=MagicMock(download_to_drive=AsyncMock()))
        return tg_file

    def _rich_post(self):
        return MagicMock(text_markdown="text", image_urls=["u1", "u2"], videos=[], is_rich=True,
                         author_name="Chan", teaser="", album_post_ids=[])

    def _relay(self, update, rich_mode="webxdc", rich_post=None):
        """Run the channel handler; return the first MsgData sent to DC."""
        dc_bot = MagicMock()
        dc_bot.rpc.send_msg.return_value = 100
        sent = []
        dc_bot.rpc.send_msg.side_effect = lambda acc, chat, data: sent.append(data) or 100
        with patch.object(bot, "dc_bot_instance", dc_bot), patch.object(bot, "dc_accid", 1), \
             patch.object(bot, "_extract_public_tg_post_rich", AsyncMock(return_value=rich_post)), \
             patch.object(bot, "_package_tg_post_webxdc", AsyncMock(return_value=True)), \
             patch.object(tg_events, "_download_image_url", AsyncMock(return_value=self.page_image)), \
             patch.object(tg_events, "_get_cached_dc_channel_chat_id", lambda cid: 7), \
             patch.object(tg_events, "_get_cached_last_msg_id", lambda cid: 0), \
             patch.object(tg_events, "_update_cached_last_msg_id", lambda *a: None), \
             patch.object(tg_events, "_mark_processed", lambda *a: False), \
             patch.object(tg_events, "_is_media_group_processed", lambda *a: False), \
             patch.object(tg_events, "_is_rate_limited", lambda *a: False), \
             patch.object(tg_events, "_wait_for_global_dc_rate_limit", AsyncMock()), \
             patch.object(tg_events, "track_dc_outgoing", lambda *a: None), \
             patch("tg_events.database.get_dc_msg_id", lambda *a: None), \
             patch("tg_events.database.get_rich_mode", lambda: rich_mode), \
             patch("tg_events.database.save_message_map", lambda *a, **k: None), \
             patch("tg_events.database.mark_media_group_processed", lambda *a, **k: None):
            asyncio.run(tg_events.handle_tg_channel_post(update, MagicMock()))
        return sent[0]

    def test_document_keeps_its_name(self):
        data = self._relay(self._post(document=self._tg_file(file_name="log-windows-2.json")))
        self.assertEqual(data.filename, "log-windows-2.json")

    def test_album_webxdc_is_not_named_after_a_photo(self):
        update = self._post(photo=[self._tg_file()])
        update.channel_post.media_group_id = "album1"
        data = self._relay(update, rich_post=self._rich_post())
        self.assertTrue(data.file.endswith(".xdc"))
        self.assertIsNone(data.filename)

    def test_page_image_is_not_named_after_a_skipped_video(self):
        video = self._tg_file(file_name="clip.mp4", file_size=30 * 1024 * 1024, qualities=[])
        update = self._post(video=video)
        update.channel_post.media_group_id = "album2"
        data = self._relay(update, rich_mode="split", rich_post=self._rich_post())
        self.assertEqual(data.file, self.page_image)
        self.assertIsNone(data.filename)


if __name__ == "__main__":
    unittest.main()
