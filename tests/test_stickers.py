import os
import sys
import asyncio
import tempfile
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
import relay
from media import _sticker_file_info


class TestStickerFileInfo(unittest.TestCase):
    def test_static_sticker(self):
        self.assertEqual(_sticker_file_info(False, False), ("sticker.webp", "Sticker"))

    def test_animated_sticker_is_tgs_sticker(self):
        self.assertEqual(_sticker_file_info(True, False), ("sticker.tgs", "Sticker"))

    def test_video_sticker_is_webm_video(self):
        self.assertEqual(_sticker_file_info(False, True), ("sticker.webm", "Video"))


def _tg_msg(message_id):
    m = MagicMock()
    m.message_id = message_id
    return m


class TestDcStickerToTg(unittest.TestCase):
    def setUp(self):
        fd, self.webp = tempfile.mkstemp(suffix=".webp")
        os.write(fd, b"RIFF....WEBP")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.webp)

    def _run(self, tg_app, file_path):
        saved = []
        with patch.object(bot, "tg_app", tg_app), \
             patch.object(bot, "_get_tg_chat_desc", lambda cid: str(cid)), \
             patch("relay.database.save_message_map", lambda *a, **k: saved.append(a)):
            relay._tg_send_locks.clear()
            asyncio.run(relay.async_relay_to_tg(-100, 1, 10, file_path, "<b>Gluek</b>", 5, True, False, False, "Sticker"))
        return saved

    def test_sticker_sent_with_name_reply(self):
        tg_app = MagicMock()
        tg_app.bot.send_sticker = AsyncMock(return_value=_tg_msg(77))
        tg_app.bot.send_message = AsyncMock(return_value=_tg_msg(78))
        tg_app.bot.send_photo = AsyncMock()

        saved = self._run(tg_app, self.webp)

        tg_app.bot.send_photo.assert_not_called()
        self.assertEqual(tg_app.bot.send_sticker.call_args.kwargs["reply_to_message_id"], 5)
        name_kwargs = tg_app.bot.send_message.call_args.kwargs
        self.assertEqual(name_kwargs["text"], "<b>Gluek</b>")
        self.assertEqual(name_kwargs["reply_to_message_id"], 77)
        self.assertTrue(name_kwargs["disable_notification"])
        # Sticker mapped first, so DC replies/edits find it before the name line
        self.assertEqual([a[2] for a in saved][:2], [77, 78])

    def test_falls_back_to_photo_when_sticker_rejected(self):
        tg_app = MagicMock()
        tg_app.bot.send_sticker = AsyncMock(side_effect=Exception("Bad Request: wrong file type"))
        tg_app.bot.send_message = AsyncMock()
        tg_app.bot.send_photo = AsyncMock(return_value=_tg_msg(90))

        with patch("relay.asyncio.sleep", AsyncMock()):
            saved = self._run(tg_app, self.webp)

        tg_app.bot.send_photo.assert_called_once()
        self.assertEqual(tg_app.bot.send_photo.call_args.kwargs["caption"], "<b>Gluek</b>")
        tg_app.bot.send_message.assert_not_called()
        self.assertEqual([a[2] for a in saved], [90])

    def test_png_sticker_converted_to_webp(self):
        from PIL import Image
        fd, png = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        Image.new("RGBA", (1024, 600), (255, 0, 0, 0)).save(png)
        try:
            out = relay._sticker_to_webp(png)
            with Image.open(out) as img:
                self.assertEqual(img.format, "WEBP")
                self.assertEqual(img.size, (512, 300))
                self.assertEqual(img.mode, "RGBA")
            os.unlink(out)
        finally:
            os.unlink(png)


if __name__ == "__main__":
    unittest.main()
