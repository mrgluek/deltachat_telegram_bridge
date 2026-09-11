import os
import sys
import tempfile
import zipfile
import asyncio
import unittest
from unittest.mock import MagicMock, patch, AsyncMock
try:
    from PIL import Image
except ImportError:
    Image = MagicMock()

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database
import bot

TEST_DB = "test_rich_posts.db"

class TestRichPosts(unittest.TestCase):
    def setUp(self):
        database.DB_PATH = TEST_DB
        database.init_db()
        bot._processed_media_groups.clear()

    def tearDown(self):
        bot._processed_media_groups.clear()
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

    def test_rich_mode_database_config(self):
        # Default should be webxdc
        self.assertEqual(database.get_rich_mode(), "webxdc")

        # Set to valid modes
        self.assertTrue(database.set_rich_mode("split"))
        self.assertEqual(database.get_rich_mode(), "split")

        self.assertTrue(database.set_rich_mode("both"))
        self.assertEqual(database.get_rich_mode(), "both")

        self.assertTrue(database.set_rich_mode("off"))
        self.assertEqual(database.get_rich_mode(), "off")

        self.assertTrue(database.set_rich_mode("webxdc"))
        self.assertEqual(database.get_rich_mode(), "webxdc")

        # Invalid mode should return False and keep existing value
        self.assertFalse(database.set_rich_mode("invalid_mode"))
        self.assertEqual(database.get_rich_mode(), "webxdc")

    def test_media_group_deduplication(self):
        group_id = 987654321
        self.assertFalse(bot._is_media_group_processed(group_id))
        # Second call should return True (already processed)
        self.assertTrue(bot._is_media_group_processed(group_id))
        # Different group should return False
        self.assertFalse(bot._is_media_group_processed(123456789))

    def test_clean_html_for_webxdc(self):
        raw_html = (
            'Hello <tg-spoiler>secret text</tg-spoiler> and '
            '<a href="https://t.me/durov">channel link</a> and '
            '<script>alert("hack")</script> '
            '<table><tr><td>Data 1</td><td>Data 2</td></tr></table>'
        )
        cleaned = bot._clean_html_for_webxdc(raw_html)

        # Spoilers converted to clickable spans
        self.assertIn('<span class="spoiler" onclick="this.classList.toggle(\'revealed\')">secret text</span>', cleaned)
        # Scripts stripped
        self.assertNotIn('<script>', cleaned)
        self.assertNotIn('alert', cleaned)
        # Links and tables preserved
        self.assertIn('<a href="https://t.me/durov" target="_blank"', cleaned)
        self.assertIn('<div class="table-wrap"><table>', cleaned)
        self.assertIn('<td>Data 1</td>', cleaned)

    def test_clean_toml_string(self):
        self.assertEqual(bot._clean_toml_string('Hello "World" \n test'), 'Hello \\"World\\"   test')
        self.assertEqual(bot._clean_toml_string(''), '')

    def test_make_teaser(self):
        long_text = "Word " * 100
        teaser = bot._make_teaser(long_text, max_len=50)
        self.assertLessEqual(len(teaser), 55)
        self.assertTrue(teaser.endswith("…"))

    def test_extract_public_tg_post_rich(self):
        sample_embed_html = '''
        <!DOCTYPE html>
        <html>
        <body>
            <div class="tgme_widget_message">
                <div class="tgme_widget_message_user_photo">
                    <img src="https://cdn4.telesco.pe/file/avatar.jpg">
                </div>
                <div class="tgme_widget_message_owner_name">
                    <span>Durov\'s Channel</span>
                </div>
                <div class="tgme_widget_message_text js-message_text">
                    Major update with <b>tables</b> and spoilers: <span class="tg-spoiler">secret</span>
                    <table><tr><th>Header</th></tr><tr><td>Row 1</td></tr></table>
                </div>
                <div class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/photo1.jpg')"></div>
                <div class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/photo2.jpg')"></div>
                <span class="tgme_widget_message_views">125.4K</span>
                <time datetime="2026-09-11T10:00:00+00:00">Sep 11, 2026</time>
            </div>
        </body>
        </html>
        '''

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = sample_embed_html

        with patch('httpx.AsyncClient.get', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            rich_post = asyncio.run(bot._extract_public_tg_post_rich("durov", 1234))

        self.assertIsNotNone(rich_post)
        self.assertEqual(rich_post.author_name, "Durov's Channel")
        self.assertEqual(rich_post.author_avatar_url, "https://cdn4.telesco.pe/file/avatar.jpg")
        self.assertEqual(len(rich_post.image_urls), 2)
        self.assertIn("https://cdn4.telesco.pe/file/photo1.jpg", rich_post.image_urls)
        self.assertIn("https://cdn4.telesco.pe/file/photo2.jpg", rich_post.image_urls)
        self.assertTrue(rich_post.is_rich)
        self.assertEqual(rich_post.views, "125.4K")
        self.assertEqual(rich_post.published_date, "2026-09-11T10:00:00+00:00")
        self.assertIn("Major update", rich_post.text_markdown)
        self.assertIn("Header", rich_post.text_html)

    def test_package_tg_post_webxdc(self):
        rich_post = bot.TelegramRichPost(
            username="durov",
            post_id=4321,
            author_name="Telegram News",
            author_avatar_url="https://example.com/avatar.jpg",
            text_html="<p>This is a rich post with a table: <table><tr><td>Cell 1</td></tr></table></p>",
            text_markdown="This is a rich post with a table.",
            teaser="This is a rich post with a table...",
            image_urls=["https://example.com/photo1.jpg", "https://example.com/photo2.jpg"],
            published_date="Sep 11, 2026",
            views="50K",
            is_rich=True,
        )

        async def fake_download(url, dest_path, max_dim=1280, **kwargs):
            # Create a tiny dummy image file
            try:
                img = Image.new('RGB', (32, 32), color='purple')
                img.save(dest_path, 'WEBP')
            except Exception:
                with open(dest_path, 'wb') as f:
                    f.write(b'fake_webp_data')
            return True

        tmp_fd, xdc_dest = tempfile.mkstemp(suffix=".xdc")
        os.close(tmp_fd)

        try:
            with patch('bot._download_image_to_file', side_effect=fake_download):
                ok = asyncio.run(bot._package_tg_post_webxdc(rich_post, xdc_dest))
            
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(xdc_dest))
            self.assertTrue(zipfile.is_zipfile(xdc_dest))

            with zipfile.ZipFile(xdc_dest, 'r') as z:
                names = z.namelist()
                self.assertIn("manifest.toml", names)
                self.assertIn("index.html", names)
                self.assertIn("icon.png", names)
                self.assertIn("images/img_0.webp", names)
                self.assertIn("images/img_1.webp", names)

                # Check manifest contents
                manifest_content = z.read("manifest.toml").decode("utf-8")
                self.assertIn('name = "Telegram News', manifest_content)

                # Check index.html contents
                html_content = z.read("index.html").decode("utf-8")
                self.assertIn("Telegram News", html_content)
                self.assertIn("Cell 1", html_content)
                self.assertIn("images/img_0.webp", html_content)
                self.assertIn("images/img_1.webp", html_content)
                self.assertIn("Post bridged at", html_content)
                self.assertIn("https://git.gluek.info/gluek/deltachat_telegram_bridge", html_content)
                self.assertIn("Delta Chat Telegram Bridge", html_content)
        finally:
            if os.path.exists(xdc_dest):
                try:
                    os.unlink(xdc_dest)
                except Exception:
                    pass

    def test_package_tg_post_webxdc_uses_local_channel_avatar(self):
        rich_post = bot.TelegramRichPost(
            username="exploitex",
            post_id=36560,
            author_name="Эксплойт",
            author_avatar_url="https://example.com/ignored_remote_avatar.jpg",
            text_html="<p>Test post</p>",
            text_markdown="Test post",
            teaser="Test post...",
            image_urls=[],
            published_date="Sep 11, 2026",
            views="10K",
            is_rich=True,
        )

        # Create dummy local avatar file (simulating Delta Chat chat profile image)
        tmp_avatar_fd, avatar_path = tempfile.mkstemp(suffix=".png")
        os.close(tmp_avatar_fd)
        try:
            img = Image.new('RGB', (64, 64), color='orange')
            img.save(avatar_path, 'PNG')
        except Exception:
            with open(avatar_path, 'wb') as f:
                f.write(b'fake_avatar_png')

        tmp_fd, xdc_dest = tempfile.mkstemp(suffix=".xdc")
        os.close(tmp_fd)

        mock_dc_bot = MagicMock()
        mock_dc_bot.rpc.get_basic_chat_info.return_value = {"profile_image": avatar_path}

        try:
            with patch('bot.dc_bot_instance', mock_dc_bot), \
                 patch('bot.dc_accid', 1), \
                 patch('bot._download_image_to_file') as mock_download:
                ok = asyncio.run(bot._package_tg_post_webxdc(rich_post, xdc_dest, dc_chat_id=555))
            
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(xdc_dest))
            # Verify remote avatar download was NOT called because local avatar was prioritized
            for call_args in mock_download.call_args_list:
                self.assertNotIn("ignored_remote_avatar.jpg", call_args[0])

            with zipfile.ZipFile(xdc_dest, 'r') as z:
                names = z.namelist()
                self.assertIn("manifest.toml", names)
                self.assertIn("icon.png", names)
                self.assertGreater(len(z.read("icon.png")), 0)
        finally:
            if os.path.exists(avatar_path):
                try: os.unlink(avatar_path)
                except: pass
            if os.path.exists(xdc_dest):
                try: os.unlink(xdc_dest)
                except: pass

    def test_richmode_command(self):
        mock_bot = MagicMock()
        mock_event = MagicMock()
        mock_event.msg.from_id = 999
        mock_event.msg.chat_id = 100

        with patch('bot._is_dc_admin') as mock_admin:
            # 1. Non-admin is rejected
            mock_admin.return_value = False
            mock_event.payload = "split"
            bot.richmode_command(mock_bot, 1, mock_event)
            mock_bot.rpc.send_msg.assert_called_once()
            self.assertIn("only the bot administrator", mock_bot.rpc.send_msg.call_args[0][2].text.lower())

            # 2. Admin querying status with empty payload
            mock_bot.rpc.send_msg.reset_mock()
            mock_admin.return_value = True
            mock_event.payload = ""
            bot.richmode_command(mock_bot, 1, mock_event)
            reply = mock_bot.rpc.send_msg.call_args[0][2].text
            self.assertIn("current telegram rich post mode", reply.lower())
            self.assertIn("webxdc", reply)

            # 3. Admin setting valid mode "split"
            mock_bot.rpc.send_msg.reset_mock()
            mock_event.payload = "split"
            bot.richmode_command(mock_bot, 1, mock_event)
            reply = mock_bot.rpc.send_msg.call_args[0][2].text
            self.assertIn("Telegram rich post relay mode set to **split**", reply)
            self.assertEqual(database.get_rich_mode(), "split")

            # 4. Admin setting invalid mode
            mock_bot.rpc.send_msg.reset_mock()
            mock_event.payload = "unknown_mode"
            bot.richmode_command(mock_bot, 1, mock_event)
            reply = mock_bot.rpc.send_msg.call_args[0][2].text
            self.assertIn("Invalid mode", reply)
            self.assertEqual(database.get_rich_mode(), "split")

            # 5. Reset back to webxdc
            mock_bot.rpc.send_msg.reset_mock()
            mock_event.payload = "webxdc"
            bot.richmode_command(mock_bot, 1, mock_event)
            self.assertEqual(database.get_rich_mode(), "webxdc")

if __name__ == '__main__':
    unittest.main()
