"""Telegram albums in bridged groups: one WebXDC gallery instead of one DC message per item.

The Bot API delivers an album as separate updates sharing a media_group_id and never
says when the album is complete, so items are collected per (chat, media_group_id) and
the album is flushed once no new item arrived for ALBUM_DEBOUNCE seconds. If anything
goes wrong the items are replayed through handle_tg_message one by one (the behavior
before this module), so a failed package never loses a photo.

References to bot.py singletons and to names the test suite patches on bot.py
(_package_tg_post_webxdc, _dc_send_msg_with_stats) go through a function-local
`import bot as _bot_module`, same as tg_events.py.
"""
import asyncio
import html
import logging
import os
import re
import tempfile
import time

import database
from deltachat2 import MsgData

from caching import _get_content_hash
from formatting import TelegramRichPost, TelegramRichVideo, _format_telegram_entities, _group_sender_name, _truncate
from media import _download_via_userbot
from security import (
    retry_async,
    is_text_filtered,
    _is_rate_limited,
    _processed_tg_msgs,
    _wait_for_global_dc_rate_limit,
    track_dc_outgoing,
)

logger = logging.getLogger("tg_dc_bridge")

ALBUM_DEBOUNCE = 2.0      # seconds without a new item before the album counts as complete
ALBUM_MAX_WAIT = 20.0     # an album is flushed at the latest this long after its first item
BOT_API_MAX_BYTES = 20 * 1024 * 1024   # what getFile can serve; also the WebXDC single-video limit

# (tg_chat_id, media_group_id) -> {"items": [...], "first": ts, "last": ts, "limited": bool}
_albums: dict[tuple, dict] = {}
# message ids being replayed one by one after a failed package; they must not be buffered again
_replaying: set[tuple] = set()


def _album_item(update):
    """Photo/video of an album message as a dict, or None for anything a gallery cannot show."""
    message = update.message
    if message.photo:
        return {"update": update, "file": message.photo[-1], "kind": "photo", "size": 0}
    if message.video:
        return {"update": update, "file": message.video, "kind": "video",
                "size": getattr(message.video, "file_size", 0) or 0}
    return None


def try_buffer(update, rich_mode=None):
    """Take an album item into the buffer.

    Returns None when the message is not part of a photo/video album (the caller relays it
    as usual), otherwise (outcome, reason) for group_watch.note_outcome.
    """
    message = update.message
    group_id = getattr(message, "media_group_id", None)
    if not group_id or not isinstance(group_id, (str, int)):
        return None
    if rich_mode is None:
        rich_mode = database.get_rich_mode()
    if rich_mode not in ("webxdc", "both"):
        return None
    chat_id = update.effective_chat.id
    if (chat_id, message.message_id) in _replaying:
        return None
    item = _album_item(update)
    if item is None:
        return None

    key = (chat_id, group_id)
    now = time.monotonic()
    album = _albums.get(key)
    if album is None:
        # The rate limit counts an album as one message, not one per item.
        album = _albums[key] = {"items": [], "first": now, "last": now, "limited": _is_rate_limited(chat_id)}
        asyncio.get_running_loop().create_task(_flush_when_quiet(key))
    album["last"] = now
    if album["limited"]:
        return ("skipped", "rate limit")
    album["items"].append(item)
    return ("relayed", "")


async def _flush_when_quiet(key):
    album = _albums[key]
    while True:
        now = time.monotonic()
        if now - album["last"] >= ALBUM_DEBOUNCE or now - album["first"] >= ALBUM_MAX_WAIT:
            break
        await asyncio.sleep(min(ALBUM_DEBOUNCE - (now - album["last"]), 0.5) or 0.1)
    _albums.pop(key, None)
    items = sorted(album["items"], key=lambda i: i["update"].message.message_id)
    if not items:
        return
    try:
        if len(items) > 1 and await _relay_album(key[0], key[1], items):
            return
    except Exception as e:
        logger.error(f"Album {key[1]} in {key[0]}: relaying as WebXDC failed: {type(e).__name__}: {e}")
    await _replay_one_by_one(key[0], items)


async def _replay_one_by_one(tg_chat_id, items):
    """Fallback: relay every item as its own message, as before albums were bundled."""
    from tg_events import handle_tg_message
    logger.info(f"Album in {tg_chat_id}: relaying {len(items)} item(s) one by one")
    for item in items:
        update = item["update"]
        ident = (tg_chat_id, update.message.message_id)
        _replaying.add(ident)
        # handle_tg_message skips a message it saw in the last 120 seconds
        _processed_tg_msgs.pop((tg_chat_id, update.message.message_id, "new"), None)
        try:
            await handle_tg_message(update, None)
        except Exception as e:
            logger.error(f"Album item {update.message.message_id} in {tg_chat_id} could not be relayed: {e}")
        finally:
            _replaying.discard(ident)


async def _download_item(item, tg_chat_id):
    """Download one photo/video to a temp file, or None (too big or download failed)."""
    message = item["update"].message
    tg_file = item["file"]
    suffix = ".jpg" if item["kind"] == "photo" else ".mp4"
    if item["size"] > BOT_API_MAX_BYTES:
        # A gallery embeds videos up to 20 MB; the rest is shown as a link card.
        return None

    tmp_fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(tmp_fd)

    async def _bot_api_download():
        tg_file_obj = await tg_file.get_file()
        await tg_file_obj.download_to_drive(custom_path=path)

    try:
        await retry_async(_bot_api_download)
        return path
    except Exception as e:
        logger.warning(f"Album item {message.message_id} in {tg_chat_id}: Bot API download failed ({type(e).__name__}), trying userbot")
    try:
        os.unlink(path)
    except OSError:
        pass
    return await _download_via_userbot(tg_chat_id, message.message_id, suffix=suffix)


async def _download_author_avatar(message):
    """The sender's current Telegram profile photo as a temp file, or None.

    The gallery's app icon falls back to the bot's own avatar without it. The Bot API returns
    no photo for a user whose privacy settings hide it from the bot, so the userbot (a normal
    group member, which may be allowed to see it) is asked next. Every failure just means
    "no avatar", but is logged so a missing one can be explained.
    """
    sender = message.from_user
    if not sender:
        return None
    path = await _avatar_via_bot_api(message, sender)
    if path:
        return path
    path = await _avatar_via_userbot(message, sender)
    if not path:
        logger.info(f"Album: no profile photo for {sender.id} from the Bot API or the userbot, using the default icon")
    return path


async def _avatar_via_bot_api(message, sender):
    path = None
    try:
        photos = await retry_async(message.get_bot().get_user_profile_photos, sender.id, limit=1, max_retries=2, delay=1.0)
        if not photos or not photos.photos:
            logger.info(f"Album: Bot API has no profile photo for {sender.id}")
            return None
        tmp_fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(tmp_fd)
        tg_file = await retry_async(photos.photos[0][-1].get_file, max_retries=2, delay=1.0)
        await tg_file.download_to_drive(custom_path=path)
        return path
    except Exception as e:
        logger.warning(f"Album: Bot API avatar download for {sender.id} failed: {type(e).__name__}: {e}")
        if path and os.path.exists(path):
            os.unlink(path)
        return None


async def _avatar_via_userbot(message, sender):
    import bot as _bot_module
    client = _bot_module.userbot_client
    if not (client and client.is_connected()):
        return None
    tmp_fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(tmp_fd)
    try:
        # The sender entity comes from the message itself: a bare user id may not be known to Telethon.
        tl_msg = await asyncio.wait_for(client.get_messages(message.chat.id, ids=message.message_id), timeout=15.0)
        tl_sender = await asyncio.wait_for(tl_msg.get_sender(), timeout=15.0) if tl_msg else None
        if tl_sender:
            saved = await asyncio.wait_for(client.download_profile_photo(tl_sender, file=path), timeout=30.0)
            if saved and os.path.getsize(saved) > 0:
                return saved
    except Exception as e:
        logger.warning(f"Album: userbot avatar download for {sender.id} failed: {type(e).__name__}: {e}")
    if os.path.exists(path):
        os.unlink(path)
    return None


def _caption_html(caption: str) -> str:
    """Plain caption as HTML: escaped, links clickable, line breaks kept."""
    escaped = html.escape(caption)
    escaped = re.sub(r'(https?://[^\s<]+)', r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', escaped)
    return "<p>" + escaped.replace("\n", "<br>") + "</p>"


async def _relay_album(tg_chat_id, group_id, items) -> bool:
    """Bundle the album into one WebXDC and send it to every bridged DC chat. False = nothing sent."""
    import bot as _bot_module
    if not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        return False
    dc_chats = database.get_dc_chats(tg_chat_id)
    if not dc_chats:
        return False

    messages = [i["update"].message for i in items]
    # Only one message of an album carries the caption; filter on all of them (see v2.32.2).
    captions = []
    for m in messages:
        raw = m.caption or ""
        if raw:
            captions.append(_format_telegram_entities(raw, m.caption_entities or []))
    caption = "\n".join(captions)
    filtered, matched_pat = is_text_filtered(caption)
    if filtered:
        logger.info(f"Album {group_id} in {tg_chat_id}: skipped, matches filter '{matched_pat}'")
        return True

    paths = []
    xdc_path = None
    try:
        post_images, post_videos, n_photos, n_videos = [], [], 0, 0
        for item in items:
            local = await _download_item(item, tg_chat_id)
            if item["kind"] == "photo":
                n_photos += 1
                if local:
                    paths.append(local)
                    post_images.append(local)
            else:
                n_videos += 1
                if local:
                    paths.append(local)
                    post_videos.append(TelegramRichVideo(video_url=local))
                else:
                    post_videos.append(TelegramRichVideo(is_too_big=True))
        if not post_images and not any(v.video_url for v in post_videos):
            logger.warning(f"Album {group_id} in {tg_chat_id}: no item could be downloaded")
            return False

        first = messages[0]
        chat = first.chat
        sender_name = _group_sender_name(first)
        raw_caption = "\n".join(m.caption for m in messages if m.caption)
        username = getattr(chat, "username", None) or ""
        avatar_path = await _download_author_avatar(first)
        if avatar_path:
            paths.append(avatar_path)
        post = TelegramRichPost(
            username=username,
            post_id=first.message_id,
            author_avatar_url=avatar_path or "",
            author_name=sender_name or getattr(chat, "title", "") or "Telegram",
            text_html=_caption_html(raw_caption) if raw_caption else "",
            text_markdown=raw_caption,
            image_urls=post_images,
            videos=post_videos,
            published_date=first.date.strftime("%Y-%m-%d %H:%M") if getattr(first, "date", None) else "",
        )
        tmp_fd, xdc_path = tempfile.mkstemp(suffix=".xdc")
        os.close(tmp_fd)
        if not await _bot_module._package_tg_post_webxdc(post, xdc_path, dc_chat_id=dc_chats[0]):
            return False

        counts = []
        if n_photos:
            counts.append(f"{n_photos} photo{'s' if n_photos != 1 else ''}")
        if n_videos:
            counts.append(f"{n_videos} video{'s' if n_videos != 1 else ''}")
        text = (caption + "\n\n" if caption else "") + "🖼 Album: " + ", ".join(counts)
        text = _truncate(text, _bot_module.DC_MAX_MSG_LEN)

        replied = next((m.reply_to_message for m in messages if m.reply_to_message), None)
        sent_any = False
        for dc_chat_id in dc_chats:
            try:
                msg_data = MsgData(text=text)
                msg_data.override_sender_name = sender_name
                msg_data.file = xdc_path
                dc_reply_id = database.get_dc_msg_id(replied.message_id, tg_chat_id, dc_chat_id) if replied else None
                if dc_reply_id:
                    msg_data.quoted_message_id = dc_reply_id
                await _wait_for_global_dc_rate_limit()
                try:
                    sent_id = _bot_module._dc_send_msg_with_stats(_bot_module.dc_bot_instance, _bot_module.dc_accid, dc_chat_id, msg_data)
                except Exception as e:
                    if not (dc_reply_id and "does not exist" in str(e).lower()):
                        raise
                    msg_data.quoted_message_id = None  # the quoted DC message is gone
                    sent_id = _bot_module._dc_send_msg_with_stats(_bot_module.dc_bot_instance, _bot_module.dc_accid, dc_chat_id, msg_data)
                track_dc_outgoing(sent_id)
                if sent_id:
                    # Every item points at the one DC message, so replies and reactions find it.
                    for m in messages:
                        database.save_message_map(sent_id, dc_chat_id, m.message_id, tg_chat_id, content_hash=_get_content_hash(m))
                sent_any = True
                logger.info(f"Relayed album {group_id} ({len(items)} items) from {tg_chat_id} to DC chat {dc_chat_id} as WebXDC")
            except Exception as e:
                logger.error(f"Failed to relay album {group_id} from {tg_chat_id} to DC chat {dc_chat_id}: {e}")
        if sent_any:
            database.mark_media_group_processed(group_id, tg_chat_id)
        return sent_any
    finally:
        for p in paths + ([xdc_path] if xdc_path else []):
            try:
                os.unlink(p)
            except OSError:
                pass
