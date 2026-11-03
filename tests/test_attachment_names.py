import os
import sys
import asyncio
import tempfile
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
import relay


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


if __name__ == "__main__":
    unittest.main()
