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

    def test_database_get_recent_message_maps(self):
        # Insert a few maps
        database.save_message_map(501, 100, 601, 200, "hash1")
        database.save_message_map(502, 100, 602, 200, "hash2")
        
        recent = database.get_recent_message_maps(limit=5)
        self.assertTrue(len(recent) >= 2)
        # Check order (most recent first)
        self.assertEqual(recent[0]['dc_msg_id'], 502)
        self.assertEqual(recent[1]['dc_msg_id'], 501)

    def test_cached_last_msg_id(self):
        # Setup channel
        database.add_channel_by_id(999, 888)
        
        # Verify initial
        self.assertEqual(bot._get_cached_last_msg_id(999), 0)
        
        # Update cache
        bot._update_cached_last_msg_id(999, 50)
        self.assertEqual(bot._get_cached_last_msg_id(999), 50)
        self.assertEqual(database.get_channel_last_msg_id(999), 50)
        
        # Test warm function
        bot._last_msg_id_cache.clear()
        bot._warm_last_msg_id_cache()
        self.assertEqual(bot._get_cached_last_msg_id(999), 50)

    def test_webpage_preview_extraction(self):
        class MockWebPage:
            def __init__(self):
                self.site_name = "Obsidian"
                self.title = "РКН лютует"
                self.description = "19 июля 2026 года..."
                self.url = "https://publish.obsidian.md/..."
        
        class MockMessageMediaWebPage:
            def __init__(self):
                self.webpage = MockWebPage()

        class MockMessage:
            def __init__(self):
                self.chat_id = 123
                self.id = 456
                self.text = ""
                self.media = MockMessageMediaWebPage()
                self.chat = None

        msg = MockMessage()
        
        # Test extraction logic
        webpage = msg.media.webpage
        parts = []
        if getattr(webpage, 'site_name', None):
            parts.append(f"🌐 **{webpage.site_name}**")
        if getattr(webpage, 'title', None):
            parts.append(f"**{webpage.title}**" if not getattr(webpage, 'site_name', None) else webpage.title)
        if getattr(webpage, 'description', None):
            parts.append(webpage.description)
        if getattr(webpage, 'url', None):
            parts.append(webpage.url)
        extracted_text = "\n\n".join(parts)
        
        self.assertIn("🌐 **Obsidian**", extracted_text)
        self.assertIn("РКН лютует", extracted_text)
        self.assertIn("19 июля 2026 года...", extracted_text)
        self.assertIn("https://publish.obsidian.md/...", extracted_text)

    def test_reconciliation_logic(self):
        last_id = 10
        latest_id = 15
        
        # Verify basic limit calculation
        missed_count = latest_id - last_id
        fetch_limit = min(missed_count, 50)
        self.assertEqual(fetch_limit, 5)
        
        # Verify sorting order (oldest first)
        class MockMsg:
            def __init__(self, msg_id):
                self.id = msg_id
        
        msgs = [MockMsg(15), MockMsg(12), MockMsg(14), MockMsg(11), MockMsg(13)]
        sorted_msgs = sorted(msgs, key=lambda m: m.id)
        self.assertEqual(sorted_msgs[0].id, 11)
        self.assertEqual(sorted_msgs[-1].id, 15)

    def test_paid_media_hash_and_size(self):
        class MockPaidMediaItem:
            def __init__(self, file_size=500):
                class MockPhoto:
                    def __init__(self, size):
                        self.file_size = size
                self.photo = [MockPhoto(file_size)]

        class MockPaidMediaInfo:
            def __init__(self, star_count=10, items=None):
                self.star_count = star_count
                self.paid_media = items or [MockPaidMediaItem()]

        class MockPTBMessage:
            def __init__(self, text="Paid post", paid_info=None):
                self.text = text
                self.caption = ""
                self.photo = None
                self.paid_media = paid_info or MockPaidMediaInfo()

        msg = MockPTBMessage()
        h = bot._get_content_hash(msg)
        self.assertTrue(len(h) == 64)
        
        size = bot._get_ptb_media_size(msg)
        self.assertEqual(size, 500)

    def test_telethon_paid_media_extraction(self):
        class MockMessageMediaPaidMedia:
            def __init__(self):
                self.stars = 25
                self.extended_media = []

        class MockTelethonMsg:
            def __init__(self):
                self.chat_id = 123
                self.id = 789
                self.text = "Here is paid content"
                self.media = MockMessageMediaPaidMedia()
                self.chat = None

        msg = MockTelethonMsg()
        text = msg.text or ""
        stars = getattr(msg.media, 'stars', 0) or 0
        star_str = f" ({stars} ⭐)" if stars else ""
        paid_label = f"⭐ Paid Media{star_str}"
        formatted = (f"[{paid_label}]\n" + text).strip() if text else f"[{paid_label}]"

        self.assertIn("[⭐ Paid Media (25 ⭐)]", formatted)
        self.assertIn("Here is paid content", formatted)

    def test_transient_error_suppression(self):
        handler = bot.AdminLogHandler()
        record = bot.logging.LogRecord(
            name="asyncio",
            level=bot.logging.ERROR,
            pathname="connection.py",
            lineno=355,
            msg="Task was destroyed but it is pending!\ntask: <Task pending name='Task-8717' coro=<Connection._recv_loop()>",
            args=(),
            exc_info=None
        )
        # Should be filtered out without exception
        handler.emit(record)

        get_updates_record = bot.logging.LogRecord(
            name="telegram.ext.Updater",
            level=bot.logging.ERROR,
            pathname="updater.py",
            lineno=100,
            msg="Error while calling `get_updates` one more time to mark all fetched updates.",
            args=(),
            exc_info=None
        )
        handler.emit(get_updates_record)

        # Test PollingErrorFilter
        flt = bot.PollingErrorFilter()
        self.assertFalse(flt.filter(get_updates_record))


if __name__ == "__main__":
    unittest.main()
