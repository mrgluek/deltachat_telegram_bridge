"""Non-command Delta Chat event handlers: message-send failure/failover
retry, DC->TG relay for new/edited/deleted messages and reactions, info
messages, and the custom command-prefix parser installed on the bot.

on_msg_failed, handle_dc_info_message, handle_dc_message,
handle_dc_message_changed, handle_dc_msg_deleted, and handle_dc_reaction
are registered on dc_cli explicitly in bot.py (via dc_cli.on(...)(handler),
the plain-call equivalent of the @dc_cli.on(...) decorator) rather than
decorated here directly. dc_cli is a decorator target that must exist
before the decoration runs, but it currently still lives in bot.py
(moving to dc_commands.py in a later extraction step) and a decorator
evaluates at module-import time — unlike every other cross-module
reference in this refactor, which is deferred into a function body and
therefore safe to resolve via a late `import bot as _bot_module`, a
decorator can't be deferred that way. Registering explicitly in bot.py,
after both dc_cli and these handler functions already exist, sidesteps
the problem entirely.

Every other reference to the pervasive bot.py singletons and to names
the test suite patches directly on bot.py (or that still live in
bot.py) goes through a function-local `import bot as _bot_module`,
same pattern as the rest of this refactor.
"""
import asyncio
import html
import logging
import os
import re
import time
from typing import Optional

import database
from deltachat2 import MsgData, SystemMessageType
from telegram import ReactionTypeEmoji

from resilience import _message_failover_attempts, resilient_lock
from formatting import TelegramRichPost, get_dc_help_text, _truncate, _format_telegram_entities
from caching import _get_content_hash
from media import _extract_telethon_rich_message
from dc_helpers import async_update_channels_dc, _get_tg_chat_desc
from security import _is_rate_limited, DC_FALLBACK_PATTERN, _consume_bot_initiated_delete, _is_deletion_rate_limited, DELETE_SYNC_MAX, DELETE_SYNC_WINDOW
from relay import async_relay_to_tg, _delete_tg_message

logger = logging.getLogger("tg_dc_bridge")


def _react(bot, accid, msg_id, reaction: str):
    """Set (or clear) a reaction on a Delta Chat message."""
    if not msg_id:
        return
    try:
        bot.rpc.send_reaction(accid, msg_id, [reaction] if reaction else [])
    except Exception as e:
        logger.debug(f"Failed to set reaction on msg {msg_id}: {e}")


async def _async_handle_direct_tg_post(bot, accid: int, dc_chat_id: int, username: str, post_id: int, dc_msg_id: Optional[int] = None):
    """Fetch and deliver a direct Telegram post to a Delta Chat conversation."""
    import bot as _bot_module
    clean_username = str(username).lstrip('@').strip()
    cache_key = f"{clean_username.lower()}/{post_id}"
    try:
        # 1. Periodic cleanup of expired cache entries
        try:
            database.clear_expired_tg_post_cache(86400)
        except Exception:
            pass

        # 2. Check database cache
        cached = database.get_cached_tg_post(cache_key)
        if cached:
            post_type = cached.get("post_type")
            text = cached.get("text") or ""
            file_path = cached.get("file_path")
            if post_type in ("webxdc", "photo") and file_path and os.path.exists(file_path):
                await asyncio.to_thread(_bot_module._dc_send_msg_with_stats, bot, accid, dc_chat_id, MsgData(text=text, file=file_path))
                logger.info(f"Direct TG post @{clean_username}/{post_id}: served from cache ({post_type})")
                _react(bot, accid, dc_msg_id, "☑️")
                return
            elif post_type == "text":
                await asyncio.to_thread(_bot_module._dc_send_msg_with_stats, bot, accid, dc_chat_id, MsgData(text=text))
                logger.info(f"Direct TG post @{clean_username}/{post_id}: served from cache (text)")
                _react(bot, accid, dc_msg_id, "☑️")
                return

        # 3. Cache miss: Fetch post
        rich_post: Optional[TelegramRichPost] = None
        tg_msg = None

        if _bot_module.userbot_client and _bot_module.userbot_client.is_connected():
            try:
                entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(clean_username), timeout=15.0)
                msgs = await asyncio.wait_for(_bot_module.userbot_client.get_messages(entity, ids=post_id), timeout=15.0)
                if msgs:
                    tg_msg = msgs[0] if isinstance(msgs, list) else msgs
            except Exception as ub_err:
                logger.warning(f"Userbot entity/message fetch failed for @{clean_username}/{post_id}: {ub_err}")

        # Native Telethon RichMessage support (Layer 229+)
        if tg_msg and getattr(tg_msg, 'rich_message', None):
            try:
                rich_post = await _extract_telethon_rich_message(tg_msg, _bot_module.userbot_client, dc_chat_id=dc_chat_id)
            except Exception as rm_err:
                logger.warning(f"Failed extracting telethon rich message for @{clean_username}/{post_id}: {rm_err}")

        # Fallback to public web embed extraction
        if not rich_post:
            try:
                rich_post = await _bot_module._extract_public_tg_post_rich(clean_username, post_id)
            except Exception as ex_err:
                logger.warning(f"Public rich post extraction failed for @{clean_username}/{post_id}: {ex_err}")

        # Cache storage directory setup
        db_dir = os.path.dirname(os.path.abspath(database.DB_PATH)) if database.DB_PATH != ":memory:" else "/tmp"
        cache_dir = os.path.join(db_dir, "cache", "posts")
        os.makedirs(cache_dir, exist_ok=True)

        rich_mode = database.get_rich_mode()

        if rich_post:
            clean_title = rich_post.author_name or f"@{clean_username}"
            has_rich_content = bool(rich_post.text_markdown.strip() or rich_post.image_urls or rich_post.videos)
            is_rich = bool(rich_post.is_rich or len(rich_post.image_urls) > 1 or len(rich_post.videos) > 0)

            if has_rich_content and is_rich and rich_mode in ("webxdc", "both"):
                # Package as WebXDC
                xdc_name = f"{clean_username.lower()}_{post_id}.xdc"
                xdc_path = os.path.join(cache_dir, xdc_name)
                if await _bot_module._package_tg_post_webxdc(rich_post, xdc_path, dc_chat_id=dc_chat_id):
                    caption = f"📰 **{clean_title}**\n\n{rich_post.teaser}\n\n🔗 t.me/{clean_username}/{post_id}" if rich_post.teaser else f"📰 **{clean_title}**\n\n🔗 t.me/{clean_username}/{post_id}"
                    database.add_cached_tg_post(cache_key, "webxdc", caption, xdc_path)
                    await asyncio.to_thread(_bot_module._dc_send_msg_with_stats, bot, accid, dc_chat_id, MsgData(text=caption, file=xdc_path))
                    logger.info(f"Direct TG post @{clean_username}/{post_id}: packaged and delivered as WebXDC")
                    _react(bot, accid, dc_msg_id, "☑️")
                    return
                else:
                    if os.path.exists(xdc_path):
                        try:
                            os.unlink(xdc_path)
                        except Exception:
                            pass

            if rich_post.image_urls:
                # Single photo or split mode
                img_url = rich_post.image_urls[0]
                img_name = f"{clean_username.lower()}_{post_id}.jpg"
                img_dest = os.path.join(cache_dir, img_name)
                if await _bot_module._download_image_to_file(img_url, img_dest, max_dim=1280, fmt="JPEG", quality=85):
                    text_body = rich_post.text_markdown.strip()
                    caption = f"📷 **{clean_title}**\n\n{text_body}\n\n🔗 t.me/{clean_username}/{post_id}" if text_body else f"📷 **{clean_title}**\n\n🔗 t.me/{clean_username}/{post_id}"
                    caption = _truncate(caption, _bot_module.DC_MAX_MSG_LEN)
                    database.add_cached_tg_post(cache_key, "photo", caption, img_dest)
                    await asyncio.to_thread(_bot_module._dc_send_msg_with_stats, bot, accid, dc_chat_id, MsgData(text=caption, file=img_dest))
                    logger.info(f"Direct TG post @{clean_username}/{post_id}: delivered as photo")
                    _react(bot, accid, dc_msg_id, "☑️")
                    return

            if rich_post.text_markdown.strip():
                # Plain text post
                text_body = rich_post.text_markdown.strip()
                caption = f"💬 **{clean_title}**\n\n{text_body}\n\n🔗 t.me/{clean_username}/{post_id}"
                caption = _truncate(caption, _bot_module.DC_MAX_MSG_LEN)
                database.add_cached_tg_post(cache_key, "text", caption, None)
                await asyncio.to_thread(_bot_module._dc_send_msg_with_stats, bot, accid, dc_chat_id, MsgData(text=caption))
                logger.info(f"Direct TG post @{clean_username}/{post_id}: delivered as text")
                _react(bot, accid, dc_msg_id, "☑️")
                return

        # Fallback: if rich_post failed or had no content, but tg_msg exists from userbot
        if tg_msg:
            clean_title = getattr(tg_msg.chat, 'title', '') or (f"@{clean_username}" if clean_username else "Telegram")
            raw_text = tg_msg.raw_text or ""
            entities = getattr(tg_msg, 'entities', None)
            formatted_text = _format_telegram_entities(raw_text, entities) if entities else raw_text

            if tg_msg.media and hasattr(_bot_module.userbot_client, 'download_media'):
                media_type = type(tg_msg.media).__name__
                if media_type == 'MessageMediaPhoto':
                    img_name = f"{clean_username.lower()}_{post_id}.jpg"
                    img_dest = os.path.join(cache_dir, img_name)
                    try:
                        downloaded = await asyncio.wait_for(_bot_module.userbot_client.download_media(tg_msg.media, file=img_dest), timeout=60.0)
                        if downloaded and os.path.exists(downloaded):
                            caption = f"📷 **{clean_title}**\n\n{formatted_text}\n\n🔗 t.me/{clean_username}/{post_id}" if formatted_text else f"📷 **{clean_title}**\n\n🔗 t.me/{clean_username}/{post_id}"
                            caption = _truncate(caption, _bot_module.DC_MAX_MSG_LEN)
                            database.add_cached_tg_post(cache_key, "photo", caption, downloaded)
                            await asyncio.to_thread(_bot_module._dc_send_msg_with_stats, bot, accid, dc_chat_id, MsgData(text=caption, file=downloaded))
                            _react(bot, accid, dc_msg_id, "☑️")
                            return
                    except Exception as dl_err:
                        logger.warning(f"Failed downloading userbot media for @{clean_username}/{post_id}: {dl_err}")

            if formatted_text.strip():
                caption = f"💬 **{clean_title}**\n\n{formatted_text}\n\n🔗 t.me/{clean_username}/{post_id}"
                caption = _truncate(caption, _bot_module.DC_MAX_MSG_LEN)
                database.add_cached_tg_post(cache_key, "text", caption, None)
                await asyncio.to_thread(_bot_module._dc_send_msg_with_stats, bot, accid, dc_chat_id, MsgData(text=caption))
                _react(bot, accid, dc_msg_id, "☑️")
                return

        logger.warning(f"Could not extract content for direct post link @{clean_username}/{post_id}")
        _react(bot, accid, dc_msg_id, "❌")
    except Exception as e:
        logger.error(f"Error handling direct Telegram post @{clean_username}/{post_id}: {e}", exc_info=True)
        _react(bot, accid, dc_msg_id, "❌")


def on_msg_failed(bot, accid, event):
    """Handle message sending failures by switching to a backup transport temporarily with backoff."""
    import bot as _bot_module
    try:
        if database.get_config("resilient") == "1":
            return
    except Exception:
        pass

    msg_id = getattr(event, 'msg_id', None)
    if not msg_id:
        return

    try:
        global _message_failover_attempts
        if len(_message_failover_attempts) > 1000:
            _message_failover_attempts.clear()

        # Retrieve or initialize tracking state for this message
        state = _message_failover_attempts.get(msg_id)
        if state is None:
            state = {'count': 0, 'transports': set()}
            _message_failover_attempts[msg_id] = state

        # Stop retrying if we reached the maximum attempt limit (e.g. 10 attempts)
        if state['count'] >= 10:
            return

        state['count'] += 1

        # Retrieve message and verify it is indeed in failed state (state 24)
        try:
            msg_snapshot = bot.rpc.get_message(accid, msg_id)
            msg_state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
            if msg_state != 24:
                return
        except Exception:
            return

        # Fetch chat details to include in logs (checking both snake_case and camelCase key fallbacks)
        chat_id = None
        if isinstance(msg_snapshot, dict):
            chat_id = msg_snapshot.get('chat_id') or msg_snapshot.get('chatId')
        else:
            chat_id = getattr(msg_snapshot, 'chat_id', getattr(msg_snapshot, 'chatId', None))
        
        chat_name = "Unknown"

        if chat_id:
            try:
                chat_info = bot.rpc.get_full_chat_by_id(accid, chat_id)
                if isinstance(chat_info, dict):
                    chat_name = chat_info.get('name', 'Unknown')
                else:
                    chat_name = getattr(chat_info, 'name', 'Unknown')
            except Exception:
                pass

        # Check if it's a permanent E2E encryption failure
        msg_error = msg_snapshot.get('error') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'error', None)
        if msg_error:
            msg_error_lower = msg_error.lower()
            if "encryption" in msg_error_lower or "unencrypted" in msg_error_lower or "шифр" in msg_error_lower or "зашифр" in msg_error_lower:
                bot.logger.warning(
                    f"Permanent E2E encryption failure for message {msg_id} in chat '{chat_name}' (ID: {chat_id}): {msg_error}. "
                    f"Stopping failover attempts immediately."
                )
                return

        # List all configured transports
        try:
            transports = bot.rpc.list_transports(accid)
        except Exception:
            transports = []

        if len(transports) <= 1:
            bot.logger.info(f"Message {msg_id} failed to send, but only {len(transports)} transport(s) configured. Cannot failover.")
            return

        current_addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
        if not current_addr:
            return

        # Find current transport index
        current_idx = -1
        for idx, t in enumerate(transports):
            t_addr = t.get('addr') if isinstance(t, dict) else getattr(t, 'addr', None)
            if t_addr and t_addr.lower() == current_addr.lower():
                current_idx = idx
                break

        if current_idx == -1:
            bot.logger.warning(f"Current transport {current_addr} not found in transports list.")
            current_idx = 0

        # Try to find the next transport
        next_idx = (current_idx + 1) % len(transports)
        next_t = transports[next_idx]
        next_addr = next_t.get('addr') if isinstance(next_t, dict) else getattr(next_t, 'addr', None)

        if not next_addr or next_addr.lower() == current_addr.lower():
            bot.logger.info("No alternative transport available for failover.")
            return

        # Check if we have already tried this transport for this message
        if next_addr.lower() in state['transports']:
            if len(state['transports']) >= len(transports):
                bot.logger.warning(f"All available transports have been tried for message {msg_id}. Stopping failover.")
                return

        state['transports'].add(current_addr.lower())

        # Calculate exponential backoff delay: 5, 10, 20, 40, 80, 160... seconds (max 5 minutes)
        delay = min(300, 5 * (2 ** (state['count'] - 1)))
        bot.logger.warning(
            f"Resilient Failover: Message {msg_id} (Chat: {chat_name}, ID: {chat_id}) failed on {current_addr} (attempt {state['count']}/10). "
            f"Scheduling resend on transport {next_addr} in {delay}s."
        )

        init_addr = current_addr

        # Schedule the resend asynchronously using a non-blocking Timer thread
        def delayed_resend():
            try:
                bot.logger.info(f"Executing scheduled resend for message {msg_id} in chat '{chat_name}' (ID: {chat_id}) on transport {next_addr}...")
                with resilient_lock:
                    # Switch configured_addr to next transport temporarily
                    bot.rpc.set_config(accid, "configured_addr", next_addr)
                    time.sleep(1) # Give core a moment to reconfigure
                    
                    bot.rpc.resend_messages(accid, [msg_id])
                    
                    # Wait up to 10 seconds for the resent message to be delivered/failed
                    start_time = time.time()
                    delivered = False
                    while time.time() - start_time < 10:
                        try:
                            raw_msg = bot.rpc.get_message(accid, msg_id)
                            if raw_msg:
                                from deltachat2 import AttrDict
                                msg_snapshot = AttrDict(raw_msg)
                                state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
                                if state in (26, 28):
                                    bot.logger.info(f"Resilient Failover bg: msg {msg_id} delivered successfully on {next_addr}.")
                                    delivered = True
                                    break
                                if state == 24:
                                    bot.logger.warning(f"Resilient Failover bg: msg {msg_id} failed on {next_addr}.")
                                    break
                        except Exception as poll_err:
                            bot.logger.debug(f"Resilient Failover bg poll error: {poll_err}")
                        time.sleep(0.5)

                    if not delivered:
                        bot.logger.warning(f"Resilient Failover bg: msg {msg_id} did not deliver on {next_addr} within timeout.")

            except Exception as resend_err:
                bot.logger.warning(f"Error executing scheduled resend for message {msg_id} in chat '{chat_name}' (ID: {chat_id}): {resend_err}")
                err_str = str(resend_err).lower()
                if "e2e encryption" in err_str or "encryption" in err_str:
                    bot.logger.warning(f"E2E encryption error detected during resend of msg {msg_id} in chat '{chat_name}'. Stopping further failovers.")
                    try:
                        _message_failover_attempts[msg_id]['count'] = 10
                    except Exception:
                        pass
            finally:
                # Always restore the initial primary transport address!
                try:
                    bot.logger.info(f"Resilient Failover bg: restoring primary transport to {init_addr}")
                    bot.rpc.set_config(accid, "configured_addr", init_addr)
                except Exception as restore_err:
                    bot.logger.error(f"Resilient Failover bg: failed to restore transport to {init_addr}: {restore_err}")

        import threading
        threading.Timer(delay, delayed_resend).start()



    except Exception as e:
        bot.logger.error(f"Error handling message failover for message {msg_id}: {e}")


def handle_dc_info_message(bot, accid, event):
    """Handle Delta Chat system/info messages (like member additions)."""
    import bot as _bot_module
    
    msg = event.msg
    dc_chat_id = msg.chat_id
    
    # Get the system_message_type in a robust way - it can be an int, enum, or string
    smt = msg.system_message_type
    smt_str = str(smt).lower() if smt is not None else ""
    
    # Log every info message to help debug - only at debug level to avoid noise
    logger.debug(f"DC info msg in chat {dc_chat_id}: system_message_type={smt!r} ({smt_str!r})")
    
    # Match member-added/joined events regardless of how the library represents them:
    # - As enum: SystemMessageType.MEMBER_ADDED_TO_GROUP / .MEMBER_JOINED_GROUP
    # - As string: "MemberAddedToGroup", "member_added_to_group", etc.
    # - As int: whatever the underlying value happens to be
    is_member_event = False
    try:
        if smt == SystemMessageType.MEMBER_ADDED_TO_GROUP:
            is_member_event = True
        elif hasattr(SystemMessageType, 'MEMBER_JOINED_GROUP') and smt == SystemMessageType.MEMBER_JOINED_GROUP:
            is_member_event = True
    except Exception:
        pass
    
    # Also match by string representation as a fallback
    if not is_member_event:
        member_keywords = ("memberadded", "member_added", "memberjoined", "member_joined")
        is_member_event = any(kw in smt_str.replace(" ", "").replace("_", "") for kw in
                              ("memberadded", "memberjoined"))
    
    if is_member_event:
        logger.info(f"Member event detected in DC chat {dc_chat_id} (type={smt!r}). History is resent automatically by core.")


# The "🔗 t.me/channel/123" footer the bridge appends to every relayed post.
_BOT_POST_FOOTER_RE = re.compile(r'^\s*🔗\s*t\.me/', re.MULTILINE)


def _find_direct_post_link(text):
    """Return the TG_POST_URL_RE match for a post link a user shared, or None.

    Only full links (https://t.me/channel/123, as copied from Telegram) count.
    Text carrying the bridge's own "🔗 t.me/..." footer is a relayed post being
    forwarded back into a chat, so it is ignored to avoid re-posting it.
    """
    import bot as _bot_module
    if not text or _BOT_POST_FOOTER_RE.search(text):
        return None
    for m in _bot_module.TG_POST_URL_RE.finditer(text):
        if re.match(r'https?://', m.group(0), re.IGNORECASE):
            return m
    return None


def handle_dc_message(bot, accid, event):
    """Relay Delta Chat messages to Telegram."""
    import bot as _bot_module

    msg = event.msg
    dc_chat_id = msg.chat_id
    from_id = msg.from_id

    # Track receiving stats
    try:
        # Use configured_addr (SMTP override) if set, otherwise fallback to main account addr
        addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
        if addr:
            database.increment_transport_received(addr)
    except Exception:
        pass

    # Detect new users in private chats and send help
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, dc_chat_id)
        if chat_info.get("type") == 1: # Private chat
            # Check if we already greeted this contact
            greeted_key = f"greeted_{from_id}"
            if not database.get_config(greeted_key):
                sender_email = bot.rpc.get_contact(accid, from_id).address
                help_text = get_dc_help_text(bot, accid, sender_email, from_id)
                _bot_module._dc_send_msg_with_stats(bot, accid, dc_chat_id, MsgData(text=help_text))
                database.set_config(greeted_key, "1")
    except Exception as e:
        logger.warning(f"Greeting check failed: {e}")

    if bot.has_command(event.command):
        return  # Ignore standard commands
    
    # Handle /channelN and /channelNqr commands for subscriptions
    text = msg.text or ""
    cmd = text.split()[0] if text else ""

    # Handle /channelssyncN and /channelsyncN commands
    if (cmd.startswith("/channelssync") or cmd.startswith("/channelsync")) and any(c.isdigit() for c in cmd):
        if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
            _bot_module._dc_send_msg_with_stats(bot, accid, dc_chat_id, MsgData(text="❌ Only administrators can sync channels."))
            return
        
        import re
        match = re.search(r'(\d+)', cmd)
        if match:
            channel_id = int(match.group(1))
            if _bot_module.main_loop:
                asyncio.run_coroutine_threadsafe(
                    async_update_channels_dc(bot, accid, dc_chat_id, channel_id),
                    _bot_module.main_loop
                )
            return
    
    if cmd.startswith("/channel") and any(c.isdigit() for c in cmd):
        try:
            is_qr = cmd.endswith("qr")
            id_str = cmd[8:-2] if is_qr else cmd[8:]
            if not id_str.isdigit():
                 # Handle cases like /channel123qr where id_str is "123"
                 import re
                 match = re.search(r'(\d+)', cmd[8:])
                 if match:
                     channel_id = int(match.group(1))
                 else:
                     return # Not a valid command
            else:
                channel_id = int(id_str)
                
            ch = database.get_channel_by_id(channel_id)
            if ch:
                # Security Check:
                # 1. If public, everyone can join. 
                # 2. If private, only admin/creator can join.
                is_public = bool(ch.get('tg_channel_username'))
                
                if not is_public:
                    # Check if sender is admin or sub-admin
                    is_admin = _bot_module._is_dc_admin(bot, accid, msg.from_id)
                    
                    # Also check if they are the creator of this bridge (optional but good)
                    # Note: sub-admins are identified by TG ID in the DB, so for DC we mostly rely on admin_dc_email
                    
                    if not is_admin:
                        # Deny access to private channels for regular users
                        _bot_module._dc_send_msg_with_stats(bot, accid, dc_chat_id, MsgData(text=f"❌ Channel #{channel_id} not found."))
                        return

                invite_link = ch.get('invite_link', '')
                if not invite_link:
                    _bot_module._dc_send_msg_with_stats(bot, accid, dc_chat_id, MsgData(text="❌ No invite link available for this channel."))
                    return
                
                if is_qr:
                    import qrcode
                    import tempfile
                    import os
                    
                    qr = qrcode.QRCode(version=1, box_size=10, border=5)
                    qr.add_data(invite_link)
                    qr.make(fit=True)
                    img = qr.make_image(fill_color="black", back_color="white")
                    
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                        tmp_path = tmp.name
                        img.save(tmp_path)
                    
                    title = ch.get('title')
                    if not title:
                         try:
                             chat_info = bot.rpc.get_basic_chat_info(accid, ch['dc_chat_id'])
                             title = chat_info.get("name", "channel")
                         except Exception:
                             title = "channel"
                    
                    # Format caption: Only include t.me link if it exists
                    tg_username = ch.get('tg_channel_username')
                    link_part = f" (t.me/{tg_username})" if tg_username else ""
                    
                    _bot_module._dc_send_msg_with_stats(bot, accid, dc_chat_id, MsgData(
                        text=f"📷 QR Code for **{title}**{link_part}", 
                        file=tmp_path
                    ))
                    
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
                else:
                    title = ch.get('title')
                    if not title:
                         try:
                             chat_info = bot.rpc.get_basic_chat_info(accid, ch['dc_chat_id'])
                             title = chat_info.get("name", "channel")
                         except Exception:
                             title = "channel"
                    
                    tg_username = ch.get('tg_channel_username')
                    link_part = f" (t.me/{tg_username})" if tg_username else ""
                    _bot_module._dc_send_msg_with_stats(bot, accid, dc_chat_id, MsgData(text=f"🔗 Join channel **{title}**{link_part}:\n\n{invite_link}"))
                return
            else:
                _bot_module._dc_send_msg_with_stats(bot, accid, dc_chat_id, MsgData(text=f"❌ Channel #{channel_id} not found."))
                return
        except Exception as e:
            logger.error(f"Error handling /channelN(qr): {e}")
            return

    # Skip bot's own messages to prevent echo loops
    if msg.from_id == 1 or (_bot_module.bot_contact_id and msg.from_id == _bot_module.bot_contact_id):
        return

    # Check for direct Telegram post links (e.g. t.me/channel/123)
    raw_text = (msg.text or "").strip()
    # Anti-loop guard: never process links from bot preview cards or from bot accounts
    if raw_text and not raw_text.startswith(("/", "📰", "🌐", "🤖", "📷", "💬")):
        is_bot_sender = getattr(msg, "is_bot", False) is True
        if not is_bot_sender:
            try:
                c = bot.rpc.get_contact(accid, msg.from_id)
                if getattr(c, "is_bot", False) is True:
                    is_bot_sender = True
            except Exception:
                pass

        if not is_bot_sender:
            tg_post_m = _find_direct_post_link(raw_text)
            if tg_post_m:
                post_username = tg_post_m.group(1)
                post_id_str = tg_post_m.group(2)
                if post_username.lower() != "c" and post_id_str.isdigit():
                    target_post_id = int(post_id_str)
                    logger.info(f"Detected direct Telegram post link for @{post_username}/{target_post_id} in DC chat {dc_chat_id}")
                    # React immediately, synchronously: the async task below may sit queued
                    # behind other work on the shared userbot event loop for a while before
                    # it gets to run its own "⏳" reaction.
                    _react(bot, accid, msg.id, "⏳")
                    if _bot_module.main_loop and _bot_module.main_loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            _bot_module._async_handle_direct_tg_post(bot, accid, dc_chat_id, post_username, target_post_id, msg.id),
                            _bot_module.main_loop
                        )
                    else:
                        try:
                            loop = asyncio.get_event_loop()
                            if loop.is_running():
                                asyncio.create_task(_bot_module._async_handle_direct_tg_post(bot, accid, dc_chat_id, post_username, target_post_id, msg.id))
                            else:
                                loop.run_until_complete(_bot_module._async_handle_direct_tg_post(bot, accid, dc_chat_id, post_username, target_post_id, msg.id))
                        except RuntimeError:
                            asyncio.run(_bot_module._async_handle_direct_tg_post(bot, accid, dc_chat_id, post_username, target_post_id, msg.id))

    # Only relay group messages
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, dc_chat_id)
        if chat_info.get("type") == 1:
            return
    except Exception:
        return

    # Check if this is chat is bridged
    tg_chats = database.get_tg_chats(dc_chat_id)
    if not tg_chats or not _bot_module.tg_app:
        return

    # Rate limit check
    if _is_rate_limited(dc_chat_id):
        return

    try:
        sender_contact = bot.rpc.get_contact(accid, msg.from_id)
        sender_name = msg.override_sender_name or sender_contact.display_name or sender_contact.address
    except Exception:
        sender_name = "Unknown"

    text = msg.text or ""
    text = DC_FALLBACK_PATTERN.sub('', text).strip()

    # Do not relay command messages (starting with '/') to Telegram
    if text.startswith('/'):
        logger.info(f"DC→TG: Suppressed command message starting with slash: '{text[:30]}...'")
        return

    file_path = getattr(msg, 'file', None) or None
    viewtype = getattr(msg, 'viewtype', None) or ''
    is_media = viewtype in ('Image', 'Gif', 'Sticker', 'Video', 'Voice', 'Audio', 'Document', 'File')

    # Skip messages with neither text nor file
    if not text and not file_path and not is_media:
        return

    # HTML-escape both name and text to prevent injection
    safe_name = html.escape(sender_name)
    safe_text = html.escape(text) if text else ""

    # Check if this is a reply to another message
    reply_quote = None
    if hasattr(msg, 'quote') and msg.quote:
        quote_text = msg.quote.get('text', '') if isinstance(msg.quote, dict) else ''
        quote_text = DC_FALLBACK_PATTERN.sub('', quote_text).strip()
        if quote_text:
            short_quote = _truncate(quote_text, 50)
            reply_quote = f"<i>↩ {html.escape(short_quote)}</i>\n"

    # Determine if this is a media message
    is_image = viewtype in ('Image', 'Gif', 'Sticker') or (
        file_path and file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp'))
    )
    is_video = viewtype == 'Video' or (
        file_path and file_path.lower().endswith(('.mp4', '.mov', '.avi', '.mkv'))
    )
    is_voice = viewtype in ('Voice', 'Audio')

    # Send to all mapped Telegram chats
    for tg_chat_id in tg_chats:
        try:
            tg_reply_id = None
            if hasattr(msg, 'quote') and msg.quote:
                quote_msg_id = msg.quote.get('message_id') if isinstance(msg.quote, dict) else None
                if quote_msg_id:
                    tg_reply_id = database.get_tg_msg_id(quote_msg_id, dc_chat_id, tg_chat_id)

            # Build caption/text for this specific chat
            chat_reply_quote = reply_quote if not tg_reply_id else None
            
            if safe_text:
                if chat_reply_quote:
                    formatted_msg = f"{chat_reply_quote}<b>{safe_name}</b>: {safe_text}"
                else:
                    formatted_msg = f"<b>{safe_name}</b>: {safe_text}"
            else:
                formatted_msg = f"<b>{safe_name}</b>"
            formatted_msg = _truncate(formatted_msg, _bot_module.TG_MAX_MSG_LEN)

            if _bot_module.main_loop:
                asyncio.run_coroutine_threadsafe(
                    async_relay_to_tg(tg_chat_id, dc_chat_id, msg.id, file_path, formatted_msg, tg_reply_id, is_image, is_video, is_voice, viewtype),
                    _bot_module.main_loop
                )
        except Exception as e:
            bot.logger.error(f"Error scheduling relay of DC msg {msg.id} to TG chat {tg_chat_id}: {e}")


def handle_dc_message_changed(bot, accid, event):
    """Detect when a Delta Chat message is edited and edit it on Telegram."""
    import bot as _bot_module
    if not _bot_module.tg_app or not _bot_module.main_loop:
        return

    msg_id = getattr(event, 'msg_id', None)
    chat_id = getattr(event, 'chat_id', None)
    if not msg_id or not chat_id:
        return

    # Look up all TG mappings for this message
    mappings = database.get_tg_mappings_with_hash_by_dc_msg_id(msg_id)
    if not mappings:
        return

    try:
        msg = bot.rpc.get_message(accid, msg_id)
    except Exception as e:
        logger.error(f"Failed to load DC message {msg_id} during edit check: {e}")
        return

    # Only process if it has actually been edited
    is_edited = msg.get('is_edited') if isinstance(msg, dict) else getattr(msg, 'is_edited', False)
    if not is_edited:
        return

    # Check if the content changed
    new_hash = _get_content_hash(msg)

    # We will build and dispatch edits for each mapped Telegram chat
    for mapping in mappings:
        tg_msg_id, tg_chat_id, dc_chat_id, created_at, old_hash = mapping

        # 1. Do not sync edits backwards to Telegram for bridged channels / bots (channels are read-only feeds)
        if database.get_channel_by_dc_chat_id(dc_chat_id) or database.get_channel_by_tg_id(tg_chat_id):
            continue

        # 2. For group bridges: verify that the bridge is still active
        active_tg_chats = database.get_tg_chats(dc_chat_id)
        if tg_chat_id not in active_tg_chats:
            continue

        # If content hash is the same, no edit needed
        if old_hash and old_hash == new_hash:
            continue

        # Re-build format of message
        try:
            sender_contact = bot.rpc.get_contact(accid, msg.from_id)
            sender_name = msg.override_sender_name or sender_contact.display_name or sender_contact.address
        except Exception:
            sender_name = "Unknown"

        text = msg.text or ""
        text = DC_FALLBACK_PATTERN.sub('', text).strip()

        # Do not relay command messages
        if text.startswith('/'):
            continue

        safe_name = html.escape(sender_name)
        safe_text = html.escape(text) if text else ""

        # Check if reply quote is needed
        reply_quote = None
        if hasattr(msg, 'quote') and msg.quote:
            quote_text = msg.quote.get('text', '') if isinstance(msg.quote, dict) else getattr(msg.quote, 'text', '')
            quote_text = DC_FALLBACK_PATTERN.sub('', quote_text).strip()
            if quote_text:
                short_quote = _truncate(quote_text, 50)
                reply_quote = f"<i>↩ {html.escape(short_quote)}</i>\n"

        tg_reply_id = None
        if hasattr(msg, 'quote') and msg.quote:
            quote_msg_id = msg.quote.get('message_id') if isinstance(msg.quote, dict) else getattr(msg.quote, 'message_id', None)
            if quote_msg_id:
                tg_reply_id = database.get_tg_msg_id(quote_msg_id, dc_chat_id, tg_chat_id)
        chat_reply_quote = reply_quote if not tg_reply_id else None

        if safe_text:
            if chat_reply_quote:
                formatted_msg = f"{chat_reply_quote}<b>{safe_name}</b>: {safe_text}"
            else:
                formatted_msg = f"<b>{safe_name}</b>: {safe_text}"
        else:
            formatted_msg = f"<b>{safe_name}</b>"
        formatted_msg = _truncate(formatted_msg, _bot_module.TG_MAX_MSG_LEN)

        file_path = getattr(msg, 'file', None) or None
        viewtype = getattr(msg, 'viewtype', None) or ''
        is_media = viewtype in ('Image', 'Gif', 'Sticker', 'Video', 'Voice', 'Audio', 'Document', 'File') or file_path is not None

        # Update database with new hash first to prevent duplicate edit events
        database.save_message_map(msg_id, dc_chat_id, tg_msg_id, tg_chat_id, content_hash=new_hash)

        # Dispatch async edit call
        asyncio.run_coroutine_threadsafe(
            _bot_module.async_edit_in_tg(tg_chat_id, tg_msg_id, formatted_msg, is_media),
            _bot_module.main_loop
        )


def handle_dc_msg_deleted(bot, accid, event):
    """Sync DC message deletion to Telegram."""
    import bot as _bot_module
    if not _bot_module.tg_app or not _bot_module.main_loop:
        return

    try:
        msg_id = getattr(event, 'msg_id', None)
        if not msg_id:
            return

        # If the bot itself initiated this deletion (e.g. replacing an old edit copy)
        # skip it entirely — no need to sync back to TG, no rate-limit charge.
        if _consume_bot_initiated_delete(msg_id):
            database.delete_message_map_entry_by_dc(msg_id, None)
            return

        # Look up all TG messages mapped to this DC message
        tg_mappings = database.get_tg_mappings_by_dc_msg_id(msg_id)
        if not tg_mappings:
            return

        # Determine deletion retention threshold to ignore auto-cleanup deletions
        delete_after = os.environ.get("DELETE_DEVICE_AFTER", "604800")
        try:
            delete_after_seconds = int(delete_after)
        except ValueError:
            delete_after_seconds = 604800

        for mapping in tg_mappings:
            tg_msg_id, tg_chat_id, dc_chat_id = mapping[0], mapping[1], mapping[2]
            created_at = mapping[3] if len(mapping) > 3 else None

            # If the mapping has a timestamp, check if its age is close to or exceeds
            # the retention period. If so, skip deletion as it was likely triggered by DC core.
            if delete_after_seconds > 0 and created_at is not None:
                age = time.time() - created_at
                # Use a small 60-second buffer to handle processing delay
                if age >= (delete_after_seconds - 60):
                    logger.info(
                        f"DC→TG: Skipping deletion sync for TG msg {tg_msg_id} in {tg_chat_id}. "
                        f"Message age ({int(age)}s) is close to/exceeds retention threshold ({delete_after_seconds}s)."
                    )
                    continue

            # Clean up the mapping immediately to prevent echo loops
            database.delete_message_map_entry_by_dc(msg_id, None)

            # 1. Verify if the bridge is still active
            # For channels:
            if database.get_channel_by_dc_chat_id(dc_chat_id):
                 # Do not sync deletions from DC to TG for channels (users are just subscribers)
                 continue
            
            # For group bridges:
            active_tg_chats = database.get_tg_chats(dc_chat_id)
            if tg_chat_id not in active_tg_chats:
                logger.info(f"Skipping DC→TG deletion sync: bridge for DC chat {dc_chat_id} to TG {tg_chat_id} no longer exists.")
                continue

            # Try to get chat title, author and text for better debugging
            chat_title = f"Chat {dc_chat_id}"
            extra_info = ""
            try:
                chat_info = bot.rpc.get_basic_chat_info(accid, dc_chat_id)
                if chat_info and chat_info.get('name'):
                    chat_title = chat_info['name']
            except Exception:
                pass
                
            try:
                # Try to fetch message info (might be gone already)
                msg_obj = bot.rpc.get_message(accid, msg_id)
                if msg_obj:
                    author = getattr(msg_obj, 'from_id', 'Unknown')
                    text = getattr(msg_obj, 'text', '')
                    if text:
                        extra_info = f" | Author: {author} | Text: '{_truncate(text, 50)}'"
                    else:
                        extra_info = f" | Author: {author} (Media/Other)"
            except Exception:
                pass

            if _is_deletion_rate_limited():
                logger.warning(
                    f"DC→TG deletion sync rate limit hit ({DELETE_SYNC_MAX}/{DELETE_SYNC_WINDOW}s). "
                    f"Skipping deletion of TG msg {tg_msg_id} in {tg_chat_id} (DC: '{chat_title}', msg {msg_id}{extra_info})."
                )
                # Notify admin
                admin_tg_id = database.get_config("admin_tg_id")
                if admin_tg_id and _bot_module.tg_app and _bot_module.main_loop:
                    asyncio.run_coroutine_threadsafe(
                        _bot_module.tg_app.bot.send_message(
                            chat_id=int(admin_tg_id),
                            text=(
                                f"⚠️ <b>Bulk deletion blocked</b>\n"
                                f"More than {DELETE_SYNC_MAX} messages deleted in {DELETE_SYNC_WINDOW}s.\n"
                                f"<b>Chat:</b> {html.escape(chat_title)}\n"
                                f"<b>Message Info:</b> {html.escape(extra_info.strip(' | ') or 'Not available')}\n"
                                f"DC msg {msg_id} → TG msg {tg_msg_id} in chat {tg_chat_id} was <b>NOT</b> deleted on Telegram."
                            ),
                            parse_mode='HTML'
                        ),
                        _bot_module.main_loop
                    )
                return

            asyncio.run_coroutine_threadsafe(
                _delete_tg_message(tg_chat_id, tg_msg_id, f" (DC: '{chat_title}'{extra_info})"),
                _bot_module.main_loop
            )

    except Exception as e:
        logger.error(f"Error in DC msg deletion handler: {e}")


def handle_dc_reaction(bot, accid, event):
    """Relay Delta Chat reactions to Telegram."""
    import bot as _bot_module
    if not _bot_module.tg_app or not _bot_module.main_loop:
        return
        
    try:
        # Ignore events triggered by the bot itself to prevent echo loops
        contact_id = getattr(event, 'contact_id', None)
        # Only return if both exist and match
        if _bot_module.bot_contact_id and contact_id and str(contact_id) == str(_bot_module.bot_contact_id):
            return

        msg_id = getattr(event, 'msg_id', None)
        if not msg_id:
            return

        dc_chat_id = bot.rpc.get_message(accid, msg_id).chat_id
        
        bot.logger.info(f"DC Reaction event for msg {msg_id} in DC chat {dc_chat_id} from contact {contact_id}")
            
        try:
            dc_reactions = bot.rpc.get_message_reactions(accid, msg_id)
        except Exception:
            dc_reactions = None
            
        tg_mappings = database.get_tg_mappings_by_dc_msg_id(msg_id)
        if not tg_mappings:
            return
            
        # get_message_reactions returns:
        # {'reactions': [{'count': N, 'emoji': '👍', 'is_from_self': True}], 'reactions_by_contact': {'1': ['👍']}}
        primary_emoji = None
        if dc_reactions and hasattr(dc_reactions, 'reactions') and dc_reactions.reactions:
            primary_emoji = dc_reactions.reactions[0].emoji
        else:
            bot.logger.info(f"No reactions found for DC msg {msg_id}")
                
        for mapping in tg_mappings:
            tg_msg_id, tg_chat_id, dc_chat_id = mapping[0], mapping[1], mapping[2]
            # Verify if bridge is still active
            is_channel = database.get_channel_by_dc_chat_id(int(dc_chat_id)) or database.get_channel_by_tg_id(int(tg_chat_id))
            is_group = int(tg_chat_id) in database.get_tg_chats(int(dc_chat_id))
            
            if not is_channel and not is_group:
                logger.info(f"Skipping DC→TG reaction sync: bridge for DC chat {dc_chat_id} to TG {_get_tg_chat_desc(tg_chat_id)} no longer exists.")
                continue

            try:
                if is_channel:
                    # Channels and bridged bots are one-way broadcast feeds.
                    # Reactions placed by DC subscribers are tracked locally in DB stats and not relayed to TG.
                    if primary_emoji:
                        database.increment_channel_reaction_count(int(tg_chat_id))
                    logger.info(f"Recorded DC reaction '{primary_emoji}' for channel post {tg_msg_id} in {_get_tg_chat_desc(tg_chat_id)}")
                else:
                    # For bidirectional group chats, relay reaction to TG
                    reaction = [ReactionTypeEmoji(primary_emoji)] if primary_emoji else []
                    asyncio.run_coroutine_threadsafe(
                        _bot_module.tg_app.bot.set_message_reaction(chat_id=tg_chat_id, message_id=tg_msg_id, reaction=reaction),
                        _bot_module.main_loop
                    )
                    if primary_emoji:
                        database.increment_bridge_reaction_count(int(dc_chat_id), int(tg_chat_id))
            except Exception as e:
                bot.logger.error(f"Failed to relay DC reaction to TG chat {_get_tg_chat_desc(tg_chat_id)}: {e}")
    except Exception as e:
        bot.logger.error(f"Error handling DC reaction: {e}")


def setup_custom_command_parser(bot, allowed_prefixes):
    original_parse_command = bot._parse_command

    def custom_parse_command(accid: int, event) -> None:
        text = event.msg.text
        if not text:
            original_parse_command(accid, event)
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0]
        
        if "@" in cmd:
            cmd_name, suffix = cmd.split("@", 1)
            suffix_lower = suffix.lower()
            
            if suffix_lower:
                try:
                    self_address = bot.rpc.get_contact(accid, 1).address.lower()
                except Exception:
                    self_address = ""
                
                matched = False
                for p in allowed_prefixes:
                    if suffix_lower.startswith(p.lower()) or p.lower().startswith(suffix_lower):
                        matched = True
                        break
                if not matched and self_address and suffix_lower == self_address:
                    matched = True
                
                if matched:
                    new_text = cmd_name
                    if len(parts) > 1:
                        new_text += " " + parts[1]
                    
                    original_text = event.msg.text
                    event.msg["text"] = new_text
                    try:
                        original_parse_command(accid, event)
                    finally:
                        event.msg["text"] = original_text
                else:
                    event.command = ""
                    event.payload = ""
            else:
                original_parse_command(accid, event)
        else:
            original_parse_command(accid, event)
            
            if event.command in ("/help", "/stats"):
                try:
                    chat = bot.rpc.get_chat(accid, event.msg.chat_id)
                    is_group = getattr(chat, "chat_type", "Single") != "Single"
                except Exception:
                    is_group = False
                
                if is_group:
                    try:
                        contacts = bot.rpc.get_chat_contacts(accid, event.msg.chat_id)
                        bot_count = 0
                        for contact_id in contacts:
                            if contact_id == 1:
                                bot_count += 1
                                continue
                            c = bot.rpc.get_contact(accid, contact_id)
                            if getattr(c, "is_bot", False):
                                bot_count += 1
                                if bot_count > 1:
                                    break
                        if bot_count > 1:
                            event.command = ""
                            event.payload = ""
                    except Exception:
                        pass

    bot._parse_command = custom_parse_command

