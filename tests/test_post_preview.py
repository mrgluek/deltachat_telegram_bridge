import os
import sys
import tempfile
import asyncio
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database
import bot

TEST_DB = "test_post_preview.db"


class TestTelegramPostPreview(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = TEST_DB
        database.init_db()

    def tearDown(self):
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

    def test_post_cache_roundtrip(self):
        """Test adding and retrieving cached posts with case insensitivity."""
        database.add_cached_tg_post("channel/123", "text", "Hello World")
        cached = database.get_cached_tg_post("Channel/123")
        self.assertIsNotNone(cached)
        self.assertEqual(cached["post_type"], "text")
        self.assertEqual(cached["text"], "Hello World")
        self.assertIsNone(cached["file_path"])

    def test_post_cache_file_existence_check(self):
        """If cached file does not exist on disk, get_cached_tg_post returns None."""
        database.add_cached_tg_post("channel/456", "webxdc", "Teaser", "/tmp/non_existent_file_987654.xdc")
        self.assertIsNone(database.get_cached_tg_post("channel/456"))

        # With existing file
        with tempfile.NamedTemporaryFile(suffix=".xdc", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            database.add_cached_tg_post("channel/456", "webxdc", "Teaser", tmp_path)
            cached = database.get_cached_tg_post("channel/456")
            self.assertIsNotNone(cached)
            self.assertEqual(cached["file_path"], tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_post_cache_cleanup(self):
        """Test clearing expired cache entries."""
        with tempfile.NamedTemporaryFile(suffix=".xdc", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            database.add_cached_tg_post("channel/old", "webxdc", "Old", tmp_path)
            # Max age 0 will consider all entries expired
            database.clear_expired_tg_post_cache(max_age_seconds=0)
            self.assertIsNone(database.get_cached_tg_post("channel/old"))
            self.assertFalse(os.path.exists(tmp_path))
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_tg_post_url_regex(self):
        """Test URL pattern matching for direct Telegram post links."""
        m = bot.TG_POST_URL_RE.search("https://t.me/durov/123")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "durov")
        self.assertEqual(m.group(2), "123")

        m = bot.TG_POST_URL_RE.search("https://t.me/s/my_channel/456?single")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "my_channel")
        self.assertEqual(m.group(2), "456")

        m = bot.TG_POST_URL_RE.search("Look here: telegram.me/channel/789 comments")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "channel")
        self.assertEqual(m.group(2), "789")

        # Invalid
        self.assertIsNone(bot.TG_POST_URL_RE.search("https://t.me/durov"))
        self.assertIsNone(bot.TG_POST_URL_RE.search("https://example.com/durov/123"))

    def test_async_handle_direct_tg_post_cache_hit(self):
        """When post is in cache, deliver immediately without network requests."""
        with tempfile.NamedTemporaryFile(suffix=".xdc", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            database.add_cached_tg_post("testchannel/100", "webxdc", "Cached Teaser", tmp_path)
            mock_bot = MagicMock()

            with patch("bot._dc_send_msg_with_stats") as mock_send:
                asyncio.run(bot._async_handle_direct_tg_post(mock_bot, 1, 10, "testchannel", 100))
                mock_send.assert_called_once()
                args = mock_send.call_args[0]
                self.assertEqual(args[2], 10)  # dc_chat_id
                self.assertEqual(args[3].file, tmp_path)
                self.assertEqual(args[3].text, "Cached Teaser")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_async_handle_direct_tg_post_webxdc(self):
        """Rich posts are packaged into WebXDC applications."""
        mock_rich_post = bot.TelegramRichPost(
            username="testchan",
            post_id=200,
            author_name="Test Channel",
            author_avatar_url="",
            text_html="<b>Rich post</b>",
            text_markdown="**Rich post**",
            teaser="Rich post teaser",
            image_urls=["https://t.me/img1.jpg", "https://t.me/img2.jpg"],
            video_urls=[],
            videos=[],
            album_post_ids=[200],
            published_date="Sep 18",
            views="1.2k",
            is_rich=True
        )

        created_files = []
        async def mock_package_side_effect(post, path, dc_chat_id=None):
            with open(path, "w") as f:
                f.write("dummy xdc")
            created_files.append(path)
            return True

        mock_bot = MagicMock()
        with (
            patch("bot._extract_public_tg_post_rich", new_callable=AsyncMock) as mock_extract,
            patch("bot._package_tg_post_webxdc", side_effect=mock_package_side_effect) as mock_package,
            patch("bot._dc_send_msg_with_stats") as mock_send,
        ):
            mock_extract.return_value = mock_rich_post

            try:
                asyncio.run(bot._async_handle_direct_tg_post(mock_bot, 1, 10, "testchan", 200))

                mock_package.assert_called_once()
                mock_send.assert_called_once()
                msg_data = mock_send.call_args[0][3]
                self.assertTrue(msg_data.file.endswith(".xdc"))
                self.assertIn("Test Channel", msg_data.text)
                self.assertIn("t.me/testchan/200", msg_data.text)

                # Check cached
                cached = database.get_cached_tg_post("testchan/200")
                self.assertIsNotNone(cached)
                self.assertEqual(cached["post_type"], "webxdc")
            finally:
                for f in created_files:
                    if os.path.exists(f):
                        try:
                            os.remove(f)
                        except Exception:
                            pass

    def test_async_handle_direct_tg_post_photo(self):
        """Single photo posts are delivered as photo messages with caption."""
        mock_post = bot.TelegramRichPost(
            username="photochan",
            post_id=300,
            author_name="Photo Channel",
            author_avatar_url="",
            text_html="Beautiful view",
            text_markdown="Beautiful view",
            teaser="",
            image_urls=["https://t.me/single_photo.jpg"],
            video_urls=[],
            videos=[],
            album_post_ids=[300],
            published_date="Sep 18",
            views="500",
            is_rich=False
        )

        created_files = []
        async def mock_dl_side_effect(url, dest, max_dim=1280, fmt="JPEG", quality=85):
            with open(dest, "w") as f:
                f.write("dummy image")
            created_files.append(dest)
            return True

        mock_bot = MagicMock()
        with (
            patch("bot._extract_public_tg_post_rich", new_callable=AsyncMock) as mock_extract,
            patch("bot._download_image_to_file", side_effect=mock_dl_side_effect) as mock_dl,
            patch("bot._dc_send_msg_with_stats") as mock_send,
        ):
            mock_extract.return_value = mock_post

            try:
                asyncio.run(bot._async_handle_direct_tg_post(mock_bot, 1, 10, "photochan", 300))

                mock_send.assert_called_once()
                msg_data = mock_send.call_args[0][3]
                self.assertTrue(msg_data.file.endswith(".jpg"))
                self.assertIn("Beautiful view", msg_data.text)

                cached = database.get_cached_tg_post("photochan/300")
                self.assertIsNotNone(cached)
                self.assertEqual(cached["post_type"], "photo")
            finally:
                for f in created_files:
                    if os.path.exists(f):
                        try:
                            os.remove(f)
                        except Exception:
                            pass

    def test_async_handle_direct_tg_post_text_only(self):
        """Plain text posts are delivered as text messages."""
        mock_post = bot.TelegramRichPost(
            username="textchan",
            post_id=400,
            author_name="Text Channel",
            author_avatar_url="",
            text_html="Just some simple text",
            text_markdown="Just some simple text",
            teaser="",
            image_urls=[],
            video_urls=[],
            videos=[],
            album_post_ids=[400],
            published_date="Sep 18",
            views="100",
            is_rich=False
        )

        mock_bot = MagicMock()
        with (
            patch("bot._extract_public_tg_post_rich", new_callable=AsyncMock) as mock_extract,
            patch("bot._dc_send_msg_with_stats") as mock_send,
        ):
            mock_extract.return_value = mock_post

            asyncio.run(bot._async_handle_direct_tg_post(mock_bot, 1, 10, "textchan", 400))

            mock_send.assert_called_once()
            msg_data = mock_send.call_args[0][3]
            self.assertIsNone(msg_data.file)
            self.assertIn("Just some simple text", msg_data.text)

            cached = database.get_cached_tg_post("textchan/400")
            self.assertIsNotNone(cached)
            self.assertEqual(cached["post_type"], "text")

    def test_handle_dc_message_dispatches_direct_post(self):
        """handle_dc_message intercepts t.me/channel/123 and triggers _async_handle_direct_tg_post."""
        mock_bot = MagicMock()
        mock_bot.has_command.return_value = False
        mock_bot.rpc.get_basic_chat_info.return_value = {"type": 1}

        mock_event = MagicMock()
        mock_event.command = None
        mock_event.msg.chat_id = 10
        mock_event.msg.from_id = 99
        mock_event.msg.id = 55
        mock_event.msg.text = "Look at this https://t.me/durov/42"

        with (
            patch("bot._async_handle_direct_tg_post", new_callable=AsyncMock) as mock_handler,
            patch("bot._is_dc_admin", return_value=False),
        ):
            bot.handle_dc_message(mock_bot, 1, mock_event)
            mock_handler.assert_called_once_with(mock_bot, 1, 10, "durov", 42, 55)


if __name__ == "__main__":
    unittest.main()
