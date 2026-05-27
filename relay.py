import asyncio
import html
import logging
import os
import tempfile
import time
import threading
from typing import Optional, Any

from deltachat2 import MsgData

import database
from app_context import app_ctx
from constants import TG_MAX_MSG_LEN, DC_MAX_MSG_LEN, DC_FALLBACK_PATTERN
from rate_limiter import rate_limiter
from edit_sync import edit_sync_service

logger = logging.getLogger("tg_dc_bridge")


class MessageRelay:
    def __init__(self):
        self.live_locations: dict[int, tuple[float, float]] = {}
        self._pending_dc_relays: set[tuple[int, int]] = set()
        self._pending_dc_relays_lock = threading.Lock()

    def dc_send_msg(self, bot, accid, chat_id: int, msg_data: MsgData, quoted_message_id: int = None):
        if quoted_message_id:
            msg_data.quoted_message_id = quoted_message_id
        transports = []
        max_attempts = 2
        try:
            transports = bot.rpc.list_transports(accid)
            max_attempts = max(2, len(transports))
        except Exception:
            pass

        for attempt in range(max_attempts):
            try:
                msg_id = bot.rpc.send_msg(accid, chat_id, msg_data)
                addr = "unknown"
                try:
                    addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr") or "unknown"
                    if addr != "unknown":
                        database.increment_transport_sent(addr)
                except Exception:
                    pass
                logger.info(f"Successfully sent msg_id {msg_id} to chat {chat_id} on account {accid} (from {addr})")
                return msg_id
            except Exception as e:
                error_str = str(e).lower()
                transport_errors = ["network", "timeout", "connection", "unreachable", "smtp", "status 0", "socket", "refused"]

                if attempt < max_attempts - 1 and any(err in error_str for err in transport_errors):
                    try:
                        current_primary = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
                        if not transports:
                            transports = bot.rpc.list_transports(accid)
                        if len(transports) > 1:
                            for t in transports:
                                t_addr = t.get('addr') if isinstance(t, dict) else getattr(t, 'addr', None)
                                if t_addr and t_addr != current_primary:
                                    logger.warning(f"Transport {current_primary} failed. Switching to backup: {t_addr}")
                                    try:
                                        bot.rpc.set_config(accid, "configured_addr", t_addr)
                                        time.sleep(2)
                                        break
                                    except Exception as set_e:
                                        logger.error(f"Failed to switch transport: {set_e}")
                                        continue
                    except Exception as fail_e:
                        logger.error(f"Failover logic error: {fail_e}")

                if attempt == max_attempts - 1:
                    logger.error(f"Failed to send DC message after {max_attempts} attempts. Last error: {e}")
                    raise e

    async def relay_to_tg(self, tg_chat_id: int, dc_chat_id: int, msg_id: int,
                          file_path: Optional[str], formatted_msg: str, tg_reply_id: Optional[int],
                          is_image: bool, is_video: bool, is_voice: bool, viewtype: str = ''):
        try:
            tg_msg = None
            is_media = is_image or is_video or is_voice or viewtype in ('Document', 'File', 'Image', 'Video', 'Voice', 'Audio', 'Gif', 'Sticker')

            if is_media and not file_path:
                for _ in range(30):
                    await asyncio.sleep(2)
                    try:
                        updated_msg = app_ctx.dc_bot.rpc.get_message(app_ctx.dc_accid, msg_id)
                        file_path = getattr(updated_msg, 'file', None) or None
                        if file_path and os.path.exists(file_path):
                            break
                    except Exception:
                        pass
                if not file_path:
                    logger.warning(f"Timeout waiting for media to download for DC msg {msg_id}")
                    formatted_msg += "\n\n*[Failed to relay media: timeout waiting for DC download]*"

            if file_path and os.path.exists(file_path):
                filename = os.path.basename(file_path)
                try:
                    if is_image:
                        f = open(file_path, 'rb')
                        func = app_ctx.tg_app.bot.send_photo
                        kwargs = {'chat_id': tg_chat_id, 'photo': f, 'caption': formatted_msg, 'parse_mode': 'HTML', 'reply_to_message_id': tg_reply_id}
                    elif is_video:
                        f = open(file_path, 'rb')
                        func = app_ctx.tg_app.bot.send_video
                        kwargs = {'chat_id': tg_chat_id, 'video': f, 'caption': formatted_msg, 'parse_mode': 'HTML', 'reply_to_message_id': tg_reply_id}
                    elif is_voice:
                        f = open(file_path, 'rb')
                        func = app_ctx.tg_app.bot.send_voice
                        kwargs = {'chat_id': tg_chat_id, 'voice': f, 'caption': formatted_msg, 'parse_mode': 'HTML', 'reply_to_message_id': tg_reply_id}
                    else:
                        f = open(file_path, 'rb')
                        func = app_ctx.tg_app.bot.send_document
                        kwargs = {'chat_id': tg_chat_id, 'document': f, 'caption': formatted_msg, 'parse_mode': 'HTML', 'reply_to_message_id': tg_reply_id}

                    tg_msg = await retry_async(func, **kwargs)
                except (TimeoutError, asyncio.TimeoutError):
                    fallback_text = formatted_msg + f"\n\n*[Failed to relay media: {html.escape(filename)} - timeout exceeded after retries]*"
                    tg_msg = await app_ctx.tg_app.bot.send_message(chat_id=tg_chat_id, text=fallback_text, parse_mode='HTML', reply_to_message_id=tg_reply_id)
                except Exception as e:
                    logger.error(f"Error uploading media to TG: {e}")
                    tg_msg = await app_ctx.tg_app.bot.send_message(chat_id=tg_chat_id, text=formatted_msg, parse_mode='HTML', reply_to_message_id=tg_reply_id)
            else:
                tg_msg = await app_ctx.tg_app.bot.send_message(chat_id=tg_chat_id, text=formatted_msg, parse_mode='HTML', reply_to_message_id=tg_reply_id)

            if tg_msg:
                database.save_message_map(msg_id, dc_chat_id, tg_msg.message_id, tg_chat_id)
        except Exception as e:
            logger.error(f"Failed to relay msg to TG chat {tg_chat_id}: {e}")

    async def relay_dc_to_tg_channel(self, tg_channel_id: int, dc_chat_id: int, dc_msg_id: int,
                                     text: str, file_path: Optional[str], viewtype: str = ''):
        try:
            is_image = viewtype in ('Image', 'Gif', 'Sticker') or (
                file_path and file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp'))
            )
            is_video = viewtype == 'Video' or (
                file_path and file_path.lower().endswith(('.mp4', '.mov', '.avi', '.mkv'))
            )
            is_voice = viewtype in ('Voice', 'Audio')
            is_media = is_image or is_video or is_voice or viewtype in ('Document', 'File', 'Audio', 'Gif', 'Sticker')

            if is_media and not file_path:
                for _ in range(30):
                    await asyncio.sleep(2)
                    try:
                        updated_msg = app_ctx.dc_bot.rpc.get_message(app_ctx.dc_accid, dc_msg_id)
                        file_path = getattr(updated_msg, 'file', None) or None
                        if file_path and os.path.exists(file_path):
                            break
                    except Exception:
                        pass

            tg_msg = None
            if file_path and os.path.exists(file_path):
                try:
                    if is_image:
                        with open(file_path, 'rb') as f:
                            tg_msg = await app_ctx.tg_app.bot.send_photo(chat_id=tg_channel_id, photo=f, caption=text)
                    elif is_video:
                        with open(file_path, 'rb') as f:
                            tg_msg = await app_ctx.tg_app.bot.send_video(chat_id=tg_channel_id, video=f, caption=text)
                    elif is_voice:
                        with open(file_path, 'rb') as f:
                            tg_msg = await app_ctx.tg_app.bot.send_voice(chat_id=tg_channel_id, voice=f, caption=text)
                    else:
                        with open(file_path, 'rb') as f:
                            tg_msg = await app_ctx.tg_app.bot.send_document(chat_id=tg_channel_id, document=f, caption=text)
                except Exception as e:
                    logger.error(f"Error uploading media to TG channel {tg_channel_id}: {e}")
                    tg_msg = await app_ctx.tg_app.bot.send_message(chat_id=tg_channel_id, text=text or "*[Media omitted]*")
            else:
                tg_msg = await app_ctx.tg_app.bot.send_message(chat_id=tg_channel_id, text=text)

            if tg_msg:
                database.save_message_map(dc_msg_id, dc_chat_id, tg_msg.message_id, tg_channel_id)
        except Exception as e:
            logger.error(f"Failed to relay DC msg {dc_msg_id} to TG channel {tg_channel_id}: {e}")
        finally:
            with self._pending_dc_relays_lock:
                self._pending_dc_relays.discard((dc_chat_id, dc_msg_id))

    def register_pending_relay(self, dc_chat_id: int, dc_msg_id: int):
        with self._pending_dc_relays_lock:
            self._pending_dc_relays.add((dc_chat_id, dc_msg_id))

    def is_pending_relay(self, dc_chat_id: int, dc_msg_id: int) -> bool:
        with self._pending_dc_relays_lock:
            return (dc_chat_id, dc_msg_id) in self._pending_dc_relays

    async def delete_tg_message(self, tg_chat_id: int, tg_msg_id: int, info_text: str = ""):
        userbot_client = app_ctx.userbot_client
        deleted = False
        if app_ctx.tg_app:
            try:
                await app_ctx.tg_app.bot.delete_message(chat_id=tg_chat_id, message_id=tg_msg_id)
                logger.info(f"DC→TG: Deleted TG msg {tg_msg_id} in {tg_chat_id}{info_text}")
                deleted = True
            except Exception as e:
                e_str = str(e).lower()
                if any(pat in e_str for pat in ("not a member", "chat not found", "message to delete not found", "can't be deleted")):
                    logger.debug(f"DC→TG: Bot API failed to delete msg {tg_msg_id} in {tg_chat_id}{info_text}, falling back to Userbot: {e}")
                else:
                    logger.warning(f"DC→TG: Could not delete TG msg {tg_msg_id} in {tg_chat_id}{info_text} via Bot API: {e}")

        if not deleted and userbot_client and userbot_client.is_connected():
            try:
                entity = await userbot_client.get_entity(tg_chat_id)
                await userbot_client.delete_messages(entity, [tg_msg_id])
                logger.info(f"DC→TG: Deleted TG msg {tg_msg_id} in {tg_chat_id}{info_text} via Userbot")
            except Exception as e:
                e_str = str(e).lower()
                if any(pat in e_str for pat in ("not a member", "chat not found", "can't be deleted", "invalid object id", "message to delete not found")):
                    logger.debug(f"DC→TG: Could not delete TG msg {tg_msg_id} in {tg_chat_id}{info_text} via Userbot (no permission/not found): {e}")
                else:
                    logger.warning(f"DC→TG: Could not delete TG msg {tg_msg_id} in {tg_chat_id}{info_text} via Userbot either: {e}")

    async def edit_tg_channel_post(self, tg_chat_id: int, tg_msg_id: int, new_text: str, has_file: bool,
                                   dc_msg_id: int = 0, dc_chat_id: int = 0, new_hash: str = ""):
        if not app_ctx.tg_app:
            return
        try:
            if has_file:
                await app_ctx.tg_app.bot.edit_message_caption(
                    chat_id=tg_chat_id, message_id=tg_msg_id, caption=new_text or None,
                )
            else:
                await app_ctx.tg_app.bot.edit_message_text(
                    chat_id=tg_chat_id, message_id=tg_msg_id, text=new_text or " ",
                )
            if new_hash and dc_msg_id and dc_chat_id:
                database.save_message_map(dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id, content_hash=new_hash)
            logger.info(f"DC→TG: Edited TG msg {tg_msg_id} in {tg_chat_id}")
        except Exception as e:
            e_str = str(e).lower()
            if "message is not modified" in e_str:
                logger.debug(f"DC→TG: Skipping edit — content unchanged (msg {tg_msg_id} in {tg_chat_id})")
            elif any(pat in e_str for pat in ("flood", "too many requests", "timed out", "retry after")):
                logger.warning(f"DC→TG: Transient error editing TG msg {tg_msg_id} in {tg_chat_id}: {e}")
            else:
                logger.error(f"Failed to edit TG msg {tg_msg_id} in {tg_chat_id}: {e}")

    async def relay_userbot_message(self, dc_chat_id: int, msg, is_edit: bool = False, display_author: str = None):
        userbot_client = app_ctx.userbot_client
        if not (userbot_client and userbot_client.is_connected()):
            return

        tg_channel_id = msg.chat_id
        text = msg.text or ""

        if text.startswith('/') and not msg.media:
            return

        formatted_msg = text

        edit_prefix = "✏️ [Edited]" if is_edit else ""
        if msg.chat and getattr(msg.chat, 'username', None):
            formatted_msg = (formatted_msg + f"\n\n🔗 t.me/{msg.chat.username}/{msg.id}").strip()

        if not formatted_msg and not msg.media:
            return

        formatted_msg = _truncate(formatted_msg, DC_MAX_MSG_LEN)

        file_path = None
        if msg.media:
            try:
                media_size = 0
                if hasattr(msg, 'file') and msg.file:
                    media_size = msg.file.size or 0

                if media_size > 50 * 1024 * 1024:
                    logger.warning(f"Userbot: Media in channel {tg_channel_id} is too large: {media_size // 1024 // 1024}MB > 50MB")
                    formatted_msg += f"\n\n[Media is too large to be forwarded (limit 50MB)]"
                else:
                    suffix = getattr(msg.file, 'ext', "") if hasattr(msg, 'file') and msg.file else ""
                    tmp_fd, file_path_tmp = tempfile.mkstemp(suffix=suffix)
                    os.close(tmp_fd)
                    file_path = await userbot_client.download_media(msg.media, file=file_path_tmp)
            except Exception as e:
                logger.error(f"Failed to download userbot media: {e}")

        try:
            msg_data = MsgData(text=formatted_msg)

            if not display_author:
                if hasattr(msg, 'post_author') and msg.post_author:
                    display_author = msg.post_author
                else:
                    sender = await msg.get_sender()
                    if sender:
                        if hasattr(sender, 'first_name'):
                            display_author = f"{sender.first_name} {getattr(sender, 'last_name', '') or ''}".strip()
                        elif hasattr(sender, 'title'):
                            display_author = sender.title

            if display_author:
                msg_data.override_sender_name = display_author if not is_edit else f"✏️ [Edited] {display_author}"
            elif is_edit:
                msg_data.override_sender_name = "✏️ [Edited]"

            if file_path:
                msg_data.file = file_path

            if is_edit:
                old_dc_msg_id = database.get_dc_msg_id(msg.id, tg_channel_id, dc_chat_id)
                if old_dc_msg_id:
                    try:
                        edit_sync_service.register_bot_delete(old_dc_msg_id)
                        app_ctx.dc_bot.rpc.delete_messages(app_ctx.dc_accid, [old_dc_msg_id])
                    except Exception as del_e:
                        logger.debug(f"Could not delete old DC msg {old_dc_msg_id} before userbot edit relay: {del_e}")
                        edit_sync_service.consume_bot_delete(old_dc_msg_id)

            await rate_limiter.wait_global_dc()
            sent_id = app_ctx.dc_bot.rpc.send_msg(app_ctx.dc_accid, dc_chat_id, msg_data)
            if sent_id:
                c_hash = edit_sync_service.get_content_hash(msg)
                database.save_message_map(sent_id, dc_chat_id, msg.id, tg_channel_id, content_hash=c_hash)
            logger.info(f"Relayed userbot {'edited ' if is_edit else ''}post from {tg_channel_id} to DC chat {dc_chat_id}")
        except Exception as e:
            logger.error(f"Failed to relay userbot message: {e}")
        finally:
            if file_path and os.path.exists(file_path):
                try:
                    os.unlink(file_path)
                except Exception:
                    pass

    def update_live_location(self, msg_id: int, lat: float, lon: float):
        self.live_locations[msg_id] = (lat, lon)

    def pop_live_location(self, msg_id: int):
        return self.live_locations.pop(msg_id, None)

    def get_live_location(self, msg_id: int):
        return self.live_locations.get(msg_id)


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


async def retry_async(coro_func, *args, max_retries=5, delay=2.0, backoff=2.0,
                      exceptions=(TimeoutError, asyncio.TimeoutError, ConnectionError, OSError), **kwargs):
    from telegram.error import NetworkError, TimedOut
    exceptions = exceptions + (NetworkError, TimedOut)
    last_exception = None
    for attempt in range(max_retries):
        try:
            return await coro_func(*args, **kwargs)
        except exceptions as e:
            last_exception = e
            if attempt == max_retries - 1:
                break
            wait = delay * (backoff ** attempt)
            logger.warning(f"Operation failed with {type(e).__name__}, retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(wait)
    raise last_exception


message_relay = MessageRelay()
