import logging
import os
import tempfile
from typing import Optional, Any

from app_context import app_ctx

logger = logging.getLogger("tg_dc_bridge")


class MediaHandler:
    def detect_media(self, obj) -> tuple:
        tg_file = None
        file_name = None
        text_extra = ""
        if obj.photo:
            tg_file = obj.photo[-1]
            file_name = "photo.jpg"
        elif obj.video:
            vid = obj.video
            tg_file = vid
            file_name = vid.file_name or "video.mp4"
            v_size = getattr(vid, 'file_size', 0) or 0
            if v_size > 20 * 1024 * 1024:
                result = self._handle_large_video(vid, v_size)
                if result:
                    tg_file, file_name, text_extra = result
        elif obj.animation:
            tg_file = obj.animation
            file_name = "animation.gif"
        elif obj.voice:
            tg_file = obj.voice
            file_name = "voice.ogg"
        elif obj.audio:
            tg_file = obj.audio
            file_name = obj.audio.file_name or "audio.mp3"
        elif obj.document:
            tg_file = obj.document
            file_name = obj.document.file_name or "file"
        elif obj.sticker:
            tg_file = obj.sticker
            file_name = "sticker.webp"
        elif obj.video_note:
            tg_file = obj.video_note
            file_name = "video_note.mp4"
        return tg_file, file_name, text_extra

    detect_channel_post_media = detect_media
    detect_message_media = detect_media

    def _handle_large_video(self, vid, v_size: int, text: str = "") -> tuple:
        qualities = getattr(vid, 'qualities', []) or []
        valid_q = [q for q in qualities if (getattr(q, 'file_size', 0) or 0) < 20 * 1024 * 1024]
        if valid_q:
            valid_q.sort(key=lambda q: getattr(q, 'file_size', 0) or getattr(q, 'height', 0) or 0, reverse=True)
            text_extra = f"\n\n[Video was too large ({v_size // 1024 // 1024} MB), forwarded in lower resolution]"
            return valid_q[0], getattr(valid_q[0], 'file_name', 'video.mp4'), text_extra
        else:
            text_extra = f"\n\n[🎥 Video is too large ({v_size // 1024 // 1024} MB) to be forwarded]"
            return None, None, text_extra

    def detect_location(self, update_or_post) -> str:
        text = ""
        venue = getattr(update_or_post, 'venue', None)
        location = getattr(update_or_post, 'location', None)
        if venue:
            text = f"📍 Venue: {venue.title}\n{venue.address}\nhttps://maps.google.com/?q={venue.location.latitude},{venue.location.longitude}"
        elif location:
            is_live = getattr(location, 'live_period', None) is not None
            if is_live:
                live_mins = location.live_period // 60
                text = f"📍 Live Location ({live_mins} min)\nhttps://maps.google.com/?q={location.latitude},{location.longitude}\n\n(Reply with /locupdate to get the latest coordinates)"
            else:
                text = f"📍 Location: https://maps.google.com/?q={location.latitude},{location.longitude}"
        return text

    async def download_media(self, tg_file, file_name: str, tg_chat_id: int = None, tg_msg_id: int = None) -> tuple:
        if not tg_file:
            return None, ""
        local_file_path = None
        timeout_error_text = ""
        suffix = os.path.splitext(file_name)[1] if file_name else ""

        try:
            f_size = getattr(tg_file, 'file_size', 0) or 0

            if f_size > 20 * 1024 * 1024 and tg_chat_id and tg_msg_id:
                local_file_path = await self._userbot_download(tg_chat_id, tg_msg_id, suffix)

            if not local_file_path:
                tg_file_obj = await tg_file.get_file()
                tmp_fd, local_file_path_tmp = tempfile.mkstemp(suffix=suffix)
                os.close(tmp_fd)
                await self._retry_download(tg_file_obj, local_file_path_tmp)
                local_file_path = local_file_path_tmp
        except Exception as e:
            logger.error(f"Failed to download media {file_name}: {e}")
            if "File is too big" in str(e) and not local_file_path and tg_chat_id and tg_msg_id:
                local_file_path = await self._userbot_download(tg_chat_id, tg_msg_id, suffix)
            if not local_file_path:
                timeout_error_text = f"\n\n[Failed to download media: {file_name}]"

        return local_file_path, timeout_error_text

    async def _userbot_download(self, chat_id: int, msg_id: int, suffix: str = "") -> Optional[str]:
        userbot_client = app_ctx.userbot_client
        if not (userbot_client and userbot_client.is_connected()):
            return None
        try:
            msg = await userbot_client.get_messages(chat_id, ids=msg_id)
            if not (msg and msg.media):
                return None

            size = 0
            if hasattr(msg, 'document') and msg.document:
                size = msg.document.size
            elif hasattr(msg, 'video') and msg.video:
                size = msg.video.size

            if size > 50 * 1024 * 1024:
                logger.warning(f"Userbot: Media in {chat_id}:{msg_id} is too large ({size // 1024 // 1024} MB > 50 MB)")
                return None

            tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            os.close(tmp_fd)
            logger.info(f"Userbot downloading media from {chat_id}:{msg_id} (size: {size // 1024 // 1024} MB)...")
            path = await userbot_client.download_media(msg.media, file=tmp_path)
            return path
        except Exception as e:
            logger.error(f"Userbot download failed for {chat_id}:{msg_id}: {e}")
        return None

    async def _retry_download(self, file_obj, path: str, max_retries: int = 5, delay: float = 2.0, backoff: float = 2.0):
        import asyncio
        from telegram.error import NetworkError, TimedOut
        last_exception = None
        exceptions = (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError, NetworkError, TimedOut)
        for attempt in range(max_retries):
            try:
                await file_obj.download_to_drive(custom_path=path)
                return
            except exceptions as e:
                last_exception = e
                if attempt == max_retries - 1:
                    break
                wait = delay * (backoff ** attempt)
                logger.warning(f"Download failed with {type(e).__name__}, retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
                await asyncio.sleep(wait)
        raise last_exception


media_handler = MediaHandler()
