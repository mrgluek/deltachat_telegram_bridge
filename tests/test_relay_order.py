import os
import sys
import asyncio
import unittest
from unittest.mock import MagicMock, patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot
import relay


class TestDcToTgRelayOrder(unittest.TestCase):
    def test_relays_to_same_tg_chat_keep_dc_order(self):
        """A slow media relay must not be overtaken by a later text message."""
        sent = []

        async def fake_send(chat_id, text, **kwargs):
            # The first message is slow to send (like a photo still uploading)
            if "first" in text:
                await asyncio.sleep(0.2)
            sent.append(text)
            msg = MagicMock()
            msg.message_id = len(sent)
            return msg

        tg_app = MagicMock()
        tg_app.bot.send_message = fake_send

        async def run():
            relay._tg_send_locks.clear()
            await asyncio.gather(
                relay.async_relay_to_tg(-100, 1, 10, None, "first", None, False, False, False),
                relay.async_relay_to_tg(-100, 1, 11, None, "second", None, False, False, False),
                relay.async_relay_to_tg(-200, 2, 12, None, "other chat", None, False, False, False),
            )

        with patch.object(bot, "tg_app", tg_app), \
             patch.object(bot, "_get_tg_chat_desc", lambda cid: str(cid)), \
             patch("relay.database.save_message_map"):
            asyncio.run(run())

        self.assertLess(sent.index("first"), sent.index("second"))
        # A different TG chat is not held up by the slow one
        self.assertLess(sent.index("other chat"), sent.index("first"))


if __name__ == "__main__":
    unittest.main()
