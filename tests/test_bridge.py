import os
import sys
import unittest
import time
from unittest.mock import MagicMock

# Setup test database path for isolated testing
TEST_DB = "test_bridge.db"
os.environ["DB_PATH"] = TEST_DB

# Mock native Delta Chat libraries to prevent C-library loading failures on CI
try:
    import deltachat2
except ImportError:
    mock_deltachat2 = MagicMock()
    class MsgData:
        def __init__(self, text="", file="", override_sender_name=None):
            self.text = text
            self.file = file
            self.override_sender_name = override_sender_name
    mock_deltachat2.MsgData = MsgData
    sys.modules['deltachat2'] = mock_deltachat2

try:
    import deltabot_cli
except ImportError:
    class MockBotCli:
        def __init__(self, *args, **kwargs):
            pass
        def on(self, *args, **kwargs):
            return lambda func: func
        def on_init(self, func):
            return func
        def on_start(self, func):
            return func
        def start(self):
            pass
    mock_deltabot_cli = MagicMock()
    mock_deltabot_cli.BotCli = MockBotCli
    sys.modules['deltabot_cli'] = mock_deltabot_cli

# Add parent directory to sys.path to import database and bot modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import bot


class MockEntity:
    """Mock entity to test inline links."""
    def __init__(self, type_str, offset, length, url=None):
        self.type = type_str
        self.offset = offset
        self.length = length
        self.url = url


class TestTelegramBridge(unittest.TestCase):

    def setUp(self):
        # Enforce isolated test DB path and initialize schema
        database.DB_PATH = TEST_DB
        database.init_db()

    def tearDown(self):
        # Clean up database files
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except OSError:
                pass
        for suffix in ["-wal", "-shm"]:
            fpath = TEST_DB + suffix
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

    def test_truncate(self):
        self.assertEqual(bot._truncate("hello", 10), "hello")
        self.assertEqual(bot._truncate("hello world", 10), "hello wor…")
        self.assertEqual(bot._truncate("ab", 1), "…")

    def test_inline_links(self):
        # Plain text without links
        self.assertEqual(bot._inline_links("plain text", []), "plain text")

        # Text with a hidden link
        entities = [MockEntity("text_link", 0, 5, "https://example.com")]
        self.assertEqual(
            bot._inline_links("click here", entities),
            "click ( https://example.com ) here"
        )

        # Skip links already present in text
        entities_dup = [MockEntity("text_link", 0, 19, "https://example.com")]
        self.assertEqual(
            bot._inline_links("https://example.com", entities_dup),
            "https://example.com"
        )

    def test_dc_fallback_pattern(self):
        self.assertTrue(bool(bot.DC_FALLBACK_PATTERN.match("[Document - file.pdf]")))
        self.assertTrue(bool(bot.DC_FALLBACK_PATTERN.match("  [Voice - recording.ogg]  ")))
        self.assertTrue(bool(bot.DC_FALLBACK_PATTERN.match("[Image - photo.jpg]")))
        self.assertFalse(bool(bot.DC_FALLBACK_PATTERN.match("regular message with [brackets]")))

    def test_rate_limits(self):
        bot._rate_limits.clear()
        # Initial check should pass
        self.assertFalse(bot._is_rate_limited(12345))

        # Add timestamps to trigger rate limit (max 30 messages per 60s per chat)
        now = time.time()
        bot._rate_limits[12345] = [now] * 31
        self.assertTrue(bot._is_rate_limited(12345))

    def test_deletion_rate_limits(self):
        bot._deletion_sync_times.clear()
        self.assertFalse(bot._is_deletion_rate_limited())

        # Trigger deletion rate limit (max 10 deletions per 60s)
        now = time.time()
        bot._deletion_sync_times.extend([now] * 11)
        self.assertTrue(bot._is_deletion_rate_limited())

    def test_database_config(self):
        database.set_config("api_id", "98765")
        self.assertEqual(database.get_config("api_id"), "98765")

        database.set_config("nonexistent", "")
        self.assertEqual(database.get_config("nonexistent"), "")

    def test_database_bridges(self):
        database.add_bridge(100, 200)
        self.assertEqual(database.get_tg_chats(100)[0], 200)
        self.assertEqual(database.get_dc_chats(200), [100])
        self.assertTrue(database.count_bridges_for_tg(200) > 0)

        database.remove_bridge(100)
        self.assertNotIn(100, database.get_dc_chats(200))

    def test_database_message_map(self):
        database.save_message_map(500, 100, 600, 200, "abc-hash")
        self.assertEqual(database.get_dc_msg_id(600, 200, 100), 500)
        self.assertEqual(database.get_message_content_hash(600, 200, 100), "abc-hash")

    def test_database_channel_last_msg_id(self):
        # Add channel and verify initial last_msg_id is 0
        database.add_channel_by_id(777, 888)
        self.assertEqual(database.get_channel_last_msg_id(777), 0)

        # Update last_msg_id and verify the change is saved
        database.update_channel_last_msg_id(777, 42)
        self.assertEqual(database.get_channel_last_msg_id(777), 42)


if __name__ == "__main__":
    unittest.main()
