"""Regression tests for audit findings 18, 20 and 21."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

for dependency in ("deltachat2", "deltabot_cli"):
    try:
        __import__(dependency)
    except ImportError:
        sys.modules[dependency] = MagicMock()

import bot
import caching
import database
import dc_commands
import dc_events
import live_locations
import media
import relay
import security
import userbot


def block(kind, **kwargs):
    return type(kind, (), kwargs)()


class DbTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.orig_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp.name, "test.db")
        database.init_db()
        self.addCleanup(self.restore_db)
        security._processed_tg_msgs.clear()
        security._edit_timestamps.clear()
        live_locations.LIVE_LOCATIONS.clear()
        getattr(live_locations, "_expirations", {}).clear()
        self.addCleanup(security._processed_tg_msgs.clear)
        self.addCleanup(security._edit_timestamps.clear)
        self.addCleanup(live_locations.LIVE_LOCATIONS.clear)
        self.addCleanup(getattr(live_locations, "_expirations", {}).clear)

    def restore_db(self):
        database.close_db()
        database.DB_PATH = self.orig_db

    def message(self, text="original", photo_id=None):
        return NS(id=50, chat_id=-1001, sender_id=2, out=False,
                  date=datetime.now(timezone.utc), text=text, message=text,
                  raw_text=text, chat=NS(username="channel", title="Channel"),
                  photo=NS(id=photo_id) if photo_id else None,
                  media=None, is_channel=True, is_group=False,
                  entities=[], grouped_id=None, rich_message=None)


class GroupDTests(DbTest):
    def process_events(self, events):
        client = MagicMock()
        relayed = []

        async def send(chat_id, msg, is_edit=False):
            relayed.append((msg.text, is_edit))
            database.save_message_map(500, chat_id, msg.id, msg.chat_id,
                                      content_hash=caching._get_content_hash(msg))

        async def run():
            for text, edit, photo_id in events:
                await userbot._process_userbot_event_internal(
                    NS(message=self.message(text, photo_id)), is_edit=edit)

        with patch.object(bot, "userbot_client", client), \
             patch.object(bot, "dc_bot_instance", MagicMock()), \
             patch.object(bot, "dc_accid", 1), \
             patch.object(userbot, "_get_cached_dc_channel_chat_id", return_value=10), \
             patch.object(userbot, "_get_cached_last_msg_id", return_value=0), \
             patch.object(bot, "_relay_userbot_message", send):
            asyncio.run(run())
        return relayed

    def test_new_then_edit(self):
        self.assertEqual(len(self.process_events([
            ("original", False, None), ("corrected", True, None)])), 2)

    def test_identical_edits_deduplicated(self):
        self.assertEqual(len(self.process_events([
            ("corrected", True, None), ("corrected", True, None)])), 1)

    def test_distinct_edits_processed(self):
        self.assertEqual(len(self.process_events([
            ("first", True, None), ("second", True, None)])), 2)

    def test_media_only_edit_processed(self):
        self.assertEqual(len(self.process_events([
            ("caption", True, 101), ("caption", True, 102)])), 2)

    def test_new_message_dedup_unchanged(self):
        self.assertEqual(len(self.process_events([
            ("original", False, None), ("corrected", False, None)])), 1)

    def test_locations_do_not_collide_and_clear_is_scoped(self):
        live_locations.set_live_location(-1001, 50, 1, 2)
        live_locations.set_live_location(-1002, 50, 3, 4)
        self.assertEqual(live_locations.get_live_location(-1001, 50), (1, 2))
        self.assertEqual(live_locations.get_live_location(-1002, 50), (3, 4))
        live_locations.clear_live_location(-1001, 50)
        self.assertIsNone(live_locations.get_live_location(-1001, 50))
        self.assertEqual(live_locations.get_live_location(-1002, 50), (3, 4))

    def test_locupdate_only_reads_bridged_chat(self):
        database.add_bridge(10, -1001)
        database.add_bridge(20, -1002)
        database.save_message_map(500, 10, 50, -1001)
        database.save_message_map(600, 20, 50, -1002)
        live_locations.set_live_location(-1001, 50, 1, 2)
        live_locations.set_live_location(-1002, 50, 30, 40)
        event = NS(msg=NS(chat_id=10, id=501, quote={"message_id": 500}))
        with patch.object(bot, "_dc_send_msg_with_stats") as send:
            dc_commands.locupdate_command(MagicMock(), 1, event)
            self.assertIn("q=1,2", send.call_args.args[3].text)
            self.assertNotIn("30,40", send.call_args.args[3].text)
            live_locations.clear_live_location(-1001, 50)
            dc_commands.locupdate_command(MagicMock(), 1, event)
            self.assertIn("No active live location", send.call_args.args[3].text)

    def test_locations_expire_on_write_and_read(self):
        with patch.object(live_locations.time, "time", return_value=1000):
            live_locations.set_live_location(-1, 1, 1, 2, live_period=60)
            live_locations.set_live_location(-2, 1, 3, 4, live_period=999999)
        with patch.object(live_locations.time, "time", return_value=1061):
            live_locations.set_live_location(-3, 1, 5, 6)
            self.assertNotIn((-1, 1), live_locations.LIVE_LOCATIONS)
            self.assertEqual(live_locations.get_live_location(-2, 1), (3, 4))
        with patch.object(live_locations.time, "time", return_value=1000 + 48 * 3600):
            self.assertIsNone(live_locations.get_live_location(-2, 1))

    def test_location_updates_do_not_extend_original_period(self):
        with patch.object(live_locations.time, "time", return_value=1000):
            live_locations.set_live_location(-1, 1, 1, 2, 60)
        with patch.object(live_locations.time, "time", return_value=1050):
            live_locations.set_live_location(-1, 1, 3, 4, 60)
        with patch.object(live_locations.time, "time", return_value=1061):
            self.assertIsNone(live_locations.get_live_location(-1, 1))


class RichPostCleanupTests(DbTest):
    def rich_setup(self, stack, download_error=None):
        self.inputs = Path(self.temp.name) / "intermediates"
        stack.enter_context(patch.object(media, "RICH_POST_INTERMEDIATE_DIR", str(self.inputs)))
        msg = self.message("")
        photo = NS(id=1, sizes=[])
        video = NS(id=2, size=100)
        msg.rich_message = NS(part=False, photos=[photo], documents=[video], blocks=[
            block("PageBlockCover", cover=block("PageBlockPhoto", photo_id=1)),
            block("PageBlockVideo", video_id=2)])
        self.downloads = []
        self.addCleanup(self.remove_downloads)

        async def download(obj, file, **kwargs):
            self.downloads.append(file)
            from PIL import Image
            if file.endswith(".jpg"):
                Image.new("RGB", (8, 8), "blue").save(file, "JPEG")
            else:
                Path(file).write_bytes(b"downloaded input")
            if download_error:
                raise download_error
            return file

        client = MagicMock()
        client.download_profile_photo = download
        client.download_media = download
        from telethon.tl.types import Channel
        # direct-post previews read only Channel entities (group A); keep msg.chat attributes
        entity = MagicMock(spec=Channel)
        entity.username, entity.title = msg.chat.username, msg.chat.title
        client.get_entity = AsyncMock(return_value=entity)
        client.get_messages = AsyncMock(return_value=msg)
        stack.enter_context(patch.object(bot, "userbot_client", client))
        stack.enter_context(patch.object(bot, "dc_bot_instance", MagicMock()))
        stack.enter_context(patch.object(bot, "dc_accid", 1))
        stack.enter_context(patch.object(bot, "_extract_public_tg_post_rich", AsyncMock(return_value=None)))
        return msg

    def remove_downloads(self):
        for path in self.downloads:
            Path(path).unlink(missing_ok=True)

    def assert_inputs_clean(self):
        self.assertTrue(self.downloads)
        self.assertEqual(len({str(Path(p).parent) for p in self.downloads}), 1)
        self.assertTrue(all(not Path(p).exists() for p in self.downloads))
        self.assertEqual(list(self.inputs.iterdir()), [])

    def test_rich_post_success_keeps_cached_deliverable(self):
        with ExitStack() as stack:
            self.rich_setup(stack)
            async def package(post, output, **kwargs):
                self.assertTrue(Path(post.author_avatar_url).exists())
                self.assertTrue(Path(post.image_urls[0]).exists())
                self.assertTrue(Path(post.videos[0].video_url).exists())
                Path(output).write_bytes(b"final package")
                return True
            stack.enter_context(patch.object(bot, "_package_tg_post_webxdc", package))
            stack.enter_context(patch.object(bot, "_dc_send_msg_with_stats", MagicMock()))
            asyncio.run(dc_events._async_handle_direct_tg_post(MagicMock(), 1, 10, "channel", 50))
        self.assert_inputs_clean()
        cached = database.get_cached_tg_post("channel/50")
        self.assertEqual(Path(cached["file_path"]).read_bytes(), b"final package")

    def test_real_packaging_success_keeps_deliverable(self):
        with ExitStack() as stack:
            msg = self.rich_setup(stack)
            msg.rich_message.blocks = [block("PageBlockPhoto", photo_id=1)]
            stack.enter_context(patch.object(media, "_get_channel_avatar_path", return_value=None))
            stack.enter_context(patch.object(bot, "_dc_send_msg_with_stats", MagicMock()))
            asyncio.run(dc_events._async_handle_direct_tg_post(MagicMock(), 1, 10, "channel", 50))
        self.assert_inputs_clean()
        cached = database.get_cached_tg_post("channel/50")
        self.assertTrue(Path(cached["file_path"]).exists())
        import zipfile
        with zipfile.ZipFile(cached["file_path"]) as package:
            self.assertIn("index.html", package.namelist())

    def test_relay_success_cleans_inputs_after_send(self):
        with ExitStack() as stack:
            msg = self.rich_setup(stack)
            outputs = []
            async def package(post, output, **kwargs):
                Path(output).write_bytes(b"final package")
                outputs.append(output)
                return True
            def send(accid, chat_id, msg_data):
                self.assertTrue(Path(msg_data.file).exists())
                self.assertTrue(all(Path(p).exists() for p in self.downloads))
                return 500
            stack.enter_context(patch.object(bot, "_package_tg_post_webxdc", package))
            stack.enter_context(patch.object(bot.dc_bot_instance.rpc, "send_msg", side_effect=send))
            stack.enter_context(patch.object(relay, "_wait_for_global_dc_rate_limit", AsyncMock()))
            asyncio.run(relay._relay_userbot_message(10, msg))
        self.assert_inputs_clean()
        self.assertEqual(len(outputs), 1)
        self.assertTrue(all(not Path(p).exists() for p in outputs))
        self.assertEqual(database.get_dc_msg_id(50, -1001, 10), 500)

    def test_rich_post_packaging_exception_cleans_inputs(self):
        with ExitStack() as stack:
            self.rich_setup(stack)
            stack.enter_context(patch.object(bot, "_package_tg_post_webxdc", AsyncMock(side_effect=RuntimeError("packaging failed"))))
            asyncio.run(dc_events._async_handle_direct_tg_post(MagicMock(), 1, 10, "channel", 50))
        self.assert_inputs_clean()

    def test_rich_post_download_exception_cleans_partial_inputs(self):
        with ExitStack() as stack:
            self.rich_setup(stack, RuntimeError("download failed"))
            asyncio.run(dc_events._async_handle_direct_tg_post(MagicMock(), 1, 10, "channel", 50))
        self.assert_inputs_clean()

    def test_rich_post_cancellation_cleans_inputs(self):
        with ExitStack() as stack:
            self.rich_setup(stack)
            stack.enter_context(patch.object(bot, "_package_tg_post_webxdc", AsyncMock(side_effect=asyncio.CancelledError())))
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(dc_events._async_handle_direct_tg_post(MagicMock(), 1, 10, "channel", 50))
        self.assert_inputs_clean()

    def test_relay_packaging_exception_cleans_inputs_and_output(self):
        with ExitStack() as stack:
            msg = self.rich_setup(stack)
            outputs = []
            async def package(post, output, **kwargs):
                outputs.append(output)
                raise RuntimeError("packaging failed")
            stack.enter_context(patch.object(bot, "_package_tg_post_webxdc", package))
            with self.assertRaises(RuntimeError):
                asyncio.run(relay._relay_userbot_message(10, msg))
        self.assert_inputs_clean()
        self.assertTrue(all(not Path(p).exists() for p in outputs))

    def test_sweep_only_removes_old_intermediates(self):
        inputs = Path(self.temp.name) / "intermediates"
        inputs.mkdir()
        old_dir = inputs / "old"
        old_dir.mkdir()
        (old_dir / "photo.jpg").write_bytes(b"input")
        old_file = inputs / "orphan.jpg"
        old_file.write_bytes(b"input")
        recent = inputs / "recent"
        recent.mkdir()
        cache = Path(self.temp.name) / "cache" / "posts"
        cache.mkdir(parents=True)
        final = cache / "post.xdc"
        final.write_bytes(b"cached post")
        old_time = time.time() - 86401
        for path in (old_dir, old_file, final, cache):
            os.utime(path, (old_time, old_time))
        with patch.object(media, "RICH_POST_INTERMEDIATE_DIR", str(inputs)):
            media.sweep_rich_post_intermediates()
        self.assertFalse(old_dir.exists())
        self.assertFalse(old_file.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(final.exists())


if __name__ == "__main__":
    unittest.main()
