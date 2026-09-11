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
        # Second call should return True (already processed in memory)
        self.assertTrue(bot._is_media_group_processed(group_id))
        # Different group should return False
        self.assertFalse(bot._is_media_group_processed(123456789))

        # Test persistent DB survival across memory cache wipe
        bot._processed_media_groups.clear()
        database.mark_media_group_processed(group_id, -10012345, 777)
        # Even after clearing memory cache, DB record must identify it as processed
        self.assertTrue(bot._is_media_group_processed(group_id))

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
                <div class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/photo1.jpg')">
                    <a href="https://t.me/durov/1233?single">Photo 1</a>
                </div>
                <div class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn4.telesco.pe/file/photo2.jpg')">
                    <a href="https://t.me/durov/1234?single">Photo 2</a>
                </div>
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
        self.assertEqual(rich_post.album_post_ids, [1233, 1234])
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

                # Check manifest contents (channel_title #post_id format)
                manifest_content = z.read("manifest.toml").decode("utf-8")
                self.assertIn('name = "Telegram News #4321"', manifest_content)

                # Check index.html contents
                html_content = z.read("index.html").decode("utf-8")
                self.assertIn("<title>Telegram News #4321</title>", html_content)
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

    def test_extract_public_tg_post_rich_with_videos(self):
        sample_embed_html = '''
        <!DOCTYPE html>
        <html>
        <body>
            <div class="tgme_widget_message">
                <div class="tgme_widget_message_owner_name"><span>Chtddd Channel</span></div>
                <div class="tgme_widget_message_text js-message_text">Post with two videos</div>
                <!-- Video 0: direct stream -->
                <a class="tgme_widget_message_video_player js-message_video_player" href="https://t.me/chtddd/96722">
                    <i class="tgme_widget_message_video_thumb" style="background-image:url('https://cdn4.telesco.pe/file/poster0.jpg')"></i>
                    <video src="https://cdn4.telesco.pe/file/stream0.mp4?token=abc" class="tgme_widget_message_video"></video>
                    <time class="message_video_duration">0:35</time>
                </a>
                <!-- Video 1: too big / unsupported stream -->
                <a class="tgme_widget_message_video_player js-message_video_player" href="https://t.me/chtddd/96723">
                    <i class="tgme_widget_message_video_thumb" style="background-image:url('https://cdn4.telesco.pe/file/poster1.jpg')"></i>
                    <time class="message_video_duration">1:38</time>
                    <div class="message_media_not_supported_label">Media is too big</div>
                </a>
                <!-- Extra photo -->
                <div style="background-image:url('https://cdn4.telesco.pe/file/photo1.jpg')"></div>
            </div>
        </body>
        </html>
        '''

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = sample_embed_html

        with patch('httpx.AsyncClient.get', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            rich_post = asyncio.run(bot._extract_public_tg_post_rich("chtddd", 96722))

        self.assertIsNotNone(rich_post)
        self.assertEqual(rich_post.author_name, "Chtddd Channel")
        self.assertEqual(len(rich_post.videos), 2)
        
        # Video 0 checks
        v0 = rich_post.videos[0]
        self.assertEqual(v0.video_url, "https://cdn4.telesco.pe/file/stream0.mp4?token=abc")
        self.assertEqual(v0.poster_url, "https://cdn4.telesco.pe/file/poster0.jpg")
        self.assertEqual(v0.duration, "0:35")
        self.assertFalse(v0.is_too_big)

        # Video 1 checks (Media is too big)
        v1 = rich_post.videos[1]
        self.assertEqual(v1.video_url, "")
        self.assertEqual(v1.poster_url, "https://cdn4.telesco.pe/file/poster1.jpg")
        self.assertEqual(v1.duration, "1:38")
        self.assertTrue(v1.is_too_big)

        # Posters should not leak into photo gallery
        self.assertEqual(rich_post.image_urls, ["https://cdn4.telesco.pe/file/photo1.jpg"])
        self.assertTrue(rich_post.is_rich)

    def test_package_tg_post_webxdc_with_videos_and_budgeting(self):
        rich_post = bot.TelegramRichPost(
            username="chtddd",
            post_id=96722,
            author_name="Chtddd News",
            author_avatar_url="",
            text_html="<p>Test with video limits</p>",
            text_markdown="Test with video limits",
            teaser="Test with video limits",
            image_urls=[],
            videos=[
                # Vid 0: 10 MB (within 20 MB single and 50 MB total -> embedded)
                bot.TelegramRichVideo(video_url="https://example.com/v0.mp4", poster_url="https://example.com/p0.jpg", duration="0:35", is_too_big=False),
                # Vid 1: 15 MB (within 20 MB single, total 25 MB <= 50 MB -> embedded)
                bot.TelegramRichVideo(video_url="https://example.com/v1.mp4", poster_url="https://example.com/p1.jpg", duration="0:45", is_too_big=False),
                # Vid 2: 30 MB (exceeds 20 MB single cap -> overflow card)
                bot.TelegramRichVideo(video_url="https://example.com/v2.mp4", poster_url="https://example.com/p2.jpg", duration="2:10", is_too_big=False),
                # Vid 3: Media is too big by Telegram -> overflow card
                bot.TelegramRichVideo(video_url="", poster_url="https://example.com/p3.jpg", duration="1:38", is_too_big=True),
            ],
            is_rich=True,
        )

        simulated_sizes = {
            "https://example.com/v0.mp4": 10 * 1024 * 1024,
            "https://example.com/v1.mp4": 15 * 1024 * 1024,
            "https://example.com/v2.mp4": 30 * 1024 * 1024,
        }

        async def fake_download_video(url, dest_path, max_bytes):
            size = simulated_sizes.get(url, 0)
            if size > max_bytes:
                return False
            with open(dest_path, 'wb') as f:
                f.write(b'0' * 1024)  # write dummy payload
            # simulate file size on disk
            with patch('os.path.getsize', return_value=size):
                pass
            return True

        async def fake_download_image(url, dest_path, **kwargs):
            with open(dest_path, 'wb') as f:
                f.write(b'fake_img')
            return True

        tmp_fd, xdc_dest = tempfile.mkstemp(suffix=".xdc")
        os.close(tmp_fd)

        try:
            # We mock getsize so that os.path.getsize(vid_dest) returns simulated_sizes
            orig_getsize = os.path.getsize
            def custom_getsize(path):
                for vid_idx, (u, s) in enumerate(simulated_sizes.items()):
                    if f"vid_{vid_idx}.mp4" in path:
                        return s
                return orig_getsize(path)

            with patch('bot._download_video_with_limit', side_effect=fake_download_video), \
                 patch('bot._download_image_to_file', side_effect=fake_download_image), \
                 patch('os.path.getsize', side_effect=custom_getsize):
                ok = asyncio.run(bot._package_tg_post_webxdc(rich_post, xdc_dest))

            self.assertTrue(ok)
            self.assertTrue(os.path.exists(xdc_dest))
            self.assertTrue(zipfile.is_zipfile(xdc_dest))

            with zipfile.ZipFile(xdc_dest, 'r') as z:
                names = z.namelist()
                # Vid 0 and Vid 1 embedded
                self.assertIn("videos/vid_0.mp4", names)
                self.assertIn("videos/vid_1.mp4", names)
                # Vid 2 (oversize) and Vid 3 (is_too_big) NOT in videos/
                self.assertNotIn("videos/vid_2.mp4", names)
                self.assertNotIn("videos/vid_3.mp4", names)

                # All video posters included
                self.assertIn("images/vid_poster_0.webp", names)
                self.assertIn("images/vid_poster_1.webp", names)
                self.assertIn("images/vid_poster_2.webp", names)
                self.assertIn("images/vid_poster_3.webp", names)

                html_doc = z.read("index.html").decode("utf-8")
                # Embedded videos have <video controls> and <source src="videos/vid_..."
                self.assertIn('<video controls', html_doc)
                self.assertIn('videos/vid_0.mp4', html_doc)
                self.assertIn('videos/vid_1.mp4', html_doc)

                # Overflow videos have preview card and Telegram link button
                self.assertIn('video-overflow-card', html_doc)
                self.assertIn('Смотреть все видео в Telegram ↗', html_doc)
                self.assertIn('https://t.me/chtddd/96722', html_doc)
        finally:
            if os.path.exists(xdc_dest):
                try: os.unlink(xdc_dest)
                except: pass

    def test_download_video_with_limit(self):
        # 1. Reject if Content-Length header is larger than max_bytes
        mock_head = MagicMock()
        mock_head.status_code = 200
        mock_head.headers = {"content-length": "25000000"}  # 25 MB

        with patch('httpx.AsyncClient.head', new_callable=AsyncMock) as mock_head_call:
            mock_head_call.return_value = mock_head
            tmp_fd, tmp_file = tempfile.mkstemp(suffix=".mp4")
            os.close(tmp_fd)
            try:
                ok = asyncio.run(bot._download_video_with_limit("https://example.com/large.mp4", tmp_file, max_bytes=20 * 1024 * 1024))
                self.assertFalse(ok)
            finally:
                if os.path.exists(tmp_file):
                    try: os.unlink(tmp_file)
                    except: pass

        # 2. Reject and clean up if streamed chunks exceed max_bytes
        class FakeStreamResponse:
            status_code = 200
            headers = {}

            async def aiter_bytes(self, chunk_size=65536):
                yield b'x' * (5 * 1024 * 1024)
                yield b'y' * (5 * 1024 * 1024)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                pass

        with patch('httpx.AsyncClient.head', new_callable=AsyncMock) as mock_head_call, \
             patch('httpx.AsyncClient.stream', return_value=FakeStreamResponse()):
            mock_head_call.side_effect = Exception("HEAD failed")
            tmp_fd, tmp_file = tempfile.mkstemp(suffix=".mp4")
            os.close(tmp_fd)
            try:
                # Limit is 6 MB, stream produces 10 MB
                ok = asyncio.run(bot._download_video_with_limit("https://example.com/stream.mp4", tmp_file, max_bytes=6 * 1024 * 1024))
                self.assertFalse(ok)
                self.assertFalse(os.path.exists(tmp_file))
            finally:
                if os.path.exists(tmp_file):
                    try: os.unlink(tmp_file)
                    except: pass

    def test_userbot_relay_album_maps_all_post_ids(self):
        tg_channel_id = -100888999
        dc_chat_id = 100
        database.add_channel_by_id(tg_channel_id, dc_chat_id, username="phototravel")

        mock_msg = MagicMock()
        mock_msg.id = 8888
        mock_msg.chat_id = tg_channel_id
        mock_msg.chat.username = "phototravel"
        mock_msg.grouped_id = 99887766
        mock_msg.media = MagicMock()
        mock_msg.raw_text = "Beautiful mountains album"
        mock_msg.is_channel = True
        mock_msg.is_group = False

        rich_post = bot.TelegramRichPost(
            username="phototravel",
            post_id=8888,
            author_name="Фото и путешествия",
            teaser="Красивые горы...",
            image_urls=["https://example.com/p1.jpg", "https://example.com/p2.jpg"],
            album_post_ids=[8887, 8888],
            is_rich=True,
        )

        mock_dc_bot = MagicMock()
        mock_dc_bot.rpc.send_msg.return_value = 54321
        mock_userbot = MagicMock()
        mock_userbot.is_connected.return_value = True

        with patch('bot.dc_bot_instance', mock_dc_bot), \
             patch('bot.dc_accid', 1), \
             patch('bot.userbot_client', mock_userbot), \
             patch('bot._extract_public_tg_post_rich', new_callable=AsyncMock) as mock_extract, \
             patch('bot._package_tg_post_webxdc', new_callable=AsyncMock) as mock_package:
            
            mock_extract.return_value = rich_post
            mock_package.return_value = True

            asyncio.run(bot._relay_userbot_message(dc_chat_id, mock_msg))

        # 1. Verify DC send was called
        mock_dc_bot.rpc.send_msg.assert_called_once()
        sent_msg_data = mock_dc_bot.rpc.send_msg.call_args[0][2]
        self.assertTrue(sent_msg_data.file.endswith(".xdc"))

        # 2. Verify BOTH post 8888 and post 8887 are mapped to 54321
        self.assertEqual(database.get_dc_msg_id(8888, tg_channel_id, dc_chat_id), 54321)
        self.assertEqual(database.get_dc_msg_id(8887, tg_channel_id, dc_chat_id), 54321)

        # 3. Verify media group is marked processed persistently in database
        self.assertTrue(database.is_media_group_processed(99887766))

        # 4. Verify watermark last_msg_id is max(8887, 8888) = 8888
        self.assertEqual(database.get_channel_last_msg_id(tg_channel_id), 8888)

        # 5. Verify subsequent edit or second message for the same album is skipped
        mock_msg_8887 = MagicMock()
        mock_msg_8887.id = 8887
        mock_msg_8887.chat_id = tg_channel_id
        mock_msg_8887.chat.username = "phototravel"
        mock_msg_8887.grouped_id = 99887766

        mock_dc_bot.rpc.send_msg.reset_mock()
        with patch('bot.dc_bot_instance', mock_dc_bot), \
             patch('bot.dc_accid', 1), \
             patch('bot.userbot_client', mock_userbot):
            asyncio.run(bot._relay_userbot_message(dc_chat_id, mock_msg_8887, is_edit=True))

        mock_dc_bot.rpc.send_msg.assert_not_called()

if __name__ == '__main__':
    unittest.main()
