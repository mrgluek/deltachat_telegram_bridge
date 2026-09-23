"""Non-command Telegram-side event handlers: channel post relay
(new/edited), group message relay (new/edited), poll/reaction sync,
membership changes, and the shared channel-bridging/history-relay
helpers they call into.

References to the pervasive bot.py singletons and to names the test
suite patches directly on bot.py (_relay_userbot_message,
_extract_public_tg_post_rich, _package_tg_post_webxdc,
_dc_send_msg_with_stats) or that still live in bot.py
(update_tg_channel_stats, DC_MAX_MSG_LEN) go through a function-local
`import bot as _bot_module`, same pattern as the rest of this refactor.

_relay_channel_history now reads/writes the channel-history cache via
caching.get_history_cache/set_history_cache instead of touching the
_history_cache dict directly — the one behavioral cleanup this
extraction was set up to enable (see caching.py).
"""
import asyncio
import html
import logging
import os
import random
import re
import tempfile
import time
from typing import Optional

import database
from deltachat2 import MsgData
from telegram import Update
from telegram.ext import ContextTypes

try:
    from telethon.tl.functions.channels import JoinChannelRequest
except ImportError:
    JoinChannelRequest = None

from security import (
    retry_async,
    is_text_filtered,
    _mark_processed,
    _is_rate_limited,
    _is_edit_debounced,
    _register_bot_initiated_delete,
    _consume_bot_initiated_delete,
    _wait_for_global_dc_rate_limit,
    _edit_timestamps,
)
from live_locations import LIVE_LOCATIONS
from caching import (
    HISTORY_RELAY_COOLDOWN,
    get_history_cache,
    set_history_cache,
    mark_history_cooldown,
    _is_media_group_processed,
    _update_cached_last_msg_id,
    _get_cached_dc_channel_chat_id,
    _get_cached_last_msg_id,
    _invalidate_dc_channel_cache,
    invalidate_channels_cache,
    _get_content_hash,
)
from formatting import _format_telegram_entities, _truncate, _format_poll_text
from media import (
    _extract_public_tg_post,
    _download_image_url,
    _download_via_userbot,
)

logger = logging.getLogger("tg_dc_bridge")


async def _check_invite_permissions(update: Update) -> bool:
    """Check permissions for /invite and /inviteqr commands."""
    chat = update.effective_chat
    user = update.effective_user

    # Permission check
    admin_tg_id = database.get_config("admin_tg_id")
    if chat.type == "private":
        if not admin_tg_id:
            await update.effective_message.reply_text("❌ Bot administrator is not configured yet. Invite generation in private chat is restricted.")
            return False
        if not database.is_owner_or_admin(user.id):
            await update.effective_message.reply_text("❌ Only the bot admin can generate invite links.")
            return False
    else:
        if admin_tg_id:
            if not database.is_owner_or_admin(user.id):
                await update.effective_message.reply_text("❌ Only the bot admin can generate invite links.")
                return False
        else:
            try:
                member = await chat.get_member(user.id)
                if member.status not in ("administrator", "creator"):
                    await update.effective_message.reply_text("❌ Only group admins can generate invite links.")
                    return False
            except Exception as e:
                logger.warning(f"Could not verify group admin permissions for invite: {e}")
                await update.effective_message.reply_text("❌ Could not verify your group admin permissions.")
                return False

    return True


async def _check_channel_admin(update: Update) -> bool:
    """Only the bot owner or sub-admin may manage channels. Must be in private chat."""
    chat = update.effective_chat
    user = update.effective_user
    if chat.type != "private":
        await update.effective_message.reply_text("❌ Channel commands can only be used in a private chat with the bot.")
        return False
    admin_tg_id = database.get_config("admin_tg_id")
    if not admin_tg_id:
        await update.effective_message.reply_text("❌ Bot administrator is not configured yet. Channel management is restricted until configured.")
        return False
    if not database.is_owner_or_admin(user.id):
        await update.effective_message.reply_text("❌ Only the bot admin can manage channels.")
        return False
    return True


async def _relay_channel_history(dc_chat_id: int, tg_channel_id: int, ub_target: any, limit: int = 3, invite_link: str = None):
    """Fetch and relay the last N messages from a TG channel as history, with caching."""
    import bot as _bot_module
    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
        return

    try:
        now = time.time()
        cached = get_history_cache(dc_chat_id)
        
        # Check if we have a fresh cache (under 5 minutes old)
        if cached and (now - cached['timestamp'] < HISTORY_RELAY_COOLDOWN):
            logger.debug(f"Using cached history for DC channel {dc_chat_id}")
            history = cached['messages']
        else:
            # Ensure we have an entity Telethon can work with
            ub_entity = None
            try:
                ub_entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(ub_target), timeout=15.0)
            except Exception as e:
                if invite_link and ("t.me/" in invite_link or "telegram.me/" in invite_link):
                    logger.debug(f"Could not resolve {ub_target} by ID, trying invite link: {e}")
                    try:
                        ub_entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(invite_link), timeout=15.0)
                    except Exception as e2:
                        logger.debug(f"Could not resolve via link either: {e2}")
                else:
                    # If it's not a TG link or no link at all, we can't do more
                    if not invite_link:
                        raise e
                    else:
                        logger.debug(f"Skipping invite link resolution: {invite_link} is not a TG link.")
                        raise e
            
            if not ub_entity:
                return

            logger.info(f"Fetching fresh history for TG {ub_target}...")
            try:
                history = await asyncio.wait_for(_bot_module.userbot_client.get_messages(ub_entity, limit=limit), timeout=15)
            except asyncio.TimeoutError:
                logger.error(f"Timeout fetching fresh history for TG {ub_target}")
                history = None
            if history:
                set_history_cache(dc_chat_id, history)
        
        if history:
            # Reverse to send oldest first
            for msg in reversed(history):
                # Mark as processed to avoid duplicate relay if an event for this message comes in
                _mark_processed(tg_channel_id, msg.id)
                # Small delay to ensure order in DC and avoid rate limits
                await asyncio.sleep(0.5)
                await _bot_module._relay_userbot_message(dc_chat_id, msg)
    except Exception as e:
        logger.error(f"Failed to relay history for TG {ub_target} to DC {dc_chat_id}: {e}")


async def _add_channel_bridge(target: str, creator_tg_id: int | None = None) -> str:
    """Core logic to bridge a TG channel/group. Shared between TG and DC commands."""
    import bot as _bot_module
    
    if not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        return "❌ Error: Delta Chat bot not initialized."

    username = target.strip()
    is_numeric = False
    numeric_id = 0
    
    # Support t.me links
    if "t.me/" in username:
        username = username.split('/')[-1]
    
    if username.startswith('@'):
        username = username[1:]
    
    try:
        numeric_id = int(username)
        is_numeric = True
    except ValueError:
        pass
    
    display_name = f"@{username}" if not is_numeric else f"ID {numeric_id}"
    
    try:
        # 1. Resolve entity
        channel_title = ""
        tg_channel_id = None
        resolved_username = username
        avatar_file = None
        bot_api_ok = False
        
        # Try Bot API first (if it's a public channel or bot is member)
        try:
            chat_arg = numeric_id if is_numeric else f"@{username}"
            tg_chat_info = await _bot_module.tg_app.bot.get_chat(chat_arg)
            if tg_chat_info.type in ("channel", "group", "supergroup"):
                channel_title = tg_chat_info.title or display_name
                tg_channel_id = tg_chat_info.id
                resolved_username = tg_chat_info.username
                if tg_chat_info.photo:
                    avatar_file = await tg_chat_info.photo.get_big_file()
                bot_api_ok = True
        except Exception:
            pass

        # Ensure Userbot also "sees" and joins the channel/bot if possible (needed for history)
        is_bot = False
        if _bot_module.userbot_client and _bot_module.userbot_client.is_connected():
            try:
                # Use resolved username if available, fallback to original input
                ub_arg = numeric_id if is_numeric else (resolved_username or username)
                entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(ub_arg), timeout=15.0)
                is_bot = getattr(entity, 'bot', False)

                if is_bot:
                    # For Telegram bots: activate dialogue with /start
                    try:
                        logger.info(f"Userbot: Starting bot {display_name} with /start...")
                        await asyncio.wait_for(_bot_module.userbot_client.send_message(entity, "/start"), timeout=10.0)
                    except Exception as start_err:
                        logger.debug(f"Userbot: /start to bot failed or ignored: {start_err}")
                elif getattr(entity, 'left', True):
                    if JoinChannelRequest:
                        logger.info(f"Userbot: Joining {channel_title or display_name} for history/relay...")
                        await asyncio.wait_for(_bot_module.userbot_client(JoinChannelRequest(entity)), timeout=15.0)
                
                # If Bot API failed, use Userbot info
                if not bot_api_ok:
                    try:
                        from telethon.utils import get_peer_id
                        tg_channel_id = get_peer_id(entity)
                    except Exception:
                        tg_channel_id = getattr(entity, 'id', None)
                    if is_bot:
                        first_name = getattr(entity, 'first_name', '') or ''
                        last_name = getattr(entity, 'last_name', '') or ''
                        full_name = f"{first_name} {last_name}".strip()
                        channel_title = full_name or getattr(entity, 'title', None) or (f"@{entity.username}" if getattr(entity, 'username', None) else display_name)
                    else:
                        channel_title = getattr(entity, 'title', display_name)
                    resolved_username = getattr(entity, 'username', username)
                    bot_api_ok = True
            except Exception as e:
                if not bot_api_ok:
                    return f"❌ Failed to resolve chat/bot (Userbot error): {html.escape(str(e))}"
                logger.debug(f"Userbot could not resolve/join channel (already resolved by Bot API): {e}")

        if not bot_api_ok:
             return f"❌ Could not resolve <code>{html.escape(display_name)}</code> (Bot API failed and Userbot not connected)."

        # 2. Check if already bridged
        existing = database.get_dc_channel_chat_id(tg_channel_id)
        if existing:
            return f"⚠️ Channel {html.escape(channel_title)} is already bridged to Delta Chat."

        # 3. Create DC Broadcast Group
        dc_chat_id = _bot_module.dc_bot_instance.rpc.create_broadcast(_bot_module.dc_accid, channel_title)
        
        # Avatar copying
        try:
            if avatar_file:
                avatar_path = f"tmp_avatar_{tg_channel_id}.jpg"
                await avatar_file.download_to_drive(custom_path=avatar_path)
                _bot_module.dc_bot_instance.rpc.set_chat_profile_image(_bot_module.dc_accid, dc_chat_id, avatar_path)
                try: os.unlink(avatar_path)
                except: pass
            elif _bot_module.userbot_client:
                photo = await asyncio.wait_for(_bot_module.userbot_client.download_profile_photo(tg_channel_id), timeout=30.0)
                if photo:
                    _bot_module.dc_bot_instance.rpc.set_chat_profile_image(_bot_module.dc_accid, dc_chat_id, photo)
                    try: os.unlink(photo)
                    except: pass
        except Exception as e:
            logger.warning(f"Could not copy avatar for {channel_title}: {e}")

        # 4. Generate Invite Link
        invite_link = _bot_module.dc_bot_instance.rpc.get_chat_securejoin_qr_code(_bot_module.dc_accid, dc_chat_id)
        if invite_link.startswith("OPEN-CHAT:"):
            invite_link = "https://i.delta.chat/#" + invite_link[10:]
        elif invite_link.startswith("OPEN:"):
            invite_link = "https://i.delta.chat/#" + invite_link[5:]

        # 5. Save to DB
        row_id = database.add_channel_by_id(tg_channel_id, dc_chat_id, invite_link, username=resolved_username, created_by_tg_id=creator_tg_id)

        if row_id:
            _invalidate_dc_channel_cache(tg_channel_id)
            invalidate_channels_cache()
            # Register in cooldown so subsequent joins immediately after creation don't trigger history relay again
            mark_history_cooldown(dc_chat_id)
            
            # Relay last 10 messages as history so they are present in the broadcast chat
            # and can be automatically resent to new subscribers by deltachat-core.
            ub_target = resolved_username if resolved_username else tg_channel_id
            await _relay_channel_history(dc_chat_id, tg_channel_id, ub_target, limit=10, invite_link=invite_link)

            # Sync subscriber stats immediately
            try:
                # Use cached username or ID to resolve entity correctly
                ub_target = resolved_username if resolved_username else tg_channel_id
                ub_entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(ub_target), timeout=15.0)
                await _bot_module.update_tg_channel_stats(row_id, ub_entity)
            except Exception as se:
                logger.debug(f"Could not sync stats immediately for new channel #{row_id}: {se}")

            target_type = "Bot" if (is_bot or target.strip().lower().endswith("bot")) else "Channel"
            title_display = f"<b>{html.escape(channel_title)}</b>"
            if resolved_username:
                title_display += f" (@{html.escape(resolved_username)})"
            
            return (
                f"✅ {target_type} {title_display} bridged!\n\n"
                f"📺 DC Channel: <b>{html.escape(channel_title)}</b>\n"
                f"🆔 Channel #{row_id}\n\n"
                f"🔗 Subscribe in Delta Chat:\n{invite_link}"
            )
        else:
            return "❌ Failed to save channel to database (may already exist)."

    except Exception as e:
        logger.error(f"Failed to bridge channel: {e}")
        return "❌ Failed to bridge channel. Please check the channel link or bot permissions."


async def _send_bot_message(target: str, message_text: str) -> str:
    """Send a command or message to a Telegram bot via Userbot."""
    import bot as _bot_module
    if not _bot_module.userbot_client or not _bot_module.userbot_client.is_connected():
        return "❌ Userbot is not running or connected."
    
    target_clean = target.strip()
    if not target_clean or not message_text.strip():
        return "Usage: <code>/botsend @bot_name &lt;message&gt;</code> or <code>/botsend &lt;channel_id&gt; &lt;message&gt;</code>"

    # Support channel DB ID (e.g. channel #5)
    try:
        ch_id = int(target_clean)
        ch = database.get_channel_by_id(ch_id)
        if ch:
            target_clean = ch.get('tg_channel_username') or str(ch.get('tg_channel_id'))
    except ValueError:
        pass

    try:
        entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(target_clean), timeout=15.0)
        await asyncio.wait_for(_bot_module.userbot_client.send_message(entity, message_text), timeout=15.0)
        first_name = getattr(entity, 'first_name', '') or ''
        last_name = getattr(entity, 'last_name', '') or ''
        bot_name = f"{first_name} {last_name}".strip() or getattr(entity, 'title', '') or getattr(entity, 'username', '') or target_clean
        return f"🤖 Sent command to <b>{html.escape(str(bot_name))}</b>:\n<code>{html.escape(message_text)}</code>"
    except Exception as e:
        logger.error(f"Failed to send message to bot {target_clean}: {e}")
        return "❌ Failed to deliver message to bot. Please check bot username and try again."


async def handle_tg_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay Telegram channel posts to corresponding DC broadcast channels."""
    import bot as _bot_module

    post = update.channel_post
    if not post:
        return

    tg_channel_id = post.chat.id
    if _mark_processed(tg_channel_id, post.message_id):
        return

    media_group_id = getattr(post, 'media_group_id', None)
    if media_group_id and _is_media_group_processed(media_group_id):
        logger.info(f"Bot API: Skipping already processed album post {post.message_id} in media_group {media_group_id}")
        _update_cached_last_msg_id(tg_channel_id, post.message_id)
        return
    
    tg_username = post.chat.username

    # Look up by numeric ID first, then by username
    dc_chat_id = _get_cached_dc_channel_chat_id(tg_channel_id)

    if not dc_chat_id and tg_username:
        # First post — resolve username to numeric ID (for channels added by @username)
        ch = database.get_channel_by_tg_username(tg_username)
        if ch:
            if not ch.get('tg_channel_id'):
                database.update_channel_tg_id(tg_username, tg_channel_id)
                _invalidate_dc_channel_cache(tg_channel_id)
            dc_chat_id = ch['dc_chat_id']

    if not dc_chat_id or not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        return

    # De-duplication: check message_map directly
    existing_dc_msg_id = database.get_dc_msg_id(post.message_id, tg_channel_id, dc_chat_id)
    if existing_dc_msg_id:
        logger.info(f"Bot API: Post {post.message_id} in channel {tg_channel_id} already exists in message_map (dc_msg_id={existing_dc_msg_id}). Updating watermark and skipping.")
        _update_cached_last_msg_id(tg_channel_id, post.message_id)
        return

    # De-duplication by sequential message ID
    last_msg_id = _get_cached_last_msg_id(tg_channel_id)
    if last_msg_id > 0 and post.message_id <= last_msg_id:
        logger.info(f"Bot API: Skipping already relayed/old post {post.message_id} in channel {tg_channel_id} (last_msg_id is {last_msg_id})")
        return

    # Rate limit
    if _is_rate_limited(tg_channel_id):
        return

    text = post.text or post.caption or ""
    entities = post.entities or post.caption_entities or []
    text = _format_telegram_entities(text, entities)

    # Author signature (shown for channel posts with signatures enabled)
    author = getattr(post, 'author_signature', None)

    # Detect media
    tg_file = None
    file_name = None
    if getattr(post, 'paid_media', None):
        star_count = getattr(post.paid_media, 'star_count', 0) or 0
        star_str = f" ({star_count} ⭐)" if star_count else ""
        paid_label = f"⭐ Paid Media{star_str}"
        text = (f"[{paid_label}]\n" + text).strip() if text else f"[{paid_label}]"
        
        paid_items = getattr(post.paid_media, 'paid_media', []) or []
        for item in paid_items:
            if getattr(item, 'photo', None) and item.photo:
                tg_file = item.photo[-1]
                file_name = "paid_photo.jpg"
                break
            elif getattr(item, 'video', None) and item.video:
                v = item.video
                tg_file = v
                file_name = getattr(v, 'file_name', None) or "paid_video.mp4"
                break
    elif post.photo:
        tg_file = post.photo[-1]
        file_name = "photo.jpg"
    elif post.video:
        vid = post.video
        tg_file = vid
        file_name = vid.file_name or "video.mp4"
        v_size = getattr(vid, 'file_size', 0) or 0
        if v_size > 20 * 1024 * 1024:
            qualities = getattr(vid, 'qualities', []) or []
            valid_q = [q for q in qualities if (getattr(q, 'file_size', 0) or 0) < 20 * 1024 * 1024]
            if valid_q:
                valid_q.sort(key=lambda q: getattr(q, 'file_size', 0) or getattr(q, 'height', 0) or 0, reverse=True)
                tg_file = valid_q[0]
                text = (text + f"\n\n[Video was too large ({v_size//1024//1024} MB), forwarded in lower resolution]").strip()
            else:
                tg_file = None
                text = (text + f"\n\n[🎥 Video is too large ({v_size//1024//1024} MB) to be forwarded]").strip()
    elif post.animation:
        tg_file = post.animation
        file_name = "animation.mp4"
    elif post.voice:
        tg_file = post.voice
        file_name = "voice.ogg"
    elif post.audio:
        tg_file = post.audio
        file_name = post.audio.file_name or "audio.mp3"
    elif post.document:
        tg_file = post.document
        file_name = post.document.file_name or "file"
    elif post.sticker:
        tg_file = post.sticker
        file_name = "sticker.webp"
    elif post.video_note:
        tg_file = post.video_note
        file_name = "video_note.mp4"

    # Additional message types (Story, Giveaway, Poll, Contact, Invoice)
    if getattr(post, 'story', None):
        story_label = "📖 Story"
        text = (f"[{story_label}]\n" + text).strip() if text else f"[{story_label}]"

    if getattr(post, 'giveaway', None):
        winner_count = getattr(post.giveaway, 'winner_count', None)
        gw_label = f"🎁 Giveaway ({winner_count} winners)" if winner_count else "🎁 Giveaway"
        text = (f"[{gw_label}]\n" + text).strip() if text else f"[{gw_label}]"

    if getattr(post, 'poll', None):
        poll = post.poll
        poll_text = f"📊 {_format_poll_text(poll.question)}\n"
        for option in poll.options:
            poll_text += f"▫️ {_format_poll_text(option.text)}\n"
        text = (text + "\n\n" + poll_text).strip()

    if getattr(post, 'contact', None):
        c = post.contact
        c_name = f"{c.first_name or ''} {c.last_name or ''}".strip()
        c_text = f"👤 Contact: {c_name} ({c.phone_number})"
        text = (text + "\n\n" + c_text).strip()

    if getattr(post, 'invoice', None):
        inv = post.invoice
        inv_text = f"💳 Invoice: {inv.title} ({inv.total_amount} {inv.currency})"
        text = (text + "\n\n" + inv_text).strip()

    # Handle location / venue
    if post.venue:
        loc_text = f"📍 Venue: {post.venue.title}\n{post.venue.address}\nhttps://maps.google.com/?q={post.venue.location.latitude},{post.venue.location.longitude}"
        text = (text + "\n\n" + loc_text).strip()
    elif post.location:
        is_live = getattr(post.location, 'live_period', None) is not None
        if is_live:
            live_mins = post.location.live_period // 60
            loc_text = f"📍 Live Location ({live_mins} min)\nhttps://maps.google.com/?q={post.location.latitude},{post.location.longitude}\n\n(Reply with /locupdate to get the latest coordinates)"
            LIVE_LOCATIONS[post.message_id] = (post.location.latitude, post.location.longitude)
        else:
            loc_text = f"📍 Location: https://maps.google.com/?q={post.location.latitude},{post.location.longitude}"
        text = (text + "\n\n" + loc_text).strip()

    local_file_path = None
    rich_post = None
    rich_mode = database.get_rich_mode()
    is_webxdc_package = False

    # Trigger rich extraction if post is in a media group (album), has paid media, or has no text and no file
    if tg_username and (media_group_id or getattr(post, 'paid_media', None) or (not text and not tg_file)):
        rich_post = await _bot_module._extract_public_tg_post_rich(tg_username, post.message_id)

    has_rich_content = bool(rich_post and (rich_post.text_markdown.strip() or rich_post.image_urls or rich_post.videos))
    if rich_post and has_rich_content and rich_mode in ("webxdc", "both") and (rich_post.is_rich or (not text and not tg_file and (rich_post.image_urls or rich_post.videos or len(rich_post.text_markdown) > 500))):
        tmp_fd, xdc_path = tempfile.mkstemp(suffix=".xdc")
        os.close(tmp_fd)
        if await _bot_module._package_tg_post_webxdc(rich_post, xdc_path, dc_chat_id=dc_chat_id):
            local_file_path = xdc_path
            is_webxdc_package = True
            clean_title = rich_post.author_name or f"@{tg_username}"
            text = f"📰 **{clean_title}**\n\n{rich_post.teaser}" if rich_post.teaser else f"📰 **{clean_title}**"
        else:
            if os.path.exists(xdc_path):
                try:
                    os.unlink(xdc_path)
                except Exception:
                    pass
            if rich_post.text_markdown and not text:
                text = rich_post.text_markdown
            if rich_post.image_urls and not local_file_path and not tg_file:
                local_file_path = await _download_image_url(rich_post.image_urls[0])
    elif rich_post and rich_mode == "split" and len(rich_post.image_urls) > 1:
        if rich_post.text_markdown and not text:
            text = rich_post.text_markdown
        if rich_post.image_urls and not local_file_path and not tg_file:
            local_file_path = await _download_image_url(rich_post.image_urls[0])
    elif not text and not tg_file and tg_username:
        extracted_text, extracted_img_url = await _extract_public_tg_post(tg_username, post.message_id)
        if extracted_text:
            text = extracted_text
        if extracted_img_url and not local_file_path:
            local_file_path = await _download_image_url(extracted_img_url)
        if not text and not local_file_path:
            text = f"[📰 Post with rich formatting / unsupported media — open in Telegram to view: https://t.me/{tg_username}/{post.message_id}]"

    # Skip posts with no text and no media
    if not text and not tg_file and not local_file_path:
        return

    # Message content filter check
    filtered, matched_pat = is_text_filtered(text)
    if filtered:
        logger.info(f"Bot API: Skipping channel post {post.message_id} in channel {tg_channel_id} matching filter '{matched_pat}'")
        _update_cached_last_msg_id(tg_channel_id, post.message_id)
        return

    # Build message text
    formatted_msg = text if text else ""

    # Add link to original Telegram post
    if tg_username:
        formatted_msg = (formatted_msg + f"\n\n🔗 t.me/{tg_username}/{post.message_id}").strip()

    formatted_msg = _truncate(formatted_msg, _bot_module.DC_MAX_MSG_LEN)

    # Download media if present (skip if WebXDC already packaged all media)
    if tg_file and not is_webxdc_package:
        try:
            tg_file_obj = await tg_file.get_file()
            suffix = os.path.splitext(file_name)[1] if file_name else ""
            tmp_fd, local_file_path = tempfile.mkstemp(suffix=suffix)
            os.close(tmp_fd)
            await retry_async(tg_file_obj.download_to_drive, custom_path=local_file_path, max_retries=3, delay=3.0, backoff=2.0)
        except (TimeoutError, asyncio.TimeoutError):
            logger.error(f"Timeout downloading channel {tg_channel_id} post {post.message_id} media '{file_name}' after 3 retries")
            if local_file_path and os.path.exists(local_file_path):
                try:
                    os.unlink(local_file_path)
                except OSError:
                    pass
            local_file_path = None
            formatted_msg += f"\n\n[Failed to download media (timeout after 3 retries): {file_name}]"
        except Exception as e:
            logger.error(f"Failed to download channel {tg_channel_id} post {post.message_id} media '{file_name}' after retries: {e}")
            if local_file_path and os.path.exists(local_file_path):
                try:
                    os.unlink(local_file_path)
                except OSError:
                    pass
            local_file_path = None
            formatted_msg += f"\n\n[Failed to download media: {file_name}]"

    try:
        msg_data = MsgData(text=formatted_msg)
        if author:
            msg_data.override_sender_name = author
        if local_file_path and os.path.exists(local_file_path):
            msg_data.file = local_file_path
        dc_msg_id = await asyncio.to_thread(_bot_module.dc_bot_instance.rpc.send_msg, _bot_module.dc_accid, dc_chat_id, msg_data)
        if dc_msg_id:
            c_hash = _get_content_hash(post)
            pids_to_map = set(rich_post.album_post_ids) if (rich_post and rich_post.album_post_ids) else {post.message_id}
            pids_to_map.add(post.message_id)
            for pid in pids_to_map:
                database.save_message_map(dc_msg_id, dc_chat_id, pid, tg_channel_id, content_hash=c_hash)
            _update_cached_last_msg_id(tg_channel_id, max(pids_to_map))
            if media_group_id:
                database.mark_media_group_processed(media_group_id, tg_channel_id, dc_msg_id)

            # Follow-up images for split or both mode
            if rich_post and len(rich_post.image_urls) > 1 and rich_mode in ("split", "both"):
                start_idx = 1 if rich_mode == "split" else 0
                for idx in range(start_idx, len(rich_post.image_urls)):
                    img_url = rich_post.image_urls[idx]
                    sub_img_path = await _download_image_url(img_url)
                    if sub_img_path and os.path.exists(sub_img_path):
                        try:
                            sub_cap = f"📷 [{idx + 1}/{len(rich_post.image_urls)}] {rich_post.author_name or tg_username}\n🔗 t.me/{tg_username}/{post.message_id}"
                            sub_data = MsgData(text=sub_cap, file=sub_img_path)
                            await asyncio.to_thread(_bot_module.dc_bot_instance.rpc.send_msg, _bot_module.dc_accid, dc_chat_id, sub_data)
                        except Exception as sub_err:
                            logger.warning(f"Failed to send follow-up image {idx+1} for post {post.message_id}: {sub_err}")
                        finally:
                            try:
                                os.unlink(sub_img_path)
                            except Exception:
                                pass
        # Register in edit debounce so link-preview "edits" within 60s are suppressed
        _edit_timestamps[(tg_channel_id, post.message_id)] = time.time()
        
        logger.info(f"Relayed channel post from @{tg_username or tg_channel_id} to DC broadcast {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to relay channel post to DC: {e}")
    finally:
        if local_file_path and os.path.exists(local_file_path):
            try:
                os.unlink(local_file_path)
            except Exception:
                pass


async def handle_tg_edited_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay edited Telegram channel posts to DC broadcast with [Edited] prefix."""
    import bot as _bot_module

    post = update.edited_channel_post
    if not post:
        return

    tg_channel_id = post.chat.id
    media_group_id = getattr(post, 'media_group_id', None)
    if media_group_id and _is_media_group_processed(media_group_id):
        logger.info(f"Bot API: Skipping edit for already processed album post {post.message_id} in media_group {media_group_id}")
        return

    new_hash = _get_content_hash(post)
    if _mark_processed(tg_channel_id, post.message_id, f"edit_{new_hash}"):
        return

    # Check message age (older than 7 days / 1 week)
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        age = now - post.date
        if age.days > 7:
            logger.info(f"Skipping edit relay for post {post.message_id} in channel {tg_channel_id} because it is older than 7 days ({age.days} days).")
            return
    except Exception as e:
        logger.warning(f"Failed to check message age for post {post.message_id}: {e}")
    
    tg_username = post.chat.username

    # Check if this is a live location update
    if post.location:
        is_live = getattr(post.location, 'live_period', None) is not None
        if is_live or post.message_id in LIVE_LOCATIONS:
            lat, lon = post.location.latitude, post.location.longitude
            LIVE_LOCATIONS[post.message_id] = (lat, lon)
            
            if not is_live:
                # Live location ended either manually or expired.
                LIVE_LOCATIONS.pop(post.message_id, None)
                final_text = f"🛑 Live Location Ended\nFinal coordinates: https://maps.google.com/?q={lat},{lon}"
                dc_chat_id = _get_cached_dc_channel_chat_id(tg_channel_id)
                if not dc_chat_id and tg_username:
                    ch = database.get_channel_by_tg_username(tg_username)
                    if ch:
                        dc_chat_id = ch['dc_chat_id']
                if dc_chat_id and _bot_module.dc_bot_instance and _bot_module.dc_accid:
                    try:
                        dc_reply_id = database.get_dc_msg_id(post.message_id, tg_channel_id, dc_chat_id)
                        msg_data = MsgData(text=final_text)
                        if dc_reply_id:
                            msg_data.quoted_message_id = dc_reply_id
                        await asyncio.to_thread(_bot_module.dc_bot_instance.rpc.send_msg, _bot_module.dc_accid, dc_chat_id, msg_data)
                    except Exception as e:
                        logger.error(f"Failed to send final location: {e}")
                return

            if not (post.text or post.caption):
                return

    # Look up DC broadcast
    dc_chat_id = _get_cached_dc_channel_chat_id(tg_channel_id)
    if not dc_chat_id and tg_username:
        ch = database.get_channel_by_tg_username(tg_username)
        if ch:
            dc_chat_id = ch['dc_chat_id']

    if not dc_chat_id or not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        return

    # Filter out updates where content (text/caption) hasn't changed (e.g. reactions or view count updates)
    old_hash = database.get_message_content_hash(post.message_id, tg_channel_id, dc_chat_id)
    if old_hash and old_hash == new_hash:
        # Content hasn't changed, ignore this "edit"
        return

    # Debounce: max 1 edit relay per message per minute
    if _is_edit_debounced(tg_channel_id, post.message_id):
        return

    text = post.text or post.caption or ""
    entities = post.entities or post.caption_entities or []
    text = _format_telegram_entities(text, entities)

    author = getattr(post, 'author_signature', None)

    if not text:
        return

    # Message content filter check
    filtered, matched_pat = is_text_filtered(text)
    if filtered:
        logger.info(f"Bot API: Skipping channel post edit {post.message_id} in channel {tg_channel_id} matching filter '{matched_pat}'")
        return

    formatted_msg = text

    # Add link to original Telegram post
    if tg_username:
        formatted_msg = (formatted_msg + f"\n\n🔗 t.me/{tg_username}/{post.message_id}").strip()

    formatted_msg = _truncate(formatted_msg, _bot_module.DC_MAX_MSG_LEN)

    try:
        old_dc_msg_id = database.get_dc_msg_id(post.message_id, tg_channel_id, dc_chat_id)
        if old_dc_msg_id:
            try:
                await asyncio.to_thread(_bot_module.dc_bot_instance.rpc.send_edit_request, _bot_module.dc_accid, old_dc_msg_id, formatted_msg)
                database.save_message_map(old_dc_msg_id, dc_chat_id, post.message_id, tg_channel_id, content_hash=new_hash)
                _update_cached_last_msg_id(tg_channel_id, post.message_id)
                logger.info(f"Bot API: In-place edited broadcast channel post {post.message_id} (dc_msg_id={old_dc_msg_id}).")
                return
            except Exception as edit_err:
                logger.warning(f"Bot API: In-place edit failed for post {post.message_id} (dc_msg_id={old_dc_msg_id}): {edit_err}")
                database.save_message_map(old_dc_msg_id, dc_chat_id, post.message_id, tg_channel_id, content_hash=new_hash)
                _update_cached_last_msg_id(tg_channel_id, post.message_id)
                return

        # If not relayed yet, relay cleanly as a fresh post (without [Edited] prefix)
        msg_data = MsgData(text=formatted_msg)
        if author:
            msg_data.override_sender_name = author
        dc_sent_id = await asyncio.to_thread(_bot_module.dc_bot_instance.rpc.send_msg, _bot_module.dc_accid, dc_chat_id, msg_data)
        if dc_sent_id:
            database.save_message_map(dc_sent_id, dc_chat_id, post.message_id, tg_channel_id, content_hash=new_hash)
            _update_cached_last_msg_id(tg_channel_id, post.message_id)
        logger.info(f"Relayed edited channel post (as fresh post) from @{tg_username or tg_channel_id} to DC broadcast {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to relay edited channel post to DC: {e}")


async def handle_tg_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay edited Telegram group messages to Delta Chat with [Edited] prefix."""
    import bot as _bot_module

    msg = update.edited_message
    if not msg:
        return

    tg_chat_id = msg.chat.id
    new_hash = _get_content_hash(msg)
    if _mark_processed(tg_chat_id, msg.message_id, f"edit_{new_hash}"):
        return

    # Check message age (older than 7 days / 1 week)
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        age = now - msg.date
        if age.days > 7:
            logger.info(f"Skipping edit relay for message {msg.message_id} in chat/group {tg_chat_id} because it is older than 7 days ({age.days} days).")
            return
    except Exception as e:
        logger.warning(f"Failed to check message age for message {msg.message_id}: {e}")

    # Check if this is a live location update
    if msg.location:
        is_live = getattr(msg.location, 'live_period', None) is not None
        if is_live or msg.message_id in LIVE_LOCATIONS:
            lat, lon = msg.location.latitude, msg.location.longitude
            LIVE_LOCATIONS[msg.message_id] = (lat, lon)
            
            if not is_live:
                # Live location ended
                LIVE_LOCATIONS.pop(msg.message_id, None)
                final_text = f"🛑 Live Location Ended\nFinal coordinates: https://maps.google.com/?q={lat},{lon}"
                dc_chats = database.get_dc_chats(tg_chat_id)
                if dc_chats and _bot_module.dc_bot_instance and _bot_module.dc_accid:
                    for dc_chat_id in dc_chats:
                        dc_reply_id = database.get_dc_msg_id(msg.message_id, tg_chat_id, dc_chat_id)
                        msg_data = MsgData(text=final_text)
                        if dc_reply_id:
                            msg_data.quoted_message_id = dc_reply_id
                        try:
                            await _wait_for_global_dc_rate_limit()
                            _bot_module.dc_bot_instance.rpc.send_msg(_bot_module.dc_accid, dc_chat_id, msg_data)
                        except Exception as e:
                            logger.error(f"Failed to send final location edit: {e}")
                return

            if not (msg.text or msg.caption):
                return

    # Only bridge group chats
    if msg.chat.type == "private":
        return

    # Skip bot messages
    if msg.from_user and msg.from_user.is_bot:
        return

    dc_chats = database.get_dc_chats(tg_chat_id)
    if not dc_chats or not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        return

    # Filter out updates where content (text/caption) hasn't changed
    # We check against the first bridged DC chat; usually hashes are sync'd.
    old_hash = database.get_message_content_hash(msg.message_id, tg_chat_id, dc_chats[0])
    if old_hash and old_hash == new_hash:
        return

    # Debounce: max 1 edit relay per message per minute
    if _is_edit_debounced(tg_chat_id, msg.message_id):
        return

    sender = msg.from_user
    sender_name = sender.first_name
    if sender.last_name:
        sender_name += f" {sender.last_name}"

    text = msg.text or msg.caption or ""
    
    # Filter out commands in edits ONLY if no media
    if text.startswith('/') and not msg.photo and not msg.video and not msg.document:
        return

    entities = msg.entities or msg.caption_entities or []
    text = _format_telegram_entities(text, entities)

    if not text:
        return

    # Message content filter check
    filtered, matched_pat = is_text_filtered(text)
    if filtered:
        logger.info(f"Bot API: Skipping group message edit {msg.message_id} in {tg_chat_id} matching filter '{matched_pat}'")
        return

    formatted_msg = f"✏️ [Edited]:\n{text}"
    formatted_msg = _truncate(formatted_msg, _bot_module.DC_MAX_MSG_LEN)
    clean_msg = _truncate(text, _bot_module.DC_MAX_MSG_LEN)

    for dc_chat_id in dc_chats:
        try:
            # Try to edit in-place first if possible
            old_dc_msg_id = database.get_dc_msg_id(msg.message_id, tg_chat_id, dc_chat_id)
            edit_success = False
            if old_dc_msg_id:
                try:
                    old_msg = _bot_module.dc_bot_instance.rpc.get_message(_bot_module.dc_accid, old_dc_msg_id)
                    old_text = old_msg.get('text') if isinstance(old_msg, dict) else getattr(old_msg, 'text', '')
                    is_info = old_msg.get('isInfo') if isinstance(old_msg, dict) else getattr(old_msg, 'isInfo', False)
                    has_html = old_msg.get('hasHtml') if isinstance(old_msg, dict) else getattr(old_msg, 'hasHtml', False)
                    view_type = old_msg.get('viewType') if isinstance(old_msg, dict) else getattr(old_msg, 'viewType', None)
                    
                    if old_text and not is_info and not has_html and view_type != 'Call':
                        _bot_module.dc_bot_instance.rpc.send_edit_request(_bot_module.dc_accid, old_dc_msg_id, clean_msg)
                        database.save_message_map(old_dc_msg_id, dc_chat_id, msg.message_id, tg_chat_id, content_hash=new_hash)
                        edit_success = True
                        logger.info(f"Edited group msg {old_dc_msg_id} in-place for TG msg {msg.message_id} in chat {dc_chat_id}")
                except Exception as edit_err:
                    logger.debug(f"Could not edit old DC msg {old_dc_msg_id} in-place: {edit_err}")

            if not edit_success:
                if old_dc_msg_id:
                    try:
                        _register_bot_initiated_delete(old_dc_msg_id)
                        _bot_module.dc_bot_instance.rpc.delete_messages(_bot_module.dc_accid, [old_dc_msg_id])
                    except Exception as del_e:
                        logger.debug(f"Could not delete old DC msg {old_dc_msg_id} before edit relay: {del_e}")
                        _consume_bot_initiated_delete(old_dc_msg_id)  # clean up mark on failure

                msg_data = MsgData(text=formatted_msg)
                msg_data.override_sender_name = sender_name
                await _wait_for_global_dc_rate_limit()
                dc_sent_id = _bot_module.dc_bot_instance.rpc.send_msg(_bot_module.dc_accid, dc_chat_id, msg_data)
                if dc_sent_id:
                    database.save_message_map(dc_sent_id, dc_chat_id, msg.message_id, tg_chat_id, content_hash=new_hash)
                logger.info(f"Relayed edited TG msg (new msg) to DC chat {dc_chat_id}")
        except Exception as e:
            logger.error(f"Failed to relay edited msg to DC chat {dc_chat_id}: {e}")


async def handle_tg_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay Telegram messages to Delta Chat."""
    import bot as _bot_module

    if not update.effective_chat or not update.message:
        return

    tg_chat_id = update.effective_chat.id
    if _mark_processed(tg_chat_id, update.message.message_id):
        return

    # Only bridge group chats
    if update.effective_chat.type == "private":
        return

    # Skip messages from bots (including self) to prevent echo loops
    if update.message.from_user and update.message.from_user.is_bot:
        return

    dc_chats = database.get_dc_chats(tg_chat_id)
    if not dc_chats or not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        return

    # Rate limit check
    if _is_rate_limited(tg_chat_id):
        return

    sender = update.message.from_user
    sender_name = sender.first_name
    if sender.last_name:
        sender_name += f" {sender.last_name}"

    text = update.message.text or update.message.caption or ""
    
    # Filter out commands ONLY if it's likely a bot command and not a post with media
    if text.startswith('/') and not update.message.photo and not update.message.video and not update.message.document:
        return

    # Inline hidden links so they are not lost in DC
    entities = update.message.entities or update.message.caption_entities or []
    text = _format_telegram_entities(text, entities)

    # Detect and format polls
    if update.message.poll:
        poll = update.message.poll
        poll_text = f"📊 {_format_poll_text(poll.question)}\n"
        for option in poll.options:
            poll_text += f"▫️ {_format_poll_text(option.text)}\n"
        text = (text + "\n\n" + poll_text).strip()
        
        # Save context so we can update DC when the poll closes
        for dc_chat_id in dc_chats:
            database.save_poll_context(poll.id, tg_chat_id, dc_chat_id)

    # Detect media
    tg_file = None
    file_name = None
    if getattr(update.message, 'paid_media', None):
        star_count = getattr(update.message.paid_media, 'star_count', 0) or 0
        star_str = f" ({star_count} ⭐)" if star_count else ""
        paid_label = f"⭐ Paid Media{star_str}"
        text = (f"[{paid_label}]\n" + text).strip() if text else f"[{paid_label}]"
        
        paid_items = getattr(update.message.paid_media, 'paid_media', []) or []
        for item in paid_items:
            if getattr(item, 'photo', None) and item.photo:
                tg_file = item.photo[-1]
                file_name = "paid_photo.jpg"
                break
            elif getattr(item, 'video', None) and item.video:
                v = item.video
                tg_file = v
                file_name = getattr(v, 'file_name', None) or "paid_video.mp4"
                break
    elif update.message.photo:
        tg_file = update.message.photo[-1]  # Largest resolution
        file_name = "photo.jpg"
    elif update.message.video:
        vid = update.message.video
        tg_file = vid
        file_name = vid.file_name or "video.mp4"
        v_size = getattr(vid, 'file_size', 0) or 0
        if v_size > 20 * 1024 * 1024:
            qualities = getattr(vid, 'qualities', []) or []
            valid_q = [q for q in qualities if (getattr(q, 'file_size', 0) or 0) < 20 * 1024 * 1024]
            if valid_q:
                valid_q.sort(key=lambda q: getattr(q, 'file_size', 0) or getattr(q, 'height', 0) or 0, reverse=True)
                tg_file = valid_q[0]
                text = (text + f"\n\n[Video was too large ({v_size//1024//1024} MB), forwarded in lower resolution]").strip()
            else:
                tg_file = None
                text = (text + f"\n\n[🎥 Video is too large ({v_size//1024//1024} MB) to be forwarded]").strip()
    elif update.message.animation:  # GIF
        tg_file = update.message.animation
        file_name = "animation.mp4"
    elif update.message.voice:
        tg_file = update.message.voice
        file_name = "voice.ogg"
    elif update.message.audio:
        tg_file = update.message.audio
        file_name = update.message.audio.file_name or "audio.mp3"
    elif update.message.document:
        tg_file = update.message.document
        file_name = update.message.document.file_name or "file"
    elif update.message.sticker:
        tg_file = update.message.sticker
        file_name = "sticker.webp"
    elif update.message.video_note:
        tg_file = update.message.video_note
        file_name = "video_note.mp4"

    # Additional message types (Story, Giveaway, Contact, Invoice)
    if getattr(update.message, 'story', None):
        story_label = "📖 Story"
        text = (f"[{story_label}]\n" + text).strip() if text else f"[{story_label}]"

    if getattr(update.message, 'giveaway', None):
        winner_count = getattr(update.message.giveaway, 'winner_count', None)
        gw_label = f"🎁 Giveaway ({winner_count} winners)" if winner_count else "🎁 Giveaway"
        text = (f"[{gw_label}]\n" + text).strip() if text else f"[{gw_label}]"

    if getattr(update.message, 'contact', None):
        c = update.message.contact
        c_name = f"{c.first_name or ''} {c.last_name or ''}".strip()
        c_text = f"👤 Contact: {c_name} ({c.phone_number})"
        text = (text + "\n\n" + c_text).strip()

    if getattr(update.message, 'invoice', None):
        inv = update.message.invoice
        inv_text = f"💳 Invoice: {inv.title} ({inv.total_amount} {inv.currency})"
        text = (text + "\n\n" + inv_text).strip()

    # Handle location / venue
    if update.message.venue:
        loc_text = f"📍 Venue: {update.message.venue.title}\n{update.message.venue.address}\nhttps://maps.google.com/?q={update.message.venue.location.latitude},{update.message.venue.location.longitude}"
        text = (text + "\n\n" + loc_text).strip()
    elif update.message.location:
        is_live = getattr(update.message.location, 'live_period', None) is not None
        if is_live:
            live_mins = update.message.location.live_period // 60
            loc_text = f"📍 Live Location ({live_mins} min)\nhttps://maps.google.com/?q={update.message.location.latitude},{update.message.location.longitude}\n\n(Reply with /locupdate to get the latest coordinates)"
            LIVE_LOCATIONS[update.message.message_id] = (update.message.location.latitude, update.message.location.longitude)
        else:
            loc_text = f"📍 Location: https://maps.google.com/?q={update.message.location.latitude},{update.message.location.longitude}"
        text = (text + "\n\n" + loc_text).strip()

    # Skip messages with no text and no media
    if not text and not tg_file:
        return

    # Message content filter check
    filtered, matched_pat = is_text_filtered(text)
    if filtered:
        logger.info(f"Bot API: Skipping group message {update.message.message_id} in {tg_chat_id} matching filter '{matched_pat}'")
        return

    # Check if this is a reply to another message
    reply_prefix = ""
    tg_reply_to_msg_id = None
    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        tg_reply_to_msg_id = replied.message_id
        replied_text = replied.text or replied.caption or ""
        if replied_text:
            short_quote = _truncate(replied_text, 80)
            reply_prefix = f"↩ {short_quote}\n"

    # Download the TG file to a temp path if present
    local_file_path = None
    timeout_error_text = ""
    if tg_file:
        suffix = os.path.splitext(file_name)[1] if file_name else ""
        f_size = getattr(tg_file, 'file_size', 0) or 0
        
        # If file is potentially too large for Bot API, try Userbot first
        if f_size > 20 * 1024 * 1024:
            local_file_path = await _download_via_userbot(tg_chat_id, update.message.message_id, suffix=suffix)

        if not local_file_path:
            # Fallback (or first attempt if < 20MB) using regular Bot API
            try:
                tg_file_obj = await tg_file.get_file()
                tmp_fd, local_file_path_tmp = tempfile.mkstemp(suffix=suffix)
                os.close(tmp_fd)
                await retry_async(tg_file_obj.download_to_drive, custom_path=local_file_path_tmp)
                local_file_path = local_file_path_tmp
            except (TimeoutError, asyncio.TimeoutError):
                logger.error(f"Timeout downloading file {file_name} after retries")
                local_file_path = None
                timeout_error_text = f"\n\n*[Failed to download media: {html.escape(file_name)} - timeout exceeded after retries]*"
            except Exception as e:
                # If it's the "File is too big" error from Telegram
                if "File is too big" in str(e) and not local_file_path:
                    # Final attempt with Userbot if we haven't tried yet
                    local_file_path = await _download_via_userbot(tg_chat_id, update.message.message_id, suffix=suffix)
                
                if not local_file_path:
                    logger.error(f"Failed to download TG file {file_name}: {e}")
                    local_file_path = None



    try:
        for dc_chat_id in dc_chats:
            try:
                dc_reply_id = None
                if tg_reply_to_msg_id:
                    dc_reply_id = database.get_dc_msg_id(tg_reply_to_msg_id, tg_chat_id, dc_chat_id)

                chat_reply_prefix = reply_prefix if not dc_reply_id else ""
                
                formatted_msg = f"{chat_reply_prefix}{text}" if text else ""
                formatted_msg = _truncate(formatted_msg, _bot_module.DC_MAX_MSG_LEN)
                if timeout_error_text:
                    formatted_msg += timeout_error_text

                msg_data = MsgData(text=formatted_msg)
                msg_data.override_sender_name = sender_name
                if dc_reply_id:
                    msg_data.quoted_message_id = dc_reply_id
                if local_file_path and os.path.exists(local_file_path):
                    msg_data.file = local_file_path
                try:
                    await _wait_for_global_dc_rate_limit()
                    sent_msg_id = _bot_module._dc_send_msg_with_stats(_bot_module.dc_bot_instance, _bot_module.dc_accid, dc_chat_id, msg_data)
                except Exception as e:
                    # If quoting failed because the message was deleted in DC, retry without the quote
                    if "does not exist" in str(e).lower() and "message" in str(e).lower():
                        logger.warning(f"Quoted message {dc_reply_id} not found in DC chat {dc_chat_id}. Retrying without quote.")
                        msg_data.quoted_message_id = None
                        # Add a hint to the text that it was a reply to something now missing
                        if chat_reply_prefix:
                           # Already has the prefix if dc_reply_id was NOT found initially
                           pass
                        else:
                            # We thought we had a dc_reply_id, but it's gone from DC DB.
                            # Add the "↩" prefix back to the text
                            tg_reply_to_msg_id = update.message.reply_to_message.message_id
                            replied = update.message.reply_to_message
                            replied_text = replied.text or replied.caption or ""
                            if replied_text:
                                short_quote = _truncate(replied_text, 80)
                                chat_reply_prefix = f"↩ {short_quote}\n"
                                msg_data.text = _truncate(f"{chat_reply_prefix}{text}" if text else "", _bot_module.DC_MAX_MSG_LEN)
                        
                        await _wait_for_global_dc_rate_limit()
                        sent_msg_id = _bot_module._dc_send_msg_with_stats(_bot_module.dc_bot_instance, _bot_module.dc_accid, dc_chat_id, msg_data)
                    else:
                        raise  # Re-raise other exceptions
                
                if sent_msg_id:
                    c_hash = _get_content_hash(update.message)
                    database.save_message_map(sent_msg_id, dc_chat_id, update.message.message_id, tg_chat_id, content_hash=c_hash)
                # Register in edit debounce so link-preview "edits" within 60s are suppressed
                _edit_timestamps[(tg_chat_id, update.message.message_id)] = time.time()
                logger.info(f"Relayed TG msg to DC chat {dc_chat_id}")
            except Exception as e:
                logger.error(f"Failed to relay msg to DC chat {dc_chat_id}: {e}")
    finally:
        # Clean up temp file
        if local_file_path and os.path.exists(local_file_path):
            try:
                os.unlink(local_file_path)
            except Exception:
                pass


async def handle_tg_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle poll updates (e.g. when a poll is closed)."""
    import bot as _bot_module
    
    poll = update.poll
    if not poll or not poll.is_closed:
        return

    ctx = database.get_poll_context(poll.id)
    if not ctx:
        return
        
    tg_chat_id, dc_chat_id = ctx
    
    # Check if bridged and not rate limited
    if not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        return
    if _is_rate_limited(tg_chat_id):
        return

    # Format final results
    total_voter_count = poll.total_voter_count
    
    q_title = _format_poll_text(poll.question)
    text = f"🏁 **Poll closed:** {q_title}\n\n"
    
    # Sort options by voter count descending
    sorted_options = sorted(poll.options, key=lambda x: x.voter_count, reverse=True)
    
    for option in sorted_options:
        percentage = (option.voter_count / total_voter_count * 100) if total_voter_count > 0 else 0
        ans_text = _format_poll_text(option.text)
        text += f"▫️ {ans_text} — {option.voter_count} votes ({percentage:.1f}%)\n"
        
    text += f"\n*Total votes: {total_voter_count}*"
    
    try:
        msg_data = MsgData(text=text)
        _bot_module._dc_send_msg_with_stats(_bot_module.dc_bot_instance, _bot_module.dc_accid, dc_chat_id, msg_data)
        logger.info(f"Relayed TG poll results to DC chat {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to relay poll results to DC chat {dc_chat_id}: {e}")


async def handle_tg_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay Telegram reactions to Delta Chat."""
    import bot as _bot_module
    if not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        return
        
    reaction_update = update.message_reaction
    if not reaction_update:
        return

    # Ignore events triggered by the bot itself to prevent echo loops
    if reaction_update.user and reaction_update.user.id == context.bot.id:
        return
        
    tg_chat_id = reaction_update.chat.id
    tg_msg_id = reaction_update.message_id
    logger.info(f"TG Reaction update in {tg_chat_id} msg {tg_msg_id} from user {reaction_update.user.id if reaction_update.user else 'unknown'}")
    
    dc_chats = database.get_dc_chats(tg_chat_id)
    if not dc_chats:
        chan_dc_id = _get_cached_dc_channel_chat_id(tg_chat_id)
        if chan_dc_id:
            dc_chats = [chan_dc_id]
        else:
            return
        
    primary_emoji = None
    if reaction_update.new_reaction:
        from telegram import ReactionTypeEmoji
        for r in reaction_update.new_reaction:
            if isinstance(r, ReactionTypeEmoji):
                primary_emoji = r.emoji
                break
                
    for dc_chat_id in dc_chats:
        dc_msg_id = database.get_dc_msg_id(tg_msg_id, tg_chat_id, dc_chat_id)
        if dc_msg_id:
            try:
                # send_reaction expects a list of emojis; empty list to clear
                emoji_list = [primary_emoji] if primary_emoji else []
                await asyncio.to_thread(_bot_module.dc_bot_instance.rpc.send_reaction, _bot_module.dc_accid, dc_msg_id, emoji_list)
                if primary_emoji:
                    if _get_cached_dc_channel_chat_id(tg_chat_id):
                        database.increment_channel_reaction_count(tg_chat_id)
                    else:
                        database.increment_bridge_reaction_count(dc_chat_id, tg_chat_id)
                logger.info(f"Relayed TG reaction '{primary_emoji or '(cleared)'}' to DC chat {dc_chat_id}")
            except Exception as e:
                logger.error(f"Failed to relay TG reaction to DC: {e}")


async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Notify owner and sub-admins when the bot is added as admin to a channel."""
    member_update = update.my_chat_member
    if not member_update:
        return

    chat = member_update.chat
    new_status = member_update.new_chat_member.status if member_update.new_chat_member else None

    # Only interested in becoming admin in channels
    if chat.type != "channel" or new_status != "administrator":
        return

    admin_tg_id = database.get_config("admin_tg_id")
    if not admin_tg_id:
        return  # No admin configured — don't leak info

    channel_title = chat.title or "Unknown"
    channel_id = chat.id
    channel_username = chat.username

    # Check if already bridged
    already = database.get_channel_by_tg_id(channel_id)
    if already:
        return  # Already bridged, no need to notify

    if channel_username:
        msg_text = (
            f"📺 I was added as admin to channel <b>{html.escape(channel_title)}</b> "
            f"(@{html.escape(channel_username)}, ID: <code>{channel_id}</code>).\n\n"
            f"To bridge it:\n"
            f"<code>/channeladd @{html.escape(channel_username)}</code>\n"
            f"or\n"
            f"<code>/channeladd {channel_id}</code>"
        )
    else:
        msg_text = (
            f"📺 I was added as admin to private channel <b>{html.escape(channel_title)}</b> "
            f"(ID: <code>{channel_id}</code>).\n\n"
            f"To bridge it:\n"
            f"<code>/channeladd {channel_id}</code>"
        )

    # Notify owner only (not sub-admins — channels may be private)
    try:
        await context.bot.send_message(chat_id=int(admin_tg_id), text=msg_text, parse_mode='HTML')
        logger.info(f"Notified owner about new channel: {channel_title} ({channel_id})")
    except Exception as e:
        logger.error(f"Failed to notify owner about channel {channel_id}: {e}")


async def handle_tg_migration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle group -> supergroup migration by updating stored chat IDs."""
    msg = update.message
    if not msg:
        return

    old_id = msg.chat.id
    new_id = msg.migrate_to_chat_id
    if not new_id:
        # This is the message in the NEW chat with migrate_from_chat_id
        old_id = msg.migrate_from_chat_id
        new_id = msg.chat.id
        if not old_id:
            return

    logger.info(f"TG group migrated: {old_id} -> {new_id}")
    database.update_bridge_tg_chat_id(old_id, new_id)

