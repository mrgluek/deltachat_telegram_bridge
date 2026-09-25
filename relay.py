"""Core relay functions shared by the DC-side event handlers and the
Telethon userbot subsystem: pushing a DC message to Telegram, editing a
TG message in place, deleting a TG message, and relaying a raw Telethon
message into Delta Chat.

Every reference to the pervasive bot.py singletons (dc_bot_instance,
dc_accid, userbot_client, tg_app) and to functions not yet split out of
bot.py (or that the test suite patches directly on bot.py, such as
_extract_public_tg_post_rich/_package_tg_post_webxdc) goes through a
function-local `import bot as _bot_module` and qualified attribute
access — never a module-level `from bot import name`, which would both
go stale the moment bot.py reassigns a singleton and deadlock the
circular import bot.py's own re-export of this module creates (see the
comment on run_cli() in bot.py).
"""
import asyncio
import os
import tempfile
import time

import database
from deltachat2 import MsgData

from security import retry_async, is_text_filtered, _wait_for_global_dc_rate_limit
from caching import (
    _is_media_group_processed,
    _update_cached_last_msg_id,
    _get_cached_dc_channel_chat_id,
    _get_content_hash,
)
from formatting import _format_telegram_entities, _format_poll_text, _truncate
from rpc_proxy import _get_download_semaphore

import logging
logger = logging.getLogger("tg_dc_bridge")


# One lock per TG chat, used only on the main event loop. DC messages are
# scheduled onto the loop in arrival order and asyncio.Lock wakes waiters
# first-in-first-out, so holding it for the whole send keeps a text message
# from overtaking an image that is still downloading or uploading.
_tg_send_locks = {}


async def async_relay_to_tg(tg_chat_id, dc_chat_id, msg_id, file_path, formatted_msg, tg_reply_id, is_image, is_video, is_voice, viewtype=''):
    lock = _tg_send_locks.setdefault(tg_chat_id, asyncio.Lock())
    async with lock:
        await _relay_to_tg(tg_chat_id, dc_chat_id, msg_id, file_path, formatted_msg, tg_reply_id, is_image, is_video, is_voice, viewtype)


async def _relay_to_tg(tg_chat_id, dc_chat_id, msg_id, file_path, formatted_msg, tg_reply_id, is_image, is_video, is_voice, viewtype=''):
    import bot as _bot_module
    try:
        tg_msg = None
        
        # If the message should have a file but hasn't downloaded yet, wait up to 60s
        is_media = is_image or is_video or is_voice or viewtype in ('Document', 'File', 'Image', 'Video', 'Voice', 'Audio', 'Gif', 'Sticker')
        if is_media and not file_path:
            for _ in range(30):
                await asyncio.sleep(2)
                try:
                    updated_msg = _bot_module.dc_bot_instance.rpc.get_message(_bot_module.dc_accid, msg_id)
                    file_path = getattr(updated_msg, 'file', None) or None
                    if file_path and os.path.exists(file_path):
                        break
                except Exception:
                    pass
            if not file_path:
                logger.warning(f"Timeout waiting for media to download for DC msg {msg_id} (chat {dc_chat_id})")
                formatted_msg += "\n\n*[Failed to relay media: timeout waiting for DC download]*"

        if file_path and os.path.exists(file_path):
            filename = os.path.basename(file_path)
            try:
                if is_image:
                    f = open(file_path, 'rb')
                    func = _bot_module.tg_app.bot.send_photo
                    kwargs = {'chat_id': tg_chat_id, 'photo': f, 'caption': formatted_msg, 'parse_mode': 'HTML', 'reply_to_message_id': tg_reply_id}
                elif is_video:
                    f = open(file_path, 'rb')
                    func = _bot_module.tg_app.bot.send_video
                    kwargs = {'chat_id': tg_chat_id, 'video': f, 'caption': formatted_msg, 'parse_mode': 'HTML', 'reply_to_message_id': tg_reply_id}
                elif is_voice:
                    f = open(file_path, 'rb')
                    func = _bot_module.tg_app.bot.send_voice
                    kwargs = {'chat_id': tg_chat_id, 'voice': f, 'caption': formatted_msg, 'parse_mode': 'HTML', 'reply_to_message_id': tg_reply_id}
                else:
                    f = open(file_path, 'rb')
                    func = _bot_module.tg_app.bot.send_document
                    kwargs = {'chat_id': tg_chat_id, 'document': f, 'caption': formatted_msg, 'parse_mode': 'HTML', 'reply_to_message_id': tg_reply_id}
                
                tg_msg = await retry_async(func, max_retries=3, delay=2.0, backoff=2.0, **kwargs)
                try:
                    f.close()
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Error uploading media '{filename}' for DC msg {msg_id} to TG chat {_bot_module._get_tg_chat_desc(tg_chat_id)}: {e}. Retrying text fallback...")
                tg_msg = await retry_async(
                    _bot_module.tg_app.bot.send_message,
                    max_retries=3, delay=2.0, backoff=2.0,
                    chat_id=tg_chat_id, text=formatted_msg, parse_mode='HTML', reply_to_message_id=tg_reply_id
                )
        else:
            tg_msg = await retry_async(
                _bot_module.tg_app.bot.send_message,
                max_retries=3, delay=2.0, backoff=2.0,
                chat_id=tg_chat_id, text=formatted_msg, parse_mode='HTML', reply_to_message_id=tg_reply_id
            )
            
        if tg_msg:
            database.save_message_map(msg_id, dc_chat_id, tg_msg.message_id, tg_chat_id)
            logger.info(f"Relayed DC msg {msg_id} (DC chat {dc_chat_id}) to TG chat {_bot_module._get_tg_chat_desc(tg_chat_id)} (TG msg {tg_msg.message_id})")
    except Exception as e:
        logger.error(f"Failed to relay DC msg {msg_id} (DC chat {dc_chat_id}) to TG chat {_bot_module._get_tg_chat_desc(tg_chat_id)} after 3 retries: {e}")


async def async_edit_in_tg(tg_chat_id, tg_msg_id, formatted_msg, is_media):
    import bot as _bot_module
    try:
        if is_media:
            try:
                await retry_async(
                    _bot_module.tg_app.bot.edit_message_caption,
                    max_retries=3, delay=2.0, backoff=2.0,
                    chat_id=tg_chat_id, message_id=tg_msg_id, caption=formatted_msg, parse_mode='HTML'
                )
                logger.info(f"Edited media caption in TG chat {_bot_module._get_tg_chat_desc(tg_chat_id)} for TG msg {tg_msg_id}")
            except Exception as cap_err:
                # Fallback to edit_message_text if it wasn't actually a media message on TG
                logger.debug(f"Failed to edit caption, trying text: {cap_err}")
                await retry_async(
                    _bot_module.tg_app.bot.edit_message_text,
                    max_retries=3, delay=2.0, backoff=2.0,
                    chat_id=tg_chat_id, message_id=tg_msg_id, text=formatted_msg, parse_mode='HTML'
                )
                logger.info(f"Edited text in TG chat {_bot_module._get_tg_chat_desc(tg_chat_id)} for TG msg {tg_msg_id} (caption fallback)")
        else:
            try:
                await retry_async(
                    _bot_module.tg_app.bot.edit_message_text,
                    max_retries=3, delay=2.0, backoff=2.0,
                    chat_id=tg_chat_id, message_id=tg_msg_id, text=formatted_msg, parse_mode='HTML'
                )
                logger.info(f"Edited text in TG chat {_bot_module._get_tg_chat_desc(tg_chat_id)} for TG msg {tg_msg_id}")
            except Exception as txt_err:
                logger.debug(f"Failed to edit text, trying caption: {txt_err}")
                await retry_async(
                    _bot_module.tg_app.bot.edit_message_caption,
                    max_retries=3, delay=2.0, backoff=2.0,
                    chat_id=tg_chat_id, message_id=tg_msg_id, caption=formatted_msg, parse_mode='HTML'
                )
                logger.info(f"Edited caption in TG chat {_bot_module._get_tg_chat_desc(tg_chat_id)} for TG msg {tg_msg_id} (text fallback)")
    except Exception as e:
        logger.error(f"Failed to edit message in TG chat {_bot_module._get_tg_chat_desc(tg_chat_id)} (msg {tg_msg_id}) after 3 retries: {e}")


async def _delete_tg_message(tg_chat_id: int, tg_msg_id: int, info_text: str = ""):
    """Delete a message in Telegram via the Bot API, with Userbot fallback."""
    import bot as _bot_module
    
    deleted = False
    if _bot_module.tg_app:
        try:
            await _bot_module.tg_app.bot.delete_message(chat_id=tg_chat_id, message_id=tg_msg_id)
            logger.info(f"DC→TG: Deleted TG msg {tg_msg_id} in {tg_chat_id}{info_text}")
            deleted = True
        except Exception as e:
            e_str = str(e).lower()
            if "not a member" in e_str or "chat not found" in e_str or "message to delete not found" in e_str or "can't be deleted" in e_str:
                logger.debug(f"DC→TG: Bot API failed to delete msg {tg_msg_id} in {tg_chat_id}{info_text}, falling back to Userbot: {e}")
            else:
                logger.warning(f"DC→TG: Could not delete TG msg {tg_msg_id} in {tg_chat_id}{info_text} via Bot API: {e}")

    if not deleted and _bot_module.userbot_client and _bot_module.userbot_client.is_connected():
        try:
            entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(tg_chat_id), timeout=15.0)
            await asyncio.wait_for(_bot_module.userbot_client.delete_messages(entity, [tg_msg_id]), timeout=15.0)
            logger.info(f"DC→TG: Deleted TG msg {tg_msg_id} in {tg_chat_id}{info_text} via Userbot")
        except Exception as e:
            e_str = str(e).lower()
            if "not a member" in e_str or "chat not found" in e_str or "can't be deleted" in e_str or "invalid object id" in e_str or "message to delete not found" in e_str:
                logger.debug(f"DC→TG: Could not delete TG msg {tg_msg_id} in {tg_chat_id}{info_text} via Userbot (no permission/not found): {e}")
            else:
                logger.warning(f"DC→TG: Could not delete TG msg {tg_msg_id} in {tg_chat_id}{info_text} via Userbot either: {e}")


# Forum topic titles, keyed by (tg_chat_id, topic_id) -> (title, fetched_at).
# Kept for an hour so a renamed topic is picked up without asking Telegram
# for every relayed message.
_forum_topic_titles = {}
_FORUM_TOPIC_TTL = 3600
_FORUM_GENERAL_TOPIC_ID = 1


def _get_forum_topic_id(msg):
    """Return the forum topic id a Telethon message belongs to, or None when
    the chat is not a forum. Messages in the "General" topic carry no topic
    reply header, so they are recognised by the chat's forum flag instead."""
    reply_to = getattr(msg, 'reply_to', None)
    if reply_to is not None and getattr(reply_to, 'forum_topic', None) is True:
        topic_id = getattr(reply_to, 'reply_to_top_id', None) or getattr(reply_to, 'reply_to_msg_id', None)
        if isinstance(topic_id, int):
            return topic_id
    chat = getattr(msg, 'chat', None)
    if chat is not None and getattr(chat, 'forum', None) is True:
        return _FORUM_GENERAL_TOPIC_ID
    return None


async def _get_forum_topic_title(client, msg, topic_id):
    """Resolve a forum topic's title through the userbot, caching the result.
    Returns None if the title cannot be fetched."""
    key = (msg.chat_id, topic_id)
    cached = _forum_topic_titles.get(key)
    if cached and time.monotonic() - cached[1] < _FORUM_TOPIC_TTL:
        return cached[0]

    title = None
    try:
        from telethon.tl.functions.messages import GetForumTopicsByIDRequest
        peer = await msg.get_input_chat()
        res = await asyncio.wait_for(client(GetForumTopicsByIDRequest(peer=peer, topics=[topic_id])), timeout=5.0)
        for topic in getattr(res, 'topics', None) or []:
            if getattr(topic, 'id', None) == topic_id:
                title = getattr(topic, 'title', None)
                break
    except Exception as e:
        logger.warning(f"Failed to fetch forum topic {topic_id} title for chat {msg.chat_id}: {e}")

    if not title and topic_id == _FORUM_GENERAL_TOPIC_ID:
        title = "General"
    if title:
        _forum_topic_titles[key] = (title, time.monotonic())
    elif cached:
        return cached[0]
    return title


async def _relay_userbot_message(dc_chat_id, msg, is_edit=False, display_author=None):
    """Core logic to relay a Telethon message (from event or history) to Delta Chat."""
    import bot as _bot_module
    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
        return

    tg_channel_id = msg.chat_id
    grouped_id = getattr(msg, 'grouped_id', None)
    if isinstance(grouped_id, int) and _is_media_group_processed(grouped_id):
        logger.info(f"Userbot: Skipping already processed album post in grouped_id {grouped_id}")
        _update_cached_last_msg_id(tg_channel_id, msg.id)
        return

    raw_text = getattr(msg, 'message', '') or getattr(msg, 'raw_text', '') or getattr(msg, 'text', '') or ""
    entities = getattr(msg, 'entities', []) or []
    text = _format_telegram_entities(raw_text, entities) if entities else raw_text
    
    file_path = None
    media_to_download = None
    rich_post = None
    rich_mode = database.get_rich_mode()

    # Native Telethon RichMessage support (Layer 229+)
    rich_msg = getattr(msg, 'rich_message', None)
    if rich_msg:
        rich_post = await _bot_module._extract_telethon_rich_message(msg, _bot_module.userbot_client, dc_chat_id=dc_chat_id)
        if rich_post:
            if not text and rich_post.text_markdown:
                text = rich_post.text_markdown
            has_rich_content = bool(rich_post.text_markdown.strip() or rich_post.image_urls or rich_post.videos)
            if has_rich_content and rich_mode in ("webxdc", "both") and not is_edit:
                tmp_fd, xdc_path = tempfile.mkstemp(suffix=".xdc")
                os.close(tmp_fd)
                if await _bot_module._package_tg_post_webxdc(rich_post, xdc_path, dc_chat_id=dc_chat_id):
                    file_path = xdc_path
                    clean_title = rich_post.author_name or (f"@{msg.chat.username}" if getattr(msg, 'chat', None) and getattr(msg.chat, 'username', None) else "Telegram")
                    text = f"📰 **{clean_title}**\n\n{rich_post.teaser}" if rich_post.teaser else f"📰 **{clean_title}**"
            elif rich_post.image_urls and not file_path:
                file_path = rich_post.image_urls[0]
            elif rich_post.videos and not file_path and rich_post.videos[0].video_url:
                file_path = rich_post.videos[0].video_url

    # Extract media text and descriptions for Telethon media types
    if msg.media:
        m_type = type(msg.media).__name__
        if m_type == 'MessageMediaWebPage' and not text:
            webpage = msg.media.webpage
            if webpage and type(webpage).__name__ != 'WebPageEmpty':
                parts = []
                if getattr(webpage, 'site_name', None):
                    parts.append(f"🌐 **{webpage.site_name}**")
                if getattr(webpage, 'title', None):
                    parts.append(f"**{webpage.title}**" if not getattr(webpage, 'site_name', None) else webpage.title)
                if getattr(webpage, 'description', None):
                    parts.append(webpage.description)
                if getattr(webpage, 'url', None):
                    parts.append(webpage.url)
                text = "\n\n".join(parts)
        elif m_type == 'MessageMediaPaidMedia':
            stars = getattr(msg.media, 'stars', 0) or 0
            star_str = f" ({stars} ⭐)" if stars else ""
            paid_label = f"⭐ Paid Media{star_str}"
            text = (f"[{paid_label}]\n" + text).strip() if text else f"[{paid_label}]"
        elif m_type == 'MessageMediaStory':
            text = (f"[📖 Story]\n" + text).strip() if text else "[📖 Story]"
        elif m_type in ('MessageMediaGiveaway', 'MessageMediaGiveawayResults'):
            text = (f"[🎁 Giveaway]\n" + text).strip() if text else "[🎁 Giveaway]"
        elif m_type == 'MessageMediaPoll':
            poll = getattr(msg.media, 'poll', None)
            if poll:
                q_text = _format_poll_text(getattr(poll, 'question', 'Poll'))
                poll_text = f"📊 {q_text}\n"
                answers = getattr(poll, 'answers', []) or []
                for ans in answers:
                    ans_text = _format_poll_text(getattr(ans, 'text', ''))
                    poll_text += f"▫️ {ans_text}\n"
                text = (text + "\n\n" + poll_text).strip()
        elif m_type == 'MessageMediaContact':
            c_name = f"{getattr(msg.media, 'first_name', '')} {getattr(msg.media, 'last_name', '')}".strip()
            c_phone = getattr(msg.media, 'phone_number', '')
            text = (text + f"\n\n👤 Contact: {c_name} ({c_phone})").strip()
        elif m_type in ('MessageMediaGeo', 'MessageMediaGeoLive'):
            geo = getattr(msg.media, 'geo', None)
            if geo and getattr(geo, 'lat', None) is not None:
                text = (text + f"\n\n📍 Location: https://maps.google.com/?q={geo.lat},{geo.long}").strip()
        elif m_type == 'MessageMediaUnsupported':
            chat_username = getattr(msg.chat, 'username', None) if getattr(msg, 'chat', None) else None
            rich_mode = database.get_rich_mode()
            if isinstance(chat_username, str) and isinstance(getattr(msg, 'id', None), int):
                rich_post = await _bot_module._extract_public_tg_post_rich(chat_username, msg.id)
                has_rich_content = bool(rich_post and (rich_post.text_markdown.strip() or rich_post.image_urls or rich_post.videos))
                if rich_post and has_rich_content and rich_mode in ("webxdc", "both") and (rich_post.is_rich or (not text and (rich_post.image_urls or rich_post.videos or len(rich_post.text_markdown) > 500))):
                    tmp_fd, xdc_path = tempfile.mkstemp(suffix=".xdc")
                    os.close(tmp_fd)
                    if await _bot_module._package_tg_post_webxdc(rich_post, xdc_path, dc_chat_id=dc_chat_id):
                        file_path = xdc_path
                        clean_title = rich_post.author_name or f"@{chat_username}"
                        text = f"📰 **{clean_title}**\n\n{rich_post.teaser}" if rich_post.teaser else f"📰 **{clean_title}**"
                    else:
                        if os.path.exists(xdc_path):
                            try:
                                os.unlink(xdc_path)
                            except Exception:
                                pass
                        if rich_post.text_markdown and not text:
                            text = rich_post.text_markdown
                        if rich_post.image_urls and not file_path:
                            file_path = await _bot_module._download_image_url(rich_post.image_urls[0])
                elif rich_post and rich_mode == "split" and len(rich_post.image_urls) > 1:
                    if rich_post.text_markdown and not text:
                        text = rich_post.text_markdown
                    if not file_path and rich_post.image_urls:
                        file_path = await _bot_module._download_image_url(rich_post.image_urls[0])
                elif not text and not file_path:
                    extracted_text, extracted_img_url = await _bot_module._extract_public_tg_post(chat_username, msg.id)
                    if extracted_text:
                        text = extracted_text
                    if extracted_img_url and not file_path:
                        file_path = await _bot_module._download_image_url(extracted_img_url)
            if not text and not file_path:
                text = f"[📰 Post with rich formatting / unsupported media — open in Telegram to view: https://t.me/{chat_username}/{msg.id}]" if isinstance(chat_username, str) and chat_username else "[📰 Post with rich formatting / unsupported media — open in Telegram to view]"
        elif not text and m_type not in ('MessageMediaPhoto', 'MessageMediaDocument'):
            text = f"[{m_type}]"

    # Filter out commands in Userbot mode
    if text.startswith('/') and not msg.media:
        return

    # Message content filter check
    filtered, matched_pat = is_text_filtered(text)
    if filtered:
        logger.info(f"Userbot: Skipping message {msg.id} in {tg_channel_id} matching filter '{matched_pat}'")
        _update_cached_last_msg_id(tg_channel_id, msg.id)
        return

    # Base formatted message
    formatted_msg = text
    
    # Add link to original post
    if msg.chat and getattr(msg.chat, 'username', None):
        formatted_msg = (formatted_msg + f"\n\n🔗 t.me/{msg.chat.username}/{msg.id}").strip()

    if not formatted_msg and not msg.media and not file_path:
        return

    chat_username = getattr(msg.chat, 'username', None) if getattr(msg, 'chat', None) else None
    rich_mode = database.get_rich_mode()
    if isinstance(grouped_id, int) and isinstance(chat_username, str) and not file_path and not rich_post and rich_mode in ("webxdc", "both") and not is_edit:
        rich_post = await _bot_module._extract_public_tg_post_rich(chat_username, msg.id)
        if rich_post and (rich_post.is_rich or len(rich_post.image_urls) > 1):
            tmp_fd, xdc_path = tempfile.mkstemp(suffix=".xdc")
            os.close(tmp_fd)
            if await _bot_module._package_tg_post_webxdc(rich_post, xdc_path, dc_chat_id=dc_chat_id):
                file_path = xdc_path
                clean_title = rich_post.author_name or f"@{chat_username}"
                formatted_msg = (f"📰 **{clean_title}**\n\n{rich_post.teaser}\n\n🔗 t.me/{chat_username}/{msg.id}").strip()

    clean_msg = _truncate(formatted_msg, _bot_module.DC_MAX_MSG_LEN)
    is_broadcast_channel = bool(database.get_channel_by_tg_id(tg_channel_id) or database.get_channel_by_dc_chat_id(dc_chat_id) or (getattr(msg, 'is_channel', False) and not getattr(msg, 'is_group', False)))

    old_dc_msg_id = database.get_dc_msg_id(msg.id, tg_channel_id, dc_chat_id) if is_edit else None
    if is_edit and old_dc_msg_id:
        try:
            await asyncio.to_thread(_bot_module.dc_bot_instance.rpc.send_edit_request, _bot_module.dc_accid, old_dc_msg_id, clean_msg)
            c_hash = _get_content_hash(msg)
            database.save_message_map(old_dc_msg_id, dc_chat_id, msg.id, tg_channel_id, content_hash=c_hash)
            _update_cached_last_msg_id(tg_channel_id, msg.id)
            logger.info(f"Userbot: In-place edited {'broadcast channel ' if is_broadcast_channel else ''}post {old_dc_msg_id} for TG msg {msg.id} in DC chat {dc_chat_id}")
            return
        except Exception as edit_err:
            logger.warning(f"Userbot: Could not edit old DC msg {old_dc_msg_id} in-place: {edit_err}")
            if is_broadcast_channel:
                # In broadcast channels, NEVER fall back to sending a new message to avoid duplicates
                c_hash = _get_content_hash(msg)
                database.save_message_map(old_dc_msg_id, dc_chat_id, msg.id, tg_channel_id, content_hash=c_hash)
                _update_cached_last_msg_id(tg_channel_id, msg.id)
                return

    if is_edit and not is_broadcast_channel:
        formatted_msg = f"✏️ [Edited]\n\n{clean_msg}"
    else:
        formatted_msg = clean_msg

    # Note: downloading media with Telethon if needed
    if msg.media and not file_path:
        m_type = type(msg.media).__name__
        if m_type == 'MessageMediaPaidMedia':
            ext_media = getattr(msg.media, 'extended_media', []) or []
            for item in ext_media:
                sub = getattr(item, 'media', None) or getattr(item, 'photo', None) or getattr(item, 'video', None)
                if sub:
                    media_to_download = sub
                    break
        elif m_type not in (
            'MessageMediaWebPage', 'MessageMediaUnsupported', 'MessageMediaPoll',
            'MessageMediaContact', 'MessageMediaGeo', 'MessageMediaGeoLive',
            'MessageMediaStory', 'MessageMediaGiveaway', 'MessageMediaGiveawayResults',
            'MessageMediaEmpty'
        ):
            media_to_download = msg.media
        elif m_type == 'MessageMediaWebPage':
            webpage = msg.media.webpage
            if webpage and type(webpage).__name__ != 'WebPageEmpty' and getattr(webpage, 'photo', None):
                media_to_download = webpage.photo

    if media_to_download:
        media_size = _bot_module._get_media_size(msg)
        m_type = type(media_to_download).__name__
        file_name = getattr(msg.file, 'name', '') if hasattr(msg, 'file') and msg.file else ""
        if not file_name:
            file_name = f"media_{msg.id}" + (getattr(msg.file, 'ext', "") if hasattr(msg, 'file') and msg.file else ".jpg")

        if media_size > _bot_module.MAX_ATTACHMENT_SIZE:
            logger.warning(f"Userbot: Media in channel {tg_channel_id} (msg {msg.id}, file '{file_name}') is too large: {media_size // 1024 // 1024}MB > {_bot_module.MAX_ATTACHMENT_SIZE // 1024 // 1024}MB")
            formatted_msg += f"\n\n[Media is too large to be forwarded (limit {_bot_module.MAX_ATTACHMENT_SIZE // 1024 // 1024}MB)]"
        else:
            suffix = getattr(msg.file, 'ext', "") if hasattr(msg, 'file') and msg.file else ".jpg"
            tmp_fd, file_path_tmp = tempfile.mkstemp(suffix=suffix)
            os.close(tmp_fd)

            async def _do_download_userbot():
                async with _get_download_semaphore():
                    return await asyncio.wait_for(_bot_module.userbot_client.download_media(media_to_download, file=file_path_tmp), timeout=300.0)

            try:
                logger.info(f"Userbot downloading {m_type} '{file_name}' ({media_size // 1024} KB) for channel {tg_channel_id} msg {msg.id}...")
                file_path = await retry_async(_do_download_userbot, max_retries=3, delay=3.0, backoff=2.0)
            except Exception as e:
                logger.error(
                    f"Failed to download userbot media for channel {tg_channel_id} msg {msg.id} "
                    f"('{file_name}', type={m_type}, size={media_size} B) after 3 retries: {e}"
                )
                if os.path.exists(file_path_tmp):
                    try:
                        os.unlink(file_path_tmp)
                    except OSError:
                        pass
                file_path = None
                formatted_msg += f"\n\n[Failed to download media after retries: {file_name}]"

    try:
        msg_data = MsgData(text=formatted_msg)
        
        # Determine author if not provided
        if not display_author:
            if hasattr(msg, 'post_author') and msg.post_author:
                display_author = msg.post_author
            elif msg.is_channel and not msg.is_group:
                # For channels, the sender is the channel itself
                if msg.chat and hasattr(msg.chat, 'title'):
                    display_author = msg.chat.title
                else:
                    ch_info = database.get_channel_by_tg_id(tg_channel_id)
                    if ch_info:
                        try:
                            dc_info = _bot_module.dc_bot_instance.rpc.get_basic_chat_info(_bot_module.dc_accid, ch_info['dc_chat_id'])
                            display_author = dc_info.get("name")
                        except Exception:
                            display_author = None
                    if not display_author:
                        display_author = "Channel"
            else:
                sender = getattr(msg, 'sender', None)
                if not sender:
                    try:
                        sender = await asyncio.wait_for(msg.get_sender(), timeout=3.0)
                    except Exception as e:
                        logger.warning(f"Failed to get sender for userbot message {msg.id}: {e}")
                if sender:
                    if hasattr(sender, 'first_name'):
                        display_author = f"{sender.first_name} {getattr(sender, 'last_name', '') or ''}".strip()
                    elif hasattr(sender, 'title'):
                        display_author = sender.title

        # Forum groups mix all topics into one DC chat, so name the topic
        # after the sender: "Gluek in Soft".
        topic_id = _get_forum_topic_id(msg)
        if topic_id is not None:
            topic_title = await _get_forum_topic_title(_bot_module.userbot_client, msg, topic_id)
            if topic_title:
                display_author = f"{display_author} in {topic_title}" if display_author else topic_title

        if display_author:
            msg_data.override_sender_name = display_author
        
        if file_path:
            msg_data.file = file_path

        await _wait_for_global_dc_rate_limit()
        sent_id = await asyncio.to_thread(_bot_module.dc_bot_instance.rpc.send_msg, _bot_module.dc_accid, dc_chat_id, msg_data)
        if sent_id:
            c_hash = _get_content_hash(msg)
            pids_to_map = set(rich_post.album_post_ids) if (rich_post and rich_post.album_post_ids) else {msg.id}
            pids_to_map.add(msg.id)
            for pid in pids_to_map:
                database.save_message_map(sent_id, dc_chat_id, pid, tg_channel_id, content_hash=c_hash)
            channel_dc_chat_id = _get_cached_dc_channel_chat_id(tg_channel_id)
            if channel_dc_chat_id == dc_chat_id:
                _update_cached_last_msg_id(tg_channel_id, max(pids_to_map))
            if grouped_id:
                database.mark_media_group_processed(grouped_id, tg_channel_id, sent_id)
            if rich_post and len(rich_post.image_urls) > 1 and rich_mode in ("split", "both"):
                start_idx = 1 if rich_mode == "split" else 0
                for idx in range(start_idx, len(rich_post.image_urls)):
                    img_url = rich_post.image_urls[idx]
                    sub_img_path = await _bot_module._download_image_url(img_url)
                    if sub_img_path and os.path.exists(sub_img_path):
                        try:
                            sub_cap = f"📷 [{idx + 1}/{len(rich_post.image_urls)}] {display_author or chat_username or 'Telegram'}\n🔗 t.me/{chat_username}/{msg.id}" if (isinstance(chat_username, str) and chat_username) else f"📷 [{idx + 1}/{len(rich_post.image_urls)}] {display_author or 'Telegram'}"
                            sub_data = MsgData(text=sub_cap, file=sub_img_path)
                            if display_author:
                                sub_data.override_sender_name = display_author
                            await _wait_for_global_dc_rate_limit()
                            await asyncio.to_thread(_bot_module.dc_bot_instance.rpc.send_msg, _bot_module.dc_accid, dc_chat_id, sub_data)
                        except Exception as sub_err:
                            logger.warning(f"Failed to send follow-up userbot image {idx+1} for msg {msg.id}: {sub_err}")
                        finally:
                            try:
                                os.unlink(sub_img_path)
                            except Exception:
                                pass
        logger.info(f"Relayed userbot {'edited ' if is_edit else ''}post from {tg_channel_id} to DC chat {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to relay userbot message: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
            except Exception:
                pass

