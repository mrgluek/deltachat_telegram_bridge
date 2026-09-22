import asyncio
import html
import json
import logging
import os
import tempfile
import time
import threading
import random
from collections import defaultdict
from typing import Optional
from dataclasses import dataclass, field
import hashlib
import zipfile
import shutil
import queue

from deltachat2 import EventType, MsgData, SystemMessageType, events
from deltabot_cli import BotCli

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, MessageReactionHandler, ChatMemberHandler
from telegram import ReactionTypeEmoji
from telegram.error import NetworkError, TimedOut

try:
    from telethon import TelegramClient, events as tg_events
    from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
    from telethon.errors import ChannelPrivateError
except ImportError:
    TelegramClient = None
    tg_events = None
    JoinChannelRequest = None
    LeaveChannelRequest = None
    ImportChatInviteRequest = None
    CheckChatInviteRequest = None
    ChannelPrivateError = None


import database
import io
import sys
import getpass
import re
try:
    import qrcode
except ImportError:
    qrcode = None

TG_POST_URL_RE = re.compile(
    r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?([a-zA-Z0-9_]{3,32})/(\d+)',
    re.IGNORECASE
)

# Initialize logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("tg_dc_bridge")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Global tracker for Userbot background tasks

# In-memory cache for /channels command report (TTL: 10 minutes)


import runtime_patches
runtime_patches.apply()
from runtime_patches import _safe_telethon_reconnect, _custom_unraisablehook

from formatting import (
    TelegramRichVideo,
    TelegramRichPost,
    TG_POST_WEBXDC_HTML_TEMPLATE,
    TG_WEBXDC_VIDEO_MAX_BYTES,
    _truncate,
    _utf16_to_py_indices,
    _format_telegram_entities,
    _format_poll_text,
    _clean_toml_string,
    _format_paragraph_html,
    _make_teaser,
    _is_safe_telegram_url,
    _clean_html_for_webxdc,
    _rich_text_to_markdown,
    _rich_text_to_html,
    _largest_real_photo_size,
    _process_page_blocks,
    _inline_links,
    get_dc_help_text,
    get_tg_help_text,
    to_dc_markdown,
)
from security import (
    DC_FALLBACK_PATTERN,
    RATE_LIMIT_WINDOW,
    RATE_LIMIT_MAX,
    _reload_filter_cache,
    is_text_filtered,
    GLOBAL_DC_RATE_LIMIT,
    GLOBAL_DC_RATE_WINDOW,
    _global_dc_send_times,
    _global_dc_rate_limit_lock,
    _wait_for_global_dc_rate_limit,
    _processed_tg_msgs,
    DELETE_SYNC_MAX,
    DELETE_SYNC_WINDOW,
    _deletion_sync_times,
    _deletion_sync_lock,
    _bot_initiated_dc_deletes,
    _bot_initiated_dc_deletes_lock,
    _register_bot_initiated_delete,
    _consume_bot_initiated_delete,
    _is_deletion_rate_limited,
    _mark_processed,
    retry_async,
    _rate_limits,
    _is_rate_limited,
    EDIT_DEBOUNCE_SECONDS,
    _edit_timestamps,
    _is_edit_debounced,
)
from caching import (
    invalidate_channels_cache,
    _channels_cache,
    _CHANNELS_CACHE_TTL,
    _get_cached_dc_channel_chat_id,
    _invalidate_dc_channel_cache,
    _tg_channel_dc_id_cache,
    _tg_channel_dc_id_lock,
    _get_cached_last_msg_id,
    _update_cached_last_msg_id,
    _warm_last_msg_id_cache,
    _last_msg_id_cache,
    _last_msg_id_cache_lock,
    _clear_dc_caches,
    _history_cooldowns,
    _history_cache,
    HISTORY_RELAY_COOLDOWN,
    _is_history_on_cooldown,
    _is_media_group_processed,
    _processed_media_groups,
    _get_content_hash,
    files_are_identical,
)
from live_locations import LIVE_LOCATIONS, get_live_location, set_live_location, clear_live_location
from rpc_proxy import RpcProxy, _make_rpc_thread_safe, _get_download_semaphore
from relay import async_relay_to_tg, async_edit_in_tg, _delete_tg_message, _relay_userbot_message
from dc_helpers import (
    _get_tg_chat_desc,
    async_update_channels_dc,
    _get_contact_fingerprint,
    _parse_chat_info_is_private,
    _is_private_chat,
    _is_dc_admin,
    _dc_send_msg_with_stats,
    _userbot_leave_chat,
)
from media import (
    _get_media_size,
    _get_ptb_media_size,
    _make_square_icon,
    _generate_fallback_bridge_icon,
    _generate_fallback_telegram_icon,
    _get_bot_self_avatar_path,
    _get_channel_avatar_path,
    _download_image_to_file,
    _download_video_with_limit,
    _download_image_url,
    _download_via_userbot,
    _package_tg_post_webxdc,
    _extract_public_tg_post_rich,
    _extract_public_tg_post,
    _resolve_full_res_photos_for_group,
    _extract_telethon_rich_message,
)
from logging_setup import (
    PollingErrorFilter,
    _TRANSIENT_POLLING_ERRORS,
    _get_cached_admin_targets,
    _admin_dc_worker_loop,
    _enqueue_admin_dc_message,
    _send_admin_dc_message_bg,
    AdminLogHandler,
    admin_handler,
    _reported_inaccessible_channels,
    _handle_channel_access_revoked,
    TelethonBanLogHandler,
)
from resilience import resilient_lock, _setup_resilient_mode, _message_failover_attempts
from userbot import (
    _userbot_tasks,
    _channel_queues,
    _channel_workers,
    _is_starting_userbot,
    _is_syncing_userbot,
    _resolve_userbot_entity,
    reconcile_channel,
    run_channel_catchup,
    reconcile_channels_loop,
    sync_userbot_channels,
    _process_userbot_deletion_internal,
    _queue_userbot_event,
    _channel_queue_worker,
    _process_userbot_deletion,
    _process_userbot_event,
    _process_userbot_event_internal,
    start_userbot,
)
from tg_events import (
    _check_invite_permissions,
    _check_channel_admin,
    _relay_channel_history,
    _add_channel_bridge,
    _send_bot_message,
    handle_tg_channel_post,
    handle_tg_edited_channel_post,
    handle_tg_edited_message,
    handle_tg_message,
    handle_tg_poll,
    handle_tg_reaction,
    handle_my_chat_member,
    handle_tg_migration,
)
from tg_commands import (
    tg_start_command,
    tg_help_command,
    tg_donate_command,
    tg_id_command,
    tg_stats_command,
    tg_status_command,
    tg_bridge_command,
    tg_unbridge_command,
    tg_adminadd_command,
    tg_adminremove_command,
    tg_admins_command,
    tg_invite_command,
    tg_inviteqr_command,
    tg_botsend_command,
    tg_channeladd_command,
    tg_catchup_command,
    tg_userbotsync_command,
    tg_userbotjoin_command,
    tg_groups_command,
    tg_channels_command,
    tg_channel_command,
    tg_channelqr_command,
    tg_channelremove_command,
    tg_filters_command,
    tg_filteradd_command,
    tg_filterdel_command,
    tg_cleanup_command,
)









# Limits
TG_MAX_MSG_LEN = 4000   # Telegram limit is 4096; leave margin
DC_MAX_MSG_LEN = 10000   # Practical DC limit
MAX_ATTACHMENT_SIZE = int(os.environ.get("MAX_ATTACHMENT_SIZE_MB", "50")) * 1024 * 1024

db_lock = threading.Lock()

# Initialize DeltaBot CLI
import collections

dc_cli = BotCli("tgbridge")
USERBOT_SESSION_PATH = os.environ.get("USERBOT_SESSION_PATH", "userbot_session")

from dc_events import (
    _react,
    _async_handle_direct_tg_post,
    on_msg_failed,
    handle_dc_info_message,
    handle_dc_message,
    handle_dc_message_changed,
    handle_dc_msg_deleted,
    handle_dc_reaction,
    setup_custom_command_parser,
)
# Registered explicitly (rather than via @dc_cli.on(...) decorators in
# dc_events.py) because dc_cli is a decorator target that must exist at
# decoration time, and dc_cli currently lives here in bot.py — a decorator
# can't defer its evaluation the way every other cross-module reference in
# this refactor does via a function-local `import bot`. This is the plain
# function-call equivalent of `@dc_cli.on(...)` applied to each handler.
dc_cli.on(events.RawEvent(events.EventType.MSG_FAILED))(on_msg_failed)
dc_cli.on(events.NewMessage(is_info=True))(handle_dc_info_message)
dc_cli.on(events.NewMessage(is_info=False))(handle_dc_message)
dc_cli.on(events.RawEvent(EventType.MSGS_CHANGED))(handle_dc_message_changed)
dc_cli.on(events.RawEvent(EventType.MSG_DELETED))(handle_dc_msg_deleted)
dc_cli.on(events.RawEvent(events.EventType.REACTIONS_CHANGED))(handle_dc_reaction)


# Global references
tg_app: Optional[Application] = None
dc_bot_instance = None
dc_accid = None
main_loop = None
bot_contact_id = None  # To detect and skip own messages
userbot_client = None
VERSION = "2.24.12"


# In-memory channel ID cache to reduce DB queries on incoming posts





# Global rate limiting for Delta Chat (e.g. chatmail limits)



# Double bridging protection

# Cooldown for channel history relay (per DC chat_id)

# Cache for channel history messages (per DC chat_id)
# Stores: {dc_chat_id: {"timestamp": float, "messages": list[TelethonMessage]}}

# Cache for channel last message IDs

# Deletion sync safety: max deletions per window to avoid accidental bulk-delete

# Set of DC message IDs deleted by the bot itself (e.g. old version replaced by edit).
# These are exempt from the rate limit so that edit-replacements never block real deletions.












# Simple per-chat rate limiter


# Per-message edit debounce: tracks last edit relay time per (chat_id, msg_id)

















































































# ---------------------------------------------------------
# DELTA CHAT HANDLERS
# ---------------------------------------------------------







@dc_cli.on_init
def on_init(bot, args):
    """Called when the Delta Chat bot starts."""
    _make_rpc_thread_safe(bot)
    bot.logger.info("Initializing Delta Chat tgbridge...")
    _setup_resilient_mode(bot)
    
    # Ensure our error handler is attached to the bot's own logger
    bot.logger.addHandler(admin_handler)
    
    for accid in bot.rpc.get_all_account_ids():
        displayname = os.environ.get("DISPLAY_NAME")
        if not displayname and os.path.exists("/data/options.json"):
            try:
                with open("/data/options.json", "r", encoding="utf-8") as f:
                    opts = json.load(f)
                    displayname = opts.get("display_name", "").strip()
            except Exception:
                pass
        if not displayname:
            displayname = database.get_config("bot_displayname") or "TG Bridge"
        bot.rpc.set_config(accid, "displayname", displayname)

        status_text = os.environ.get("STATUS_TEXT")
        if not status_text and os.path.exists("/data/options.json"):
            try:
                with open("/data/options.json", "r", encoding="utf-8") as f:
                    opts = json.load(f)
                    status_text = opts.get("status_text", "").strip()
            except Exception:
                pass
        if not status_text:
            status_text = "I bridge Telegram and Delta Chat groups. Send /help for commands."
        bot.rpc.set_config(accid, "selfstatus", status_text)
        
        # Configure local message retention policy (in seconds, default: 7 days)
        delete_after = os.environ.get("DELETE_DEVICE_AFTER", "604800")
        bot.rpc.set_config(accid, "delete_device_after", delete_after)
        avatar_path = database.get_config("bot_avatar_path")
        if avatar_path:
            if not os.path.isabs(avatar_path):
                base_dir = os.path.dirname(os.path.abspath(__file__))
                avatar_path = os.path.join(base_dir, avatar_path)
            if os.path.exists(avatar_path):
                bot.rpc.set_config(accid, "selfavatar", avatar_path)
            else:
                bot.logger.warning(f"Avatar file not found: {avatar_path}")
        else:
            try:
                base_dir = os.path.dirname(os.path.abspath(__file__))
                icon_path = os.path.join(base_dir, "icon_deltachat.jpg")
                if not os.path.exists(icon_path):
                    icon_path = os.path.join(base_dir, "icon_deltachat.png")
                if os.path.exists(icon_path):
                    bot.rpc.set_config(accid, "selfavatar", icon_path)
            except Exception as e:
                bot.logger.warning(f"Could not set avatar: {e}")











@dc_cli.on(events.NewMessage(command="/setprimary"))
def setprimary_command(bot, accid, event):
    """Set a specific transport as primary. Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /setprimary."))
        return

    addr = event.payload.strip()
    if not addr:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: /setprimary user@example.com"))
        return

    try:
        bot.rpc.set_config(accid, "configured_addr", addr)
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Primary address (`configured_addr`) is now `{addr}`."))
    except Exception as e:
        logger.error(f"Failed to set primary address: {e}")
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to set primary address."))

@dc_cli.on(events.NewMessage(command="/resilient"))
def resilient_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /resilient."))
        return

    arg = event.payload.strip().lower() if event.payload else ""

    try:
        current = database.get_config("resilient") == "1"
        if not arg:
            status = "enabled" if current else "disabled"
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"ℹ️ Resilient sending mode is currently {status}."))
            return

        if arg in ("on", "1", "true"):
            database.set_config("resilient", "1")
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="✅ Resilient sending mode enabled. Each outgoing message will be sent via all connected transports."))
        elif arg in ("off", "0", "false"):
            database.set_config("resilient", "0")
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="ℹ️ Resilient sending mode disabled."))
        else:
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Invalid argument. Use '/resilient on', '/resilient off', or '/resilient' to get status."))
    except Exception as e:
        logger.error(f"Failed to update resilient mode: {e}")
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to update resilient mode."))

@dc_cli.on(events.NewMessage(command="/richmode"))
def richmode_command(bot, accid, event):
    """Configure Telegram rich post handling mode (webxdc, split, both, off)."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /richmode."))
        return

    arg = event.payload.strip().lower() if event.payload else ""

    try:
        current = database.get_rich_mode()
        if not arg:
            status_text = (
                f"ℹ️ Current Telegram rich post mode: **{current}**\n\n"
                f"Options:\n"
                f"• `/richmode webxdc` — Package rich posts, articles & albums into standalone WebXDC apps (Recommended) 📦\n"
                f"• `/richmode split` — Send text with first photo, extra photos as separate messages 📷\n"
                f"• `/richmode both` — Send WebXDC app and also send extra photos separately\n"
                f"• `/richmode off` — Disable rich post packaging (legacy fallback)"
            )
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=status_text))
            return

        if arg in ("webxdc", "split", "both", "off"):
            database.set_rich_mode(arg)
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Telegram rich post relay mode set to **{arg}**."))
        else:
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Invalid mode. Use `/richmode webxdc`, `/richmode split`, `/richmode both`, or `/richmode off`."))
    except Exception as e:
        logger.error(f"Failed to update richmode: {e}")
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to update richmode."))

@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    """Reply with help text."""
    msg = event.msg
    
    # Get sender info
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    
    help_msg = get_dc_help_text(bot, accid, sender_email, msg.from_id)
    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=help_msg))

@dc_cli.on(events.NewMessage(command="/initadmin"))
def initadmin_command(bot, accid, event):
    """Claim bot ownership in private chat (binds email & cryptographic fingerprint)."""
    msg = event.msg
    if not _is_private_chat(bot, accid, msg.chat_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ For security reasons, /initadmin can only be used in a private 1:1 chat with the bot."))
        return

    admin_email = database.get_admin_email()
    admin_fp = database.get_admin_fingerprint()

    if admin_email or admin_fp:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Admin is already set. Use `set_admin.py` on the server to change."))
        return

    contact = bot.rpc.get_contact(accid, msg.from_id)
    email = contact.address
    database.set_admin_email(email)

    fp = _get_contact_fingerprint(bot, accid, msg.from_id, contact=contact)
    if fp:
        first_fp = fp.split(',')[0]
        database.set_admin_fingerprint(first_fp)
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=
            f"✅ You are now the admin!\n\nEmail: `{email}`\nFingerprint: `{first_fp[-8:]}`"))
    else:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=
            f"✅ You are now the admin!\n\nEmail: `{email}`\n⚠️ Fingerprint not available yet (will be used after key exchange)."))


@dc_cli.on(events.NewMessage(command="/transports"))
def transports_command(bot, accid, event):
    """Show configured transports (mail relays) and their status."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /transports."))
        return

    try:
        transports = bot.rpc.list_transports(accid)
    except Exception as e:
        logger.error(f"Failed to list transports: {e}")
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to list transports."))
        return

    if not transports:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="No transports configured."))
        return

    # Get connectivity status
    connectivity_label = "❓ Unknown"
    try:
        connectivity = bot.rpc.get_connectivity(accid)
        if connectivity >= 4000:
            connectivity_label = "🟢 Connected"
        elif connectivity >= 3000:
            connectivity_label = "🔄 Working"
        elif connectivity >= 2000:
            connectivity_label = "🟡 Connecting"
        else:
            connectivity_label = "🔴 Not connected"
    except Exception:
        pass

    # Get connectivity HTML to parse per-transport status
    connectivity_html = ""
    try:
        connectivity_html = bot.rpc.get_connectivity_html(accid)
    except Exception:
        pass

    # Get resilient sending mode status
    resilient_on = False
    try:
        resilient_on = database.get_config("resilient") == "1"
    except Exception:
        pass

    # Get per-transport statistics
    stats_map = {}
    for s in database.get_all_transport_stats():
        stats_map[s['addr']] = s

    active_addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
    transport_addrs = []
    for t in transports:
        addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
        transport_addrs.append(addr)

    reply = f"🔌 **Mail Relays (Transports)**\n\nStatus: {connectivity_label}\n\n"

    import re
    for addr in transport_addrs:
        # Determine status label from HTML
        status_label = "❓ Unknown"
        if connectivity_html:
            domain = addr.split('@')[-1] if '@' in addr else addr
            pattern = rf'class="([^"]+)\s+dot".*?<b>{re.escape(domain)}:</b>\s*([^<]+)'
            match = re.search(pattern, connectivity_html, re.IGNORECASE)
            if match:
                color = match.group(1).lower()
                status_text = match.group(2).strip().lower()
                if "yellow" in color or "connecting" in status_text:
                    status_label = "🟡 Connecting"
                elif "green" in color:
                    status_label = "🔄 Working"
                elif "red" in color or "lost" in status_text or "error" in status_text:
                    status_label = "🔴 Not connected"

        is_used = resilient_on or (addr == active_addr)
        used_str = " ✔︎ Used for sending:" if is_used else ":"
        reply += f"**{status_label}**{used_str} `{addr}`\n"

        stats = stats_map.get(addr)
        if stats:
            reply += f"  📤 Sent: {stats['msgs_sent']}  📥 Received: {stats['msgs_received']}\n"
            if stats.get('last_sent_at'):
                import datetime
                last_sent = datetime.datetime.fromtimestamp(stats['last_sent_at']).strftime('%Y-%m-%d %H:%M')
                reply += f"  Last sent: {last_sent}\n"
            if stats.get('last_received_at'):
                import datetime
                last_recv = datetime.datetime.fromtimestamp(stats['last_received_at']).strftime('%Y-%m-%d %H:%M')
                reply += f"  Last received: {last_recv}\n"
        else:
            reply += f"  📤 Sent: 0  📥 Received: 0\n"
        reply += "\n"

    reply += f"Total transports: {len(transport_addrs)}"
    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=reply))

@dc_cli.on(events.NewMessage(command="/addtransport"))
def addtransport_command(bot, accid, event):
    """Add a backup mail relay (transport). Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /addtransport."))
        return

    if not _is_private_chat(bot, accid, msg.chat_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ For security reasons, /addtransport can only be used in a private 1:1 chat with the bot."))
        return

    payload = event.payload.strip()
    if not payload:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(
            text="Usage:\n"
                 "/addtransport DCACCOUNT:server.example\n"
                 "/addtransport user@example.com password123"
        ))
        return

    try:
        if payload.startswith("DCACCOUNT:"):
            bot.rpc.add_transport_from_qr(accid, payload)
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Backup transport added via chatmail URI."))
        else:
            parts = payload.split(None, 1)
            if len(parts) < 2:
                _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(
                    text="❌ For email accounts, provide both address and password:\n"
                         "/addtransport user@example.com password123"
                ))
                return
            addr, password = parts[0], parts[1]
            bot.rpc.add_or_update_transport(accid, {"addr": addr, "password": password})
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Backup transport `{addr}` added."))
    except Exception as e:
        logger.error(f"Failed to add transport: {e}")
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to add transport."))

@dc_cli.on(events.NewMessage(command="/rmtransport"))
def rmtransport_command(bot, accid, event):
    """Remove a mail relay (transport). Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /rmtransport."))
        return

    addr = event.payload.strip()
    if not addr:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: /rmtransport user@example.com"))
        return

    try:
        transports = bot.rpc.list_transports(accid)
        transport_addrs = []
        for t in transports:
            a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
            transport_addrs.append(a)
        if len(transport_addrs) <= 1:
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Cannot remove the last transport."))
            return
        if addr not in transport_addrs:
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"❌ Transport `{addr}` not found."))
            return
    except Exception as e:
        logger.error(f"Failed to check transports: {e}")
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to check transports."))
        return

    try:
        bot.rpc.delete_transport(accid, addr)
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Transport `{addr}` removed."))
    except Exception as e:
        logger.error(f"Failed to remove transport: {e}")
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to remove transport."))

@dc_cli.on(events.NewMessage(command="/donate"))
def dc_donate_command(bot, accid, event):
    """Reply with donate link."""
    msg = event.msg
    support_msg = (
        "❤️ Support Bot Development\n\n"
        "If you find this bridge useful, you can support its development and server costs here:\n\n"
        "☕️ Ko-fi: https://ko-fi.com/gluek (🌍 world cards, paypal, no commissions)\n"
        "🚀 Tribute: https://web.tribute.tg/d/IWb (🇷🇺 russian cards, SBP, high commissions)\n\n"
        "Thank you! 🙏"
    )
    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=support_msg))


@dc_cli.on(events.NewMessage(command="/channeladd"))
def dc_channeladd_command(bot, accid, event):
    """Add a channel bridge from Delta Chat. Admin only."""
    msg = event.msg
    payload = event.payload.strip()
    
    # Admin check
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can manage channels."))
        return

    if not payload:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: /channeladd @username or t.me link"))
        return

    # Use run_coroutine_threadsafe since dc_cli hooks might run in a separate thread
    async def run_add():
        status_id = _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="⏳ Processing channel bridge..."))
        result = await _add_channel_bridge(payload)
        # Convert HTML response to Markdown for DC
        result_md = to_dc_markdown(result)
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=result_md))
    
    if main_loop:
        asyncio.run_coroutine_threadsafe(run_add(), main_loop)
    else:
        logger.error("Main loop not found, cannot run channeladd")

def _notify_and_remove_channel_bridge(ch: dict) -> int | None:
    """Send unbridge notice to DC channel, remove from DB, clear caches, and leave TG chat."""
    global dc_bot_instance, dc_accid, main_loop
    ch_id = ch.get('id')
    dc_chat_id = ch.get('dc_chat_id')
    tg_channel_id = ch.get('tg_channel_id')

    # 1. Notify Delta Chat Broadcast Channel before unbridging
    if dc_chat_id and dc_bot_instance and dc_accid:
        try:
            dc_notice = (
                "⚠️ **Channel Disconnected**\n"
                "This broadcast channel has been unbridged from Telegram by the administrator and will no longer receive updates."
            )
            dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, MsgData(text=dc_notice))
        except Exception as e:
            logger.debug(f"Could not send unbridge notice to DC chat {dc_chat_id}: {e}")

    # 2. Remove channel from DB
    removed_tg_id = database.remove_channel(ch_id)
    if removed_tg_id:
        if dc_chat_id:
            _clear_dc_caches(dc_chat_id)
        if tg_channel_id:
            _invalidate_dc_channel_cache(tg_channel_id)
        invalidate_channels_cache()
        # 3. Trigger Userbot leave in background
        if main_loop and main_loop.is_running():
            asyncio.run_coroutine_threadsafe(_userbot_leave_chat(removed_tg_id), main_loop)
    return removed_tg_id

@dc_cli.on(events.NewMessage(command="/channelremove"))
@dc_cli.on(events.NewMessage(command="/channeldelete"))
def dc_channelremove_command(bot, accid, event):
    """Remove a channel bridge from Delta Chat. Admin only."""
    msg = event.msg
    payload = event.payload.strip()
    
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can manage channels."))
        return

    if not payload:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: /channelremove N (channel number)"))
        return

    try:
        channel_id = int(payload)
        ch = database.get_channel_by_id(channel_id)
        if not ch:
             _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"❌ Channel #{channel_id} not found."))
             return
             
        tg_channel_id = _notify_and_remove_channel_bridge(ch)
        if tg_channel_id:
             _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Channel bridge #{channel_id} removed."))
        else:
             _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to remove channel #{channel_id}."))
    except ValueError:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Invalid channel number."))

@dc_cli.on(events.NewMessage(command="/botsend"))
def dc_botsend_command(bot, accid, event):
    """Send a command or message to a Telegram bot via Userbot. Admin only."""
    msg = event.msg
    payload = event.payload.strip()

    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /botsend."))
        return

    parts = payload.split(maxsplit=1)
    if len(parts) < 2:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: /botsend @bot_username <message> or /botsend <channel_id> <message>"))
        return

    target, cmd_text = parts[0], parts[1]

    async def run_botsend():
        res = await _send_bot_message(target, cmd_text)
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=to_dc_markdown(res)))

    if main_loop:
        asyncio.run_coroutine_threadsafe(run_botsend(), main_loop)
    else:
        logger.error("Main loop not found, cannot run botsend")

@dc_cli.on(events.NewMessage(command="/filters"))
def dc_filters_command(bot, accid, event):
    """List all active message filters. Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can manage filters."))
        return

    filters = database.get_all_filters()
    if not filters:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="📋 **Message Filters**\nNo message filters configured.\n\nTo add a filter:\n`/filteradd <word or phrase>`"))
        return

    lines = [f"📋 **Message Filters ({len(filters)})**:"]
    for f in filters:
        lines.append(f"{f['id']}. `{f['pattern']}`")
    lines.append("\nTo add: `/filteradd <word or phrase>`")
    lines.append("To remove: `/filterdel <number or phrase>`")
    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="\n".join(lines)))

@dc_cli.on(events.NewMessage(command="/filteradd"))
def dc_filteradd_command(bot, accid, event):
    """Add a message filter. Admin only."""
    msg = event.msg
    payload = event.payload.strip()
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can manage filters."))
        return

    if not payload:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: `/filteradd <word or phrase>`\nExample: `/filteradd #реклама`"))
        return

    row_id = database.add_filter(payload)
    if row_id is not None:
        _reload_filter_cache()
        clean = payload.strip().strip('"\'')
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Filter added (#{row_id}): `{clean}`"))
    else:
        clean = payload.strip().strip('"\'')
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"⚠️ Filter `{clean}` already exists or is invalid."))

@dc_cli.on(events.NewMessage(command="/filterdel"))
@dc_cli.on(events.NewMessage(command="/filterremove"))
def dc_filterdel_command(bot, accid, event):
    """Remove a message filter. Admin only."""
    msg = event.msg
    payload = event.payload.strip()
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can manage filters."))
        return

    if not payload:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: `/filterdel <number or phrase>`\nExample: `/filterdel 1` or `/filterdel #реклама`"))
        return

    success, deleted_pattern = database.remove_filter(payload)
    if success:
        _reload_filter_cache()
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Filter `{deleted_pattern}` removed."))
    else:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"❌ Filter `{payload}` not found."))

@dc_cli.on(events.NewMessage(command="/bridge"))
def bridge_command(bot, accid, event):
    """Bridge a Delta Chat group to a Telegram group. Admin only."""
    msg = event.msg
    chat_id = msg.chat_id

    # Check if it's a group chat
    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    if chat_info.get("type") == 1:
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ You must send that command in a Delta Chat group, not here."))
        return

    # Admin check: if a global admin is set, only they can manage bridges
    if database.get_config("admin_dc_fingerprint") or database.get_config("admin_dc_email"):
        if not _is_dc_admin(bot, accid, msg.from_id):
            _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only the configured bot administrator can use /bridge."))
            return
    else:
        # Fallback to group creator check
        try:
            contacts = bot.rpc.get_chat_contacts(accid, chat_id)
            # In Delta Chat, the first contact in the list is the group creator/admin
            # Also check if the sender is the bot owner (contact ID 1 = self)
            if msg.from_id not in contacts[:1] and msg.from_id != 1:
                _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only group admins can use /bridge. (Or set a global admin via /initadmin)"))
                return
        except Exception as e:
            logger.warning(f"Could not verify DC chat contacts for /bridge: {e}")
            _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Could not verify group administrator status."))
            return

    try:
        payload = event.payload.strip()
        if not payload:
            raise ValueError()
        tg_chat_id = int(payload)
    except ValueError:
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ You must provide the Telegram chat ID. Example: /bridge -123456789"))
        return

    if database.add_bridge(chat_id, tg_chat_id):
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=f"✔️ Bridged with Telegram group {tg_chat_id}."))
    else:
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ This chat is already bridged."))

@dc_cli.on(events.NewMessage(command="/unbridge"))
def unbridge_command(bot, accid, event):
    """Remove the bridge for this Delta Chat group. Admin only."""
    msg = event.msg
    chat_id = msg.chat_id

    # Check if it's a group chat
    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    if chat_info.get("type") == 1:
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ You must send that command in a Delta Chat group, not here."))
        return

    # Admin check
    if database.get_config("admin_dc_fingerprint") or database.get_config("admin_dc_email"):
        if not _is_dc_admin(bot, accid, msg.from_id):
            _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only the configured bot administrator can use /unbridge."))
            return
    else:
        try:
            contacts = bot.rpc.get_chat_contacts(accid, chat_id)
            if msg.from_id not in contacts[:1] and msg.from_id != 1:
                _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only group admins can use /unbridge. (Or set a global admin via /initadmin)"))
                return
        except Exception as e:
            logger.warning(f"Could not verify DC chat contacts for /unbridge: {e}")
            _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Could not verify group administrator status."))
            return

    tg_chat_ids = database.remove_bridge(chat_id)
    if tg_chat_ids:
        _clear_dc_caches(chat_id)
        if main_loop:
            for tid in tg_chat_ids:
                asyncio.run_coroutine_threadsafe(_userbot_leave_chat(tid), main_loop)
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="✔️ Bridge removed."))
    else:
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ This chat is not bridged."))


@dc_cli.on(events.NewMessage(command="/cleanup"))
def dc_cleanup_command(bot, accid, event):
    """Trigger manual cleanup of stale/orphaned/duplicate bridges (Admin only)."""
    msg = event.msg
    chat_id = msg.chat_id
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only the bot administrator can use /cleanup."))
        return

    global main_loop
    if main_loop and main_loop.is_running():
        async def _do_cleanup():
            try:
                stats = await cleanup_stale_bridges(dc_bot=bot, accid=accid)
                total_removed = (
                    stats['orphaned_bridges_removed']
                    + stats['duplicate_bridges_removed']
                    + stats['dead_bridges_removed']
                    + stats['orphaned_channels_removed']
                )
                res_text = (
                    f"🧹 Cleanup Complete\n\n"
                    f"• Orphaned bridges removed from DB: {stats['orphaned_bridges_removed']}\n"
                    f"• Duplicate bridges removed: {stats['duplicate_bridges_removed']}\n"
                    f"• Dead ghost bridges removed: {stats['dead_bridges_removed']}\n"
                    f"• Orphaned channels removed: {stats['orphaned_channels_removed']}\n"
                    f"• Empty DC chats deleted: {stats['dc_chats_deleted']}\n\n"
                    f"Total items cleaned: {total_removed}"
                )
                _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=res_text))
            except Exception as e:
                logger.error(f"DC manual cleanup failed: {e}")
                _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=f"❌ Cleanup failed: {e}"))

        asyncio.run_coroutine_threadsafe(_do_cleanup(), main_loop)
    else:
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Main event loop is not ready."))


@dc_cli.on(events.NewMessage(command="/locupdate"))
def locupdate_command(bot, accid, event):
    """Fetch the latest coordinates for a live location message."""
    msg = event.msg
    chat_id = msg.chat_id

    # Check if this is a reply to another message
    if not hasattr(msg, 'quote') or not msg.quote:
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Please reply to a Live Location message with /locupdate."))
        return

    quote_msg_id = msg.quote.get('message_id') if isinstance(msg.quote, dict) else None
    if not quote_msg_id:
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Please reply to a Live Location message with /locupdate."))
        return

    # Check group chat mappings
    tg_chats = database.get_tg_chats(chat_id)
    found = False
    
    if tg_chats:
        for tg_chat_id in tg_chats:
            tg_msg_id = database.get_tg_msg_id(quote_msg_id, chat_id, tg_chat_id)
            if tg_msg_id and tg_msg_id in LIVE_LOCATIONS:
                lat, lon = LIVE_LOCATIONS[tg_msg_id]
                _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=f"📍 Updated Location: https://maps.google.com/?q={lat},{lon}", quoted_message_id=quote_msg_id))
                found = True
                break
                
    if not found:
        # Check channel mapping if needed (very rare case for locupdate but just in case)
        # We don't track channel reverse mapping in memory easily here, so we just return not found.
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ No active live location found for this message. It may have expired or not be a live location.", quoted_message_id=msg.id))


@dc_cli.on(events.NewMessage(command="/userbotsync"))
def dc_userbotsync_command(bot, accid, event):
    """Force Userbot sync from Delta Chat. Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can trigger synchronization."))
        return

    async def run_sync():
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="⏳ Starting Userbot synchronization..."))
        await sync_userbot_channels(force=True)
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="✅ Userbot synchronization completed (subscriber counts updated)."))
    
    if main_loop:
        asyncio.run_coroutine_threadsafe(run_sync(), main_loop)
    else:
        logger.error("Main loop not found, cannot run userbotsync")

@dc_cli.on(events.NewMessage(command="/userbotjoin"))
def dc_userbotjoin_command(bot, accid, event):
    """Join a channel/group via Userbot using an invite link. Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use this command."))
        return

    text = msg.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(
            text="/userbotjoin <link>\n\n"
                 "Examples:\n"
                 "• /userbotjoin https://t.me/+AbCdEfGhIjK\n"
                 "• /userbotjoin https://t.me/channelname\n"
                 "• /userbotjoin @channelname"
        ))
        return

    link = parts[1].strip()

    async def do_join():
        import re
        global userbot_client

        if not (userbot_client and userbot_client.is_connected()):
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Userbot is not connected."))
            return

        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"⏳ Attempting to join via Userbot: {link}..."))

        try:
            joined_entity = None
            joined_title = "Unknown"

            # Check if it's a private invite link
            invite_hash = None
            m = re.search(r't\.me/\+([a-zA-Z0-9_-]+)', link)
            if m:
                invite_hash = m.group(1)
            else:
                m = re.search(r't\.me/joinchat/([a-zA-Z0-9_-]+)', link)
                if m:
                    invite_hash = m.group(1)

            if invite_hash:
                if not ImportChatInviteRequest:
                    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Telethon not properly installed."))
                    return

                try:
                    check = await asyncio.wait_for(userbot_client(CheckChatInviteRequest(invite_hash)), timeout=15.0)
                    if hasattr(check, 'chat'):
                        joined_entity = check.chat
                        joined_title = getattr(joined_entity, 'title', 'Unknown')
                    else:
                        result = await asyncio.wait_for(userbot_client(ImportChatInviteRequest(invite_hash)), timeout=15.0)
                        if hasattr(result, 'chats') and result.chats:
                            joined_entity = result.chats[0]
                            joined_title = getattr(joined_entity, 'title', 'Unknown')
                except Exception as e:
                    if "already" in str(e).lower() or "USER_ALREADY_PARTICIPANT" in str(e):
                        try:
                            joined_entity = await asyncio.wait_for(userbot_client.get_entity(link), timeout=15.0)
                            joined_title = getattr(joined_entity, 'title', 'Unknown')
                        except Exception:
                            pass
                    else:
                        raise
            else:
                target = link
                if not target.startswith('@') and 't.me/' in target:
                    m = re.search(r't\.me/([a-zA-Z0-9_]+)', target)
                    if m:
                        target = f"@{m.group(1)}"

                entity = await asyncio.wait_for(userbot_client.get_entity(target), timeout=15.0)
                is_bot = getattr(entity, 'bot', False)
                if is_bot:
                    try:
                        await asyncio.wait_for(userbot_client.send_message(entity, "/start"), timeout=10.0)
                    except Exception:
                        pass
                    first_name = getattr(entity, 'first_name', '') or ''
                    last_name = getattr(entity, 'last_name', '') or ''
                    full_name = f"{first_name} {last_name}".strip()
                    joined_title = full_name or getattr(entity, 'title', None) or (f"@{entity.username}" if getattr(entity, 'username', None) else target)
                else:
                    if getattr(entity, 'left', True):
                        if JoinChannelRequest:
                            await asyncio.wait_for(userbot_client(JoinChannelRequest(entity)), timeout=15.0)
                    joined_title = getattr(entity, 'title', 'Unknown')
                joined_entity = entity

            if joined_entity:
                joined_id = getattr(joined_entity, 'id', None)
                tg_channel_id = int(f"-100{joined_id}") if joined_id and joined_id > 0 else joined_id

                matched_channel = None
                if tg_channel_id:
                    matched_channel = database.get_channel_by_tg_id(tg_channel_id)
                if not matched_channel:
                    username = getattr(joined_entity, 'username', None)
                    if username:
                        matched_channel = database.get_channel_by_tg_username(username)

                if matched_channel:
                    database.update_channel_invite_link(matched_channel['id'], link)
                    if not matched_channel.get('tg_channel_id') and tg_channel_id:
                        database.update_channel_tg_id(
                            matched_channel.get('tg_channel_username', ''),
                            tg_channel_id
                        )
                    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(
                        text=f"✅ Userbot joined *{joined_title}* (ID: {tg_channel_id}).\n"
                             f"Invite link saved for future syncs.\n\n"
                             f"Matched to bridged channel #{matched_channel['id']}."
                    ))
                else:
                    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(
                        text=f"✅ Userbot joined *{joined_title}* (ID: {tg_channel_id}).\n\n"
                             f"⚠️ This channel is not yet bridged."
                    ))

                try:
                    chan_id = matched_channel['id'] if matched_channel else None
                    if chan_id:
                        await update_tg_channel_stats(chan_id, joined_entity)
                except Exception:
                    pass
            else:
                _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="⚠️ Joined but could not determine channel details."))

        except Exception as e:
            logger.error(f"DC userbotjoin failed for {link}: {e}")
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to join: {str(e)[:500]}"))

    if main_loop:
        asyncio.run_coroutine_threadsafe(do_join(), main_loop)
    else:
        logger.error("Main loop not found, cannot run userbotjoin")


def _format_relative_time(timestamp: int) -> str:
    if not timestamp:
        return "unknown time"
    diff = int(time.time() - timestamp)
    if diff < 0:
        return "just now"
    if diff < 10:
        return "just now"
    elif diff < 60:
        return f"{diff}s ago"
    elif diff < 3600:
        return f"{diff // 60}m ago"
    elif diff < 86400:
        return f"{diff // 3600}h ago"
    else:
        return f"{diff // 86400}d ago"


async def generate_status_report(is_html=True, bot_ref=None, accid=None) -> str:
    # 1. Telegram Bot API
    tg_bot_status = "🔴 Off"
    if tg_app:
        try:
            bot_me = await tg_app.bot.get_me()
            tg_bot_status = f"🟢 Connected (as @{bot_me.username})"
        except Exception as e:
            tg_bot_status = f"🟡 Connected (error fetching info: {e})"
    
    # 2. Userbot Status
    ub_status = "🔴 Disconnected"
    if userbot_client:
        if userbot_client.is_connected():
            try:
                ub_me = await asyncio.wait_for(userbot_client.get_me(), timeout=3.0)
                ub_username = getattr(ub_me, 'username', None)
                ub_name = f"@{ub_username}" if ub_username else f"ID {ub_me.id}"
                ub_status = f"🟢 Connected (as {ub_name})"
            except Exception as e:
                ub_status = f"🟢 Connected (error fetching info: {e})"
        else:
            ub_status = "🔴 Disconnected (client exists)"
    
    # 3. Delta Chat Status
    dc_status = "🔴 Not initialized"
    dc_addr = "Unknown"
    if bot_ref and accid:
        try:
            dc_addr = bot_ref.rpc.get_config(accid, "configured_addr") or bot_ref.rpc.get_config(accid, "addr") or "Unknown"
            dc_status = f"🟢 Connected (as {dc_addr})"
        except Exception as e:
            dc_status = f"🟡 Connected (error: {e})"
    elif dc_bot_instance and dc_accid:
        try:
            dc_addr = dc_bot_instance.rpc.get_config(dc_accid, "configured_addr") or dc_bot_instance.rpc.get_config(dc_accid, "addr") or "Unknown"
            dc_status = f"🟢 Connected (as {dc_addr})"
        except Exception:
            pass

    # 4. Global Rate Limit & Message Queue Workers
    active_workers = len([w for w, task in _channel_workers.items() if not task.done()])
    queue_lines = []
    for cid, q in _channel_queues.items():
        qsize = q.qsize()
        if qsize > 0:
            ch_data = database.get_channel_by_tg_id(cid)
            ch_name = ch_data.get('tg_channel_username') if ch_data else None
            ch_name = f"@{ch_name}" if ch_name else f"ID {cid}"
            queue_lines.append(f"  • {ch_name}: {qsize} pending")
    
    queue_status = "All workers idle"
    if active_workers > 0 or queue_lines:
        queue_status = f"{active_workers} active workers"
        if queue_lines:
            queue_status += "\n" + "\n".join(queue_lines)

    # 5. Channel status
    channels = database.get_all_channels()
    chan_lines = []
    for ch in channels:
        ch_id = ch['tg_channel_id']
        username = ch.get('tg_channel_username')
        # Look up cached or database last_msg_id
        last_id = _get_cached_last_msg_id(ch_id)
        
        # Link construction
        if username:
            link = f"https://t.me/{username}/{last_id}" if last_id > 0 else f"https://t.me/{username}"
            display_name = f"@{username}"
        else:
            clean_id = str(ch_id)
            if clean_id.startswith("-100"):
                clean_id = clean_id[4:]
            elif clean_id.startswith("-"):
                clean_id = clean_id[1:]
            link = f"https://t.me/c/{clean_id}/{last_id}" if last_id > 0 else f"https://t.me/c/{clean_id}"
            display_name = f"ID {ch_id}"
        
        if is_html:
            chan_lines.append(f"• <a href='{link}'>{display_name}</a> (Last post: #{last_id if last_id > 0 else 'None'})")
        else:
            chan_lines.append(f"• {display_name} [Post #{last_id if last_id > 0 else 'None'}]({link})")
            
    # 6. Recent activity
    recent_maps = database.get_recent_message_maps(5)
    activity_lines = []
    for r in recent_maps:
        username = r.get('tg_channel_username')
        ch_display = f"@{username}" if username else f"ID {r['tg_chat_id']}"
        t_str = _format_relative_time(r['created_at']) if r.get('created_at') else "unknown time"
        
        if is_html:
            activity_lines.append(f"• TG msg <code>{r['tg_msg_id']}</code> in {ch_display} ➔ DC msg <code>{r['dc_msg_id']}</code> ({t_str})")
        else:
            activity_lines.append(f"• TG msg `{r['tg_msg_id']}` in {ch_display} ➔ DC msg `{r['dc_msg_id']}` ({t_str})")

    # Format output
    if is_html:
        report = (
            "🤖 <b>Telegram Bridge Bot Status</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🔌 <b>Bot API:</b> {tg_bot_status}\n"
            f"👤 <b>Userbot:</b> {ub_status}\n"
            f"📧 <b>Delta Chat:</b> {dc_status}\n\n"
            f"📊 <b>Queue Workers:</b>\n{queue_status}\n\n"
            f"📡 <b>Channels ({len(channels)}):</b>\n" + ("\n".join(chan_lines) if chan_lines else "None") + "\n\n"
            f"📝 <b>Recent Activity:</b>\n" + ("\n".join(activity_lines) if activity_lines else "None")
        )
    else:
        report = (
            "🤖 **Telegram Bridge Bot Status**\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🔌 **Bot API:** {tg_bot_status}\n"
            f"👤 **Userbot:** {ub_status}\n"
            f"📧 **Delta Chat:** {dc_status}\n\n"
            f"📊 **Queue Workers:**\n{queue_status}\n\n"
            f"📡 **Channels ({len(channels)}):**\n" + ("\n".join(chan_lines) if chan_lines else "None") + "\n\n"
            f"📝 **Recent Activity:**\n" + ("\n".join(activity_lines) if activity_lines else "None")
        )
    return report


@dc_cli.on(events.NewMessage(command="/status"))
def dc_status_command(bot, accid, event):
    """Show detailed bot and userbot status (admin only)."""
    msg = event.msg
    chat_id = msg.chat_id

    # Check if sender is admin
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only the bot administrator can use /status."))
        return

    async def do_status():
        try:
            report = await generate_status_report(is_html=False, bot_ref=bot, accid=accid)
            _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=report))
        except Exception as e:
            logger.error(f"Error generating status report for DC: {e}")
            _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=f"❌ Error generating status report: {e}"))

    if main_loop:
        asyncio.run_coroutine_threadsafe(do_status(), main_loop)
    else:
        logger.error("Main loop not found, cannot run /status")


@dc_cli.on(events.NewMessage(command="/catchup"))
@dc_cli.on(events.NewMessage(command="/reconcile"))
def dc_catchup_command(bot, accid, event):
    """Catch up missed channel posts (admin only)."""
    msg = event.msg
    chat_id = msg.chat_id

    # Check if sender is admin
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only the bot administrator can use /catchup."))
        return

    text = msg.text or ""
    parts = text.strip().split()
    target = parts[1] if len(parts) > 1 else None

    async def do_catchup():
        try:
            res_text = await run_channel_catchup(target, is_html=False)
            _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=res_text))
        except Exception as e:
            logger.error(f"Error during DC /catchup: {e}")
            _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=f"❌ Catchup error: {e}"))

    if main_loop:
        asyncio.run_coroutine_threadsafe(do_catchup(), main_loop)
    else:
        logger.error("Main loop not found, cannot run /catchup")


@dc_cli.on(events.NewMessage(command="/stats"))
def stats_command(bot, accid, event):
    """Show bridge statistics."""
    msg = event.msg
    chat_id = msg.chat_id

    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    is_private = chat_info.get("type") == 1

    if is_private:
        # In private chat: show all bridges (admin only)
        if not _is_dc_admin(bot, accid, msg.from_id):
            _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only the bot admin can view all stats."))
            return

        bridges = database.get_all_bridges()
        if not bridges:
            _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="📊 No bridges configured."))
            return

        lines = [f"📊 Bridge Statistics ({len(bridges)} bridge{'s' if len(bridges) != 1 else ''})\n"]
        for row in bridges:
            # row: (dc_chat_id, tg_chat_id, reactions_count)
            dc_cid, tg_cid, r_count = row if len(row) == 3 else (row[0], row[1], 0)
            m_count = database.get_bridge_message_count(dc_cid, tg_cid)
            try:
                title = bot.rpc.get_basic_chat_info(accid, dc_cid).get("name", "Unknown Group")
            except Exception:
                title = "Unknown Group"
            lines.append(f"• DC {dc_cid} ↔ TG {tg_cid} ({title}) — {m_count} 💬 {r_count} 🙂")
                
        lines.append(f"\n⚙️ Rich Post Mode: **{database.get_rich_mode()}**")
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="\n".join(lines)))
    else:
        # In group chat: show stats for this bridge only
        tg_chats = database.get_tg_chats(chat_id)
        if not tg_chats:
            _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="📊 This group is not bridged."))
            return

        lines = ["📊 Bridge Statistics\n"]
        for tg_cid in tg_chats:
            m_count = database.get_bridge_message_count(chat_id, tg_cid)
            try:
                title = bot.rpc.get_basic_chat_info(accid, chat_id).get("name", "this group")
            except Exception:
                title = "this group"
            
            # Reactions are not relevant for broadcast channels, only show for groups if we have them
            r_count = database.get_bridge_reaction_count(chat_id, tg_cid)
            lines.append(f"• TG Group {tg_cid} ({title}) — {m_count} 💬 {r_count} 🙂")
                
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="\n".join(lines)))




@dc_cli.on(events.NewMessage(command="/channels"))
def channels_command_dc(bot, accid, event):
    """List public bridged channels to Delta Chat users, or trigger sync if requested."""
    msg = event.msg
    chat_id = msg.chat_id

    # Check if requester is admin
    is_admin = _is_dc_admin(bot, accid, msg.from_id)

    # Check for sync sub-commands (e.g. "/channels sync" or "/channels sync 14")
    payload = event.payload.strip().lower()
    if payload.startswith("sync") or payload.startswith("update") or payload.startswith("refresh"):
        if not is_admin:
            _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only administrators can sync channels."))
            return
        
        parts = payload.split()
        target_channel_id = None
        if len(parts) > 1 and parts[1].isdigit():
            target_channel_id = int(parts[1])

        if main_loop:
            asyncio.run_coroutine_threadsafe(
                async_update_channels_dc(bot, accid, chat_id, target_channel_id),
                main_loop
            )
        return

    # Check if this chat is a direct 1:1 private chat with the bot
    is_private_chat = False
    try:
        current_chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
        is_private_chat = current_chat_info.get("type") == 1
    except Exception:
        pass

    channels = database.get_all_channels()
    if is_admin and is_private_chat:
        # Admin in private chat sees everything (including private channels)
        display_channels = channels
    else:
        # Groups or non-admins only see public channels (those with a username)
        display_channels = [c for c in channels if c.get('tg_channel_username')]

    if not display_channels:
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="📺 No public channels are currently available."))
        return

    lines = [f"📺 **{'All' if (is_admin and is_private_chat) else 'Public'} Channels:**\n"]
    for ch in display_channels:
        dc_cid = ch['dc_chat_id']
        tg_username = ch.get('tg_channel_username')
        tg_id = ch.get('tg_channel_id', 0)
        
        # Get counts
        r_count = ch.get('reactions_count', 0)
        m_count = database.get_bridge_message_count(dc_cid, tg_id)
        tg_sub_count = ch.get('tg_participants_count', 0)
        
        title = ch.get('title') or "Unknown Channel"
        try:
            chat_info = bot.rpc.get_basic_chat_info(accid, dc_cid)
            if chat_info and chat_info.get("name"):
                title = chat_info.get("name")
            contacts = bot.rpc.get_chat_contacts(accid, dc_cid)
            if contacts:
                try:
                    self_id = bot.rpc.get_contact(accid, 1).id
                except Exception:
                    self_id = 1
                dc_sub_count = len(contacts) - 1 if self_id in contacts else len(contacts)
            else:
                dc_sub_count = 0
        except Exception:
            dc_sub_count = "?"

        disp_title = (title[:37] + "...") if len(title) > 40 else title
        # Format: /channel22 — [42 секунды](https://t.me/ftsec) — 👤 19,373 TG / 2 DC — 💬 118
        if tg_username:
            title_display = f"[{disp_title}](https://t.me/{tg_username})"
        else:
            title_display = f"{disp_title} (ID: {tg_id})"
        stats_str = f"👤 {tg_sub_count:,} TG / {dc_sub_count} DC — 💬 {m_count}"
        line = f"/channel{ch['id']} — {title_display} — {stats_str}"
        lines.append(line)
    
    lines.append("\nClick a /channelN command for link or /channelNqr for QR code.")
    _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="\n".join(lines)))


@dc_cli.on(events.NewMessage(command="/channelssync"))
@dc_cli.on(events.NewMessage(command="/channelsync"))
def channelssync_command_dc(bot, accid, event):
    """Sync/update bridged Telegram channel names and avatars in Delta Chat."""
    msg = event.msg
    chat_id = msg.chat_id

    # Check if requester is admin
    is_admin = _is_dc_admin(bot, accid, msg.from_id)
    if not is_admin:
        _dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only administrators can sync channels."))
        return

    payload = event.payload.strip()
    target_channel_id = None
    if payload.isdigit():
        target_channel_id = int(payload)

    if main_loop:
        asyncio.run_coroutine_threadsafe(
            async_update_channels_dc(bot, accid, chat_id, target_channel_id),
            main_loop
        )











# Save references for Telegram to use and print QR
@dc_cli.on_start
def on_start(bot, _args):
    global dc_bot_instance, dc_accid, bot_contact_id
    setup_custom_command_parser(bot, ["tg", "tgbridge"])
    _make_rpc_thread_safe(bot)
    dc_bot_instance = bot
    accounts = bot.rpc.get_all_account_ids()
    if accounts:
        dc_accid = accounts[0]

        # Show configured admin and transports
        admin_dc_email = database.get_config("admin_dc_email")
        admin_dc_fingerprint = database.get_config("admin_dc_fingerprint")
        if admin_dc_email:
            fp_suffix = f" ({admin_dc_fingerprint[-8:].upper()})" if admin_dc_fingerprint else ""
            print(f"Bot Administrator: {admin_dc_email}{fp_suffix}")

        try:
            transports = bot.rpc.list_transports(dc_accid)
            print("\n" + "=" * 50)
            print("Configured Bot Transports (Relays):")
            for t in transports:
                addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
                print(f" - {addr}")
        except Exception:
            pass

        # Detect bot's own contact ID to prevent echo loops
        try:
            bot_contact_id = bot.rpc.get_contact(dc_accid, 1).id  # Contact ID 1 = self
        except Exception:
            try:
                # Fallback: use the special contact constant
                bot_contact_id = 1
            except Exception:
                pass

        try:
            qrdata = bot.rpc.get_chat_securejoin_qr_code(dc_accid, None)
            print("\n" + "="*50)
            print("To add this bot to a Delta Chat group, scan the QR code")
            print("or copy the link below:\n")

            try:
                qr = qrcode.QRCode(version=1, box_size=1, border=2)
                qr.add_data(qrdata)
                qr.make(fit=True)
                f = io.StringIO()
                qr.print_ascii(out=f)
                print(f.getvalue())
            except ImportError:
                print("(Install 'qrcode' package to see the ASCII QR code here)")

            print(qrdata)
            print("\n" + "="*50 + "\n")
        except Exception as e:
            bot.logger.error(f"Failed to generate QR code: {e}")

        # Start periodic background cleanup & stats flush worker (every 60s)
        def _bg_cleanup_worker():
            while True:
                time.sleep(60)
                try:
                    database.flush_transport_stats()
                    database.cleanup_old_records()
                except Exception as e:
                    bot.logger.debug(f"Background cleanup error: {e}")

        threading.Thread(target=_bg_cleanup_worker, daemon=True, name="bg_cleanup_worker").start()

# ---------------------------------------------------------
# TELEGRAM HANDLERS
# ---------------------------------------------------------










# ---------------------------------------------------------
# TG BRIDGE / UNBRIDGE COMMANDS
# ---------------------------------------------------------





# ---------------------------------------------------------
# ADMIN MANAGEMENT COMMANDS (owner only)
# ---------------------------------------------------------











# ---------------------------------------------------------
# CHANNEL BRIDGING COMMANDS
# ---------------------------------------------------------

































# ---------------------------------------------------------
# CHANNEL POST HANDLER
# ---------------------------------------------------------













# ---------------------------------------------------------
# MAIN ASYNC RUNNER
# ---------------------------------------------------------

async def tg_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and handle specific network exceptions."""
    exc = context.error
    exc_str = str(exc)
    exc_type = type(exc).__name__

    # Handle group -> supergroup migration errors
    from telegram.error import ChatMigrated, Forbidden, BadRequest
    if isinstance(exc, ChatMigrated):
        new_id = exc.new_chat_id
        old_id = None
        if isinstance(update, Update) and update.effective_chat:
            old_id = update.effective_chat.id
        if old_id and new_id:
            logger.info(f"TG group migrated (from error): {old_id} -> {new_id}")
            database.update_bridge_tg_chat_id(old_id, new_id)
        else:
            logger.warning(f"ChatMigrated error but could not determine old chat ID: {exc_str}")
        return

    # Silence normal API response errors like blocked by user or message not modified
    if isinstance(exc, Forbidden):
        logger.warning(f"Telegram Bot Forbidden action (e.g. blocked by user): {exc_str}")
        return
    if isinstance(exc, BadRequest):
        logger.warning(f"Telegram Bot BadRequest (e.g. message already modified/deleted): {exc_str}")
        return

    # Common network/timeout errors that we handle via retries or just want to log less noisily
    network_errors = ("ReadTimeout", "ConnectTimeout", "ProxyError", "NetworkError", "RemoteProtocolError")
    is_network = any(err in exc_str or err in exc_type for err in network_errors) or "Server disconnected" in exc_str

    if is_network:
        logger.warning(f"Telegram network error: {exc_type}: {exc_str}")
        return

    logger.error(f"Telegram Application Error [{exc_type}]: {exc_str}", exc_info=exc)

def start_dc_bot():
    """Runs the DC Bot CLI (it is blocking)"""
    try:
        dc_cli.start()
    except SystemExit:
        pass
    except Exception as e:
        logger.error(f"DC CLI error: {e}")


async def cleanup_stale_bridges(dc_bot=None, accid=None, tg_app_instance=None, ub_client=None) -> dict:
    """
    Scan configured bridges and channels to remove stale, orphaned, or duplicate records.
    Returns a dict with summary stats.
    """
    stats = {
        'orphaned_bridges_removed': 0,
        'duplicate_bridges_removed': 0,
        'dead_bridges_removed': 0,
        'orphaned_channels_removed': 0,
        'dc_chats_deleted': 0
    }

    if dc_bot is None:
        dc_bot = dc_bot_instance
    if accid is None:
        accid = dc_accid
    if tg_app_instance is None:
        tg_app_instance = tg_app
    if ub_client is None:
        ub_client = userbot_client

    if not dc_bot or not accid:
        logger.warning("Cleanup: Delta Chat bot instance or account ID not ready, skipping.")
        return stats

    # Get bot's self contact ID for DC member count checks
    try:
        self_contact = dc_bot.rpc.get_contact(accid, 1)
        self_contact_id = getattr(self_contact, 'id', 1) if hasattr(self_contact, 'id') else (self_contact.get('id', 1) if isinstance(self_contact, dict) else 1)
    except Exception:
        self_contact_id = 1

    # Helper to check DC chat details
    def get_dc_info(dc_cid):
        try:
            info = dc_bot.rpc.get_basic_chat_info(accid, dc_cid)
            if not info or not isinstance(info, dict) or not info.get("name"):
                return None, 0
            contacts = dc_bot.rpc.get_chat_contacts(accid, dc_cid)
            if contacts:
                sub_count = len(contacts) - 1 if self_contact_id in contacts else len(contacts)
            else:
                sub_count = 0
            return info, max(0, sub_count)
        except Exception:
            return None, 0

    def delete_dc_chat_safe(dc_cid):
        try:
            dc_bot.rpc.delete_chat(accid, dc_cid)
            stats['dc_chats_deleted'] += 1
            _clear_dc_caches(dc_cid)
            logger.info(f"Cleanup: Deleted DC chat {dc_cid} from Delta Chat core.")
        except Exception as e:
            logger.debug(f"Cleanup: Could not delete DC chat {dc_cid}: {e}")

    # --- 1. Scan Group Bridges ---
    bridges = database.get_all_bridges()
    # Group by tg_chat_id: {tg_chat_id: [(dc_cid, reactions_count), ...]}
    tg_to_bridges = {}
    for row in bridges:
        dc_cid, tg_cid = row[0], row[1]
        r_count = row[2] if len(row) > 2 else 0
        tg_to_bridges.setdefault(tg_cid, []).append((dc_cid, r_count))

    for tg_cid, bridge_list in tg_to_bridges.items():
        evaluated = []
        for dc_cid, r_count in bridge_list:
            info, sub_count = get_dc_info(dc_cid)
            m_count = database.get_bridge_message_count(dc_cid, tg_cid)
            evaluated.append({
                'dc_cid': dc_cid,
                'exists_in_dc': info is not None,
                'info': info,
                'title': info.get('name', '') if info else '',
                'sub_count': sub_count,
                'm_count': m_count,
                'r_count': r_count
            })

        # A. Remove non-existent DC chats (orphaned in DB)
        alive_bridges = []
        for b in evaluated:
            if not b['exists_in_dc']:
                database.remove_bridge_pair(b['dc_cid'], tg_cid)
                _clear_dc_caches(b['dc_cid'])
                stats['orphaned_bridges_removed'] += 1
                logger.info(f"Cleanup: Removed orphaned DB bridge DC {b['dc_cid']} ↔ TG {tg_cid} (chat does not exist in DC)")
            else:
                alive_bridges.append(b)

        # B. Handle duplicates for the same tg_cid
        remaining_bridges = []
        if len(alive_bridges) > 1:
            # Sort: prioritize bridges with subscribers > 0, then higher message count, then higher (newer) dc_cid
            alive_bridges.sort(key=lambda x: (x['sub_count'] > 0, x['sub_count'], x['m_count'], x['dc_cid']), reverse=True)
            primary = alive_bridges[0]
            remaining_bridges.append(primary)
            for dup in alive_bridges[1:]:
                # If the duplicate has 0 subscribers, we can safely delete it
                if dup['sub_count'] == 0:
                    database.remove_bridge_pair(dup['dc_cid'], tg_cid)
                    delete_dc_chat_safe(dup['dc_cid'])
                    stats['duplicate_bridges_removed'] += 1
                    logger.info(f"Cleanup: Removed duplicate bridge DC {dup['dc_cid']} (0 subs, title='{dup['title']}') for TG {tg_cid}, keeping DC {primary['dc_cid']}")
                else:
                    remaining_bridges.append(dup)
        else:
            remaining_bridges = alive_bridges

        # C. Check for dead fallback ghost bridges (0 subs, 0 messages, fallback title like Bridge -100... or TG Group -100...)
        for b in remaining_bridges:
            if b['sub_count'] == 0 and b['m_count'] == 0:
                title = b.get('title', '')
                is_fallback_name = title.startswith("Bridge -") or title.startswith("TG Group -") or title == "Unknown Group"
                if is_fallback_name:
                    # Check if TG chat is accessible. Bounded timeouts + a short yield between
                    # checks keep this from hammering Telegram's rate limits or hogging the
                    # shared userbot connection for many minutes when there are many stale
                    # ghost bridges to check (this runs on every startup).
                    tg_accessible = False
                    if tg_app_instance and hasattr(tg_app_instance, 'bot') and tg_app_instance.bot:
                        try:
                            await asyncio.wait_for(tg_app_instance.bot.get_chat(tg_cid), timeout=10.0)
                            tg_accessible = True
                        except Exception:
                            pass
                    if not tg_accessible and ub_client and hasattr(ub_client, 'is_connected') and ub_client.is_connected():
                        try:
                            await asyncio.wait_for(ub_client.get_entity(tg_cid), timeout=10.0)
                            tg_accessible = True
                        except Exception:
                            pass
                    await asyncio.sleep(0.3)

                    if not tg_accessible:
                        database.remove_bridge_pair(b['dc_cid'], tg_cid)
                        delete_dc_chat_safe(b['dc_cid'])
                        stats['dead_bridges_removed'] += 1
                        logger.info(f"Cleanup: Removed dead ghost bridge DC {b['dc_cid']} ↔ TG {tg_cid} (0 subs, 0 msgs, title='{b['title']}', TG chat inaccessible)")

    # --- 2. Scan Channels ---
    channels = database.get_all_channels()
    for ch in channels:
        ch_id = ch['id']
        dc_cid = ch.get('dc_chat_id')
        if not dc_cid:
            continue
        info, _ = get_dc_info(dc_cid)
        if info is None:
            database.remove_channel(ch_id)
            _invalidate_dc_channel_cache(ch.get('tg_channel_id'))
            _clear_dc_caches(dc_cid)
            stats['orphaned_channels_removed'] += 1
            logger.info(f"Cleanup: Removed orphaned channel #{ch_id} (DC {dc_cid} no longer exists in DC)")

    logger.info(f"Cleanup finished: {stats}")
    return stats




async def db_cleanup_loop():
    cleanup_counter = 0
    while True:
        try:
            database.cleanup_old_messages()
            cleanup_counter += 1
            # Run full bridge reconciliation cleanup once every 24 hours (24 iterations of 1h)
            if cleanup_counter >= 24:
                cleanup_counter = 0
                await cleanup_stale_bridges()
            await asyncio.sleep(3600)  # Every hour
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            await asyncio.sleep(60)


# ---------------------------------------------------------
# TELETHON USERBOT HANDLERS & RECONCILIATION
# ---------------------------------------------------------





async def update_tg_channel_stats(channel_id: int, tg_peer):
    """Fetch real participant count and username from TG and save to DB."""
    if not (userbot_client and userbot_client.is_connected()):
        return 0
    
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest
        from telethon.tl.types import Channel, Chat
        
        full = None
        username = getattr(tg_peer, 'username', None)
        
        if isinstance(tg_peer, Channel):
             full = await asyncio.wait_for(userbot_client(GetFullChannelRequest(tg_peer)), timeout=15.0)
        elif isinstance(tg_peer, Chat):
             full = await asyncio.wait_for(userbot_client(GetFullChatRequest(tg_peer.id)), timeout=15.0)
        
        count = 0
        if full and hasattr(full, 'full_chat'):
            count = getattr(full.full_chat, 'participants_count', 0)
        
        # Update both count and username (if we found one now)
        database.update_channel_info(channel_id, participants_count=count, username=username)
        return count
    except Exception as e:
        logger.warning(f"Failed to fetch stats for TG peer: {e}")
    return 0





















def _asyncio_exception_handler(loop, context):
    """Custom asyncio exception handler to filter benign Telethon connection teardown noise."""
    msg = context.get("message", "")
    exception = context.get("exception")
    task = context.get("task")
    task_str = str(task) if task else ""

    # Filter known benign Telethon disconnect/GC teardown noise
    if "Task was destroyed but it is pending" in msg or "coroutine ignored GeneratorExit" in msg:
        if any(k in task_str for k in ("Connection._send_loop", "Connection._recv_loop", "MTProtoSender", "_recv_loop", "_send_loop")):
            logger.debug(f"Suppressed benign Telethon connection teardown: {msg}")
            return
        if not task and not exception:
            logger.debug(f"Suppressed unhandled asyncio task message: {msg}")
            return
    if isinstance(exception, RuntimeError) and "coroutine ignored GeneratorExit" in str(exception):
        logger.debug(f"Suppressed benign Telethon GeneratorExit: {exception}")
        return

    # Pass everything else to default handler
    loop.default_exception_handler(context)

async def main():
    global tg_app, main_loop

    logger.info(f"Initializing Telegram Bridge Bot v{VERSION}...")

    args = sys.argv[:]
    is_serve = "serve" in args

    if not is_serve:
        # If we are not in 'serve' mode, we are running a blocking CLI command (like init).
        # We must run it in an executor to avoid deadlocking the asyncio loop.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, start_dc_bot)
        return

    token = database.get_config("telegram_token") or os.environ.get("TELEGRAM_TOKEN")

    if not token:
        print("Error: Telegram token not found. Setup using 'python bot.py init tg <TOKEN>'.")
        sys.exit(1)

    main_loop = asyncio.get_running_loop()
    main_loop.set_exception_handler(_asyncio_exception_handler)

    # 1. Setup Telegram Application with tuned HTTPX connection pool and timeouts
    from telegram.request import HTTPXRequest
    custom_request = HTTPXRequest(
        connection_pool_size=20,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0
    )
    custom_get_updates_request = HTTPXRequest(
        connection_pool_size=10,
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=30.0,
        pool_timeout=30.0
    )
    tg_app = (
        Application.builder()
        .token(token)
        .request(custom_request)
        .get_updates_request(custom_get_updates_request)
        .build()
    )
    tg_app.add_error_handler(tg_error_handler)
    tg_app.add_handler(CommandHandler("start", tg_start_command))
    tg_app.add_handler(CommandHandler("help", tg_help_command))
    tg_app.add_handler(CommandHandler("donate", tg_donate_command))
    tg_app.add_handler(CommandHandler("id", tg_id_command))
    tg_app.add_handler(CommandHandler("stats", tg_stats_command))
    tg_app.add_handler(CommandHandler("status", tg_status_command))
    tg_app.add_handler(CommandHandler("invite", tg_invite_command))
    tg_app.add_handler(CommandHandler("inviteqr", tg_inviteqr_command))
    # Bridge commands (TG side)
    tg_app.add_handler(CommandHandler("bridge", tg_bridge_command))
    tg_app.add_handler(CommandHandler("unbridge", tg_unbridge_command))
    # Admin management commands
    tg_app.add_handler(CommandHandler("adminadd", tg_adminadd_command))
    tg_app.add_handler(CommandHandler("adminremove", tg_adminremove_command))
    tg_app.add_handler(CommandHandler("admins", tg_admins_command))
    # Channel bridging commands
    tg_app.add_handler(CommandHandler("channeladd", tg_channeladd_command))
    tg_app.add_handler(CommandHandler("channels", tg_channels_command))
    tg_app.add_handler(CommandHandler("groups", tg_groups_command))
    tg_app.add_handler(CommandHandler("channel", tg_channel_command))
    tg_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/channel\d+$'), tg_channel_command))
    tg_app.add_handler(CommandHandler("channelqr", tg_channelqr_command))
    tg_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/channel\d+qr$'), tg_channelqr_command))
    tg_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/channelqr\d+$'), tg_channelqr_command))
    tg_app.add_handler(CommandHandler("channelremove", tg_channelremove_command))
    tg_app.add_handler(CommandHandler("channeldelete", tg_channelremove_command))
    tg_app.add_handler(CommandHandler("filters", tg_filters_command))
    tg_app.add_handler(CommandHandler("filteradd", tg_filteradd_command))
    tg_app.add_handler(CommandHandler("filterdel", tg_filterdel_command))
    tg_app.add_handler(CommandHandler("filterremove", tg_filterdel_command))
    tg_app.add_handler(CommandHandler("cleanup", tg_cleanup_command))
    tg_app.add_handler(CommandHandler("catchup", tg_catchup_command))
    tg_app.add_handler(CommandHandler("reconcile", tg_catchup_command))
    tg_app.add_handler(CommandHandler("userbotsync", tg_userbotsync_command))
    tg_app.add_handler(CommandHandler("userbotjoin", tg_userbotjoin_command))
    tg_app.add_handler(CommandHandler("botsend", tg_botsend_command))

    # Handler for bot being added to / removed from chats (my_chat_member updates)
    tg_app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER), group=1)
    # Handler for group -> supergroup migration (must be before the general message handler)
    tg_app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, handle_tg_migration), group=1)
    # Handler for channel posts (TG channel -> DC broadcast)
    tg_app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST & (
            filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO |
            filters.Document.ALL | filters.VOICE | filters.AUDIO |
            filters.Sticker.ALL | filters.ANIMATION | filters.VIDEO_NOTE |
            filters.LOCATION | filters.VENUE
        ),
        handle_tg_channel_post
    ), group=1)
    # Handler for edited channel posts
    tg_app.add_handler(MessageHandler(
        filters.UpdateType.EDITED_CHANNEL_POST & (filters.TEXT | filters.CAPTION | filters.LOCATION),
        handle_tg_edited_channel_post
    ), group=1)
    # Handler for group messages
    tg_app.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & (
            filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO |
            filters.Document.ALL | filters.VOICE | filters.AUDIO |
            filters.Sticker.ALL | filters.ANIMATION | filters.POLL |
            filters.VIDEO_NOTE | filters.LOCATION | filters.VENUE
        ),
        handle_tg_message
    ), group=1)
    # Handler for edited group messages
    tg_app.add_handler(MessageHandler(
        filters.UpdateType.EDITED_MESSAGE & (filters.TEXT | filters.CAPTION | filters.LOCATION),
        handle_tg_edited_message
    ), group=1)
    # Handler for poll state changes
    from telegram.ext import PollHandler
    tg_app.add_handler(PollHandler(handle_tg_poll))
    
    # Handler for message reactions
    tg_app.add_handler(MessageReactionHandler(handle_tg_reaction))

    # Initialize Telegram Application with robust retry loop for start-up network/DNS resilience
    for attempt in range(1, 6):
        try:
            logger.info(f"Initializing Telegram Application (attempt {attempt}/5)...")
            await tg_app.initialize()
            break
        except Exception as e:
            if attempt == 5:
                raise e
            logger.warning(f"Telegram Application initialization failed (attempt {attempt}/5): {e}. Retrying in 5s...")
            await asyncio.sleep(5)

    await tg_app.start()
    
    # allowed_updates=Update.ALL_TYPES implicitly includes message_reaction
    await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    # Start DB cleanup loop and startup reconciliation
    async def startup_cleanup_task():
        await asyncio.sleep(10)
        try:
            logger.info("Running startup cleanup for stale/orphaned bridges...")
            await cleanup_stale_bridges()
        except Exception as e:
            logger.error(f"Startup bridge cleanup failed: {e}")

    asyncio.create_task(startup_cleanup_task())
    asyncio.create_task(db_cleanup_loop())
    asyncio.create_task(reconcile_channels_loop())

    # Start Userbot if configured
    await start_userbot()

    # 2. Start DC Bot in a background thread
    loop = asyncio.get_running_loop()
    dc_task = loop.run_in_executor(None, start_dc_bot)

    # Main heart-beat and sync loop
    import time
    last_sync = time.time()
    last_userbot_check = time.time()
    last_tg_bot_check = time.time()
    
    logger.info(f"Bridge v{VERSION} is now fully running. Waiting for events...")
    try:
        while True:
            await asyncio.sleep(10)
            now = time.time()
            
            # Watchdog for Userbot (check connectivity every 60 seconds)
            if (now - last_userbot_check) > 60:
                api_id = database.get_config("api_id")
                api_hash = database.get_config("api_hash")
                if api_id and api_hash:
                    # If client is None OR disconnected OR in a bad state (AttributeError scenario)
                    is_healthy = False
                    try:
                        # Improved health check: also ensure we can call an API method
                        async def check_health():
                            if userbot_client and userbot_client.is_connected() and await userbot_client.is_user_authorized():
                                # Trigger a real updates state request to ensure connection is actually responding and not cached
                                from telethon.tl.functions.updates import GetStateRequest
                                await userbot_client(GetStateRequest())
                                return True
                            return False
                        
                        is_healthy = await asyncio.wait_for(check_health(), timeout=15.0)
                    except (AttributeError, Exception) as e:
                        logger.warning(f"Userbot health check failed: {e}. Attempting restart...")
                    
                    if not is_healthy:
                        logger.info("Userbot client is offline or unhealthy. Restarting...")
                        # Run restart in background to not block the main heartbeat
                        asyncio.create_task(start_userbot())
                last_userbot_check = now

            # Watchdog for Telegram Bot API (check connectivity every 60 seconds)
            if (now - last_tg_bot_check) > 60:
                is_tg_healthy = False
                try:
                    if tg_app and tg_app.updater and tg_app.updater.running:
                        # Trigger a cheap API request with a short timeout to ensure responsiveness
                        await asyncio.wait_for(tg_app.bot.get_me(), timeout=10.0)
                        is_tg_healthy = True
                except Exception as e:
                    logger.warning(f"Telegram Bot health check failed: {e}. Attempting polling restart...")
                
                if not is_tg_healthy:
                    logger.info("Telegram Bot API polling is offline or unhealthy. Restarting polling...")
                    if tg_app and tg_app.updater:
                        if tg_app.updater.running:
                            logger.info("Stopping active unhealthy updater...")
                            try:
                                await asyncio.wait_for(tg_app.updater.stop(), timeout=15.0)
                            except Exception as stop_e:
                                logger.warning(f"Error/Timeout stopping updater: {stop_e}")
                        
                        for attempt in range(1, 4):
                            try:
                                logger.info(f"Starting updater polling (attempt {attempt}/3)...")
                                await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
                                logger.info("Telegram Bot API polling restarted successfully.")
                                break
                            except Exception as restart_err:
                                logger.warning(f"Telegram Bot polling restart attempt {attempt}/3 failed: {restart_err}")
                                if attempt < 3:
                                    await asyncio.sleep(5)
                                else:
                                    logger.error(f"Failed to restart Telegram Bot polling after 3 attempts: {restart_err}. Will retry on next watchdog check in 60s.")
                last_tg_bot_check = now

            if (now - last_sync) > 3600: # 1 hour interval
                if userbot_client and userbot_client.is_connected():
                    asyncio.create_task(sync_userbot_channels())
                last_sync = now
    except asyncio.CancelledError:
        pass

    # Cleanup when DC bot exits
    logger.info("Shutting down Telegram bot...")
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()
    logger.info("Shutdown complete.")


def run_cli():
    """Entry point body for `python bot.py ...`. Called only via the `if __name__ ==
    "__main__"` guard below, which re-imports this file under its canonical module
    name first — otherwise, running this file directly makes Python execute it a
    second time under the name "bot" the moment any sibling module does `import bot`
    (a `__main__` execution is never registered in sys.modules as "bot"), leaving
    every other module's `import bot` pointed at an inert, never-started duplicate
    with dc_bot_instance/tg_app/userbot_client stuck at None.
    """
    if "init" in sys.argv:
        init_idx = sys.argv.index("init")
        if len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "tg":
            if len(sys.argv) > init_idx + 2:
                database.set_config("telegram_token", sys.argv[init_idx + 2])
            else:
                token = getpass.getpass("Enter your Telegram Bot Token (input will be hidden): ").strip()
                database.set_config("telegram_token", token)
            print("Telegram token saved in bridge.db.")
            sys.exit(0)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "dc":
            new_args = sys.argv[init_idx + 2:]

            if len(new_args) < 1 or not new_args[0]:
                email = input("Delta Chat email: ").strip()
                password = getpass.getpass("Password (input will be hidden): ").strip()
                new_args = [email, password] if password else [email]
            elif len(new_args) < 2:
                password = getpass.getpass("Password (input will be hidden): ").strip()
                new_args = [new_args[0], password] if password else [new_args]

            name = input("Bot display name (default: TG Bridge): ").strip()
            if name:
                database.set_config("bot_displayname", name)

            avatar = input("Avatar file path or name (default: icon_deltachat.jpg): ").strip()
            if avatar:
                database.set_config("bot_avatar_path", avatar)

            # Strip 'dc' but keep 'init' so dc_cli sees the command and email/password
            sys.argv = [sys.argv[0], "init"] + new_args
            try:
                dc_cli.start()
            except SystemExit:
                pass
            sys.exit(0)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "admin_tg":
            if len(sys.argv) > init_idx + 2:
                database.set_config("admin_tg_id", sys.argv[init_idx + 2])
                print("Admin Telegram ID saved in bridge.db.")
            else:
                print("Usage: python bot.py init admin_tg <telegram_user_id>")
            sys.exit(0)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "admin_dc":
            if len(sys.argv) > init_idx + 2:
                database.set_config("admin_dc_email", sys.argv[init_idx + 2])
                # Clear fingerprint to allow "reset" if admin lost their key
                database.set_config("admin_dc_fingerprint", "")
                print("Admin Delta Chat email saved. (Fingerprint verification reset, use /initadmin in the bot to re-link securely).")
            else:
                print("Usage: python bot.py init admin_dc <deltachat_account_email>")
            sys.exit(0)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "api_id":
            if len(sys.argv) > init_idx + 2:
                database.set_config("api_id", sys.argv[init_idx + 2])
                print("API ID saved in bridge.db.")
            else:
                print("Usage: python bot.py init api_id <api_id>")
            sys.exit(0)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "api_hash":
            if len(sys.argv) > init_idx + 2:
                database.set_config("api_hash", sys.argv[init_idx + 2])
                print("API HASH saved in bridge.db.")
            else:
                print("Usage: python bot.py init api_hash <api_hash>")
            sys.exit(0)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "userbot":
            api_id = database.get_config("api_id")
            api_hash = database.get_config("api_hash")
            if not api_id or not api_hash:
                print("Error: Provide api_id and api_hash first before initializing userbot.")
                sys.exit(1)
            
            if not TelegramClient:
                print("Error: telethon is not installed.")
                sys.exit(1)
            
            print("Initializing userbot interactive login...")
            client = TelegramClient(USERBOT_SESSION_PATH, int(api_id), api_hash)
            client.start()
            print(f"Userbot session successfully created at {USERBOT_SESSION_PATH}.session")
            sys.exit(0)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "transport":
            if len(sys.argv) < init_idx + 3:
                print("Usage:")
                print("  python bot.py init transport DCACCOUNT:uri")
                print("  python bot.py init transport addr password")
                sys.exit(1)
            
            # We need to manually initialize RPC to add transport without starting the full bot loop
            from deltachat2 import Rpc, IOTransport
            from appdirs import user_config_dir
            
            config_dir = os.environ.get("DC_DB_DIR") or user_config_dir("tgbridge")
            accounts_dir = os.path.join(config_dir, "accounts")
            
            try:
                with IOTransport(accounts_dir=accounts_dir) as trans:
                    rpc = Rpc(trans)
                    accids = rpc.get_all_account_ids()
                    if not accids:
                        print("Error: No accounts configured. Run 'python bot.py init dc addr password' first.")
                        sys.exit(1)
                    accid = accids[0]
                    
                    payload = sys.argv[init_idx + 2]
                    if payload.startswith("DCACCOUNT:"):
                        rpc.add_transport_from_qr(accid, payload)
                        print(f"Success: Backup transport added via chatmail URI.")
                    elif len(sys.argv) >= init_idx + 4:
                        addr, password = sys.argv[init_idx + 2], sys.argv[init_idx + 3]
                        rpc.add_or_update_transport(accid, {"addr": addr, "password": password})
                        print(f"Success: Backup transport {addr} added.")
                    else:
                        print("Error: For email accounts, provide both address and password.")
                        sys.exit(1)
            except Exception as e:
                print(f"Error adding transport: {e}")
                sys.exit(1)
            sys.exit(0)
        else:
            print("Usage:")
            print("  python bot.py init dc <email> [password]  - Initialize Delta Chat account")
            print("  python bot.py init tg [token]             - Initialize Telegram bot token")
            print("  python bot.py init admin_tg <tg_id>       - Set Admin Telegram ID for error logs")
            print("  python bot.py init admin_dc <email>       - Set Admin Delta Chat email (resets fingerprint)")
            print("  python bot.py init transport <uri|addr>   - Add backup mail relay (transport)")
            print("  python bot.py init api_id <id>            - Set MTProto API ID for userbot")
            print("  python bot.py init api_hash <hash>        - Set MTProto API HASH for userbot")
            print("  python bot.py init userbot                - Interactive sign-in for userbot channels")
            sys.exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")


if __name__ == "__main__":
    import bot
    bot.run_cli()
