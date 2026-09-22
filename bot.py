import asyncio
import json
import logging
import os
import time
import threading
from typing import Optional

from deltachat2 import EventType, events
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, MessageReactionHandler, ChatMemberHandler

try:
    from telethon import TelegramClient
except ImportError:
    TelegramClient = None


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

from dc_commands import (
    VERSION,
    dc_cli,
    setprimary_command,
    resilient_command,
    richmode_command,
    help_command,
    initadmin_command,
    transports_command,
    addtransport_command,
    rmtransport_command,
    dc_donate_command,
    dc_channeladd_command,
    dc_channelremove_command,
    dc_botsend_command,
    dc_filters_command,
    dc_filteradd_command,
    dc_filterdel_command,
    bridge_command,
    unbridge_command,
    dc_cleanup_command,
    locupdate_command,
    dc_userbotsync_command,
    dc_userbotjoin_command,
    dc_status_command,
    dc_catchup_command,
    stats_command,
    channels_command_dc,
    channelssync_command_dc,
    _notify_and_remove_channel_bridge,
    _format_relative_time,
    generate_status_report,
)
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
_dc_cli_hooks_registered = False

def _register_dc_cli_hooks():
    """Register on_init/on_start/the dc_events.py handlers onto dc_cli.

    Called once from run_cli(), not applied as top-level @dc_cli.on(...)
    decorators, because bot.py's own module body runs twice per process
    (once as "__main__", then again as "bot" via the self-reimport in the
    `if __name__ == "__main__"` guard at the bottom of this file — see the
    comment on run_cli()). dc_cli itself lives in dc_commands.py and is
    cached across both runs, so a plain top-level decorator/registration
    here would register two distinct function objects (one per run) onto
    the same shared dc_cli, and each event would fire twice. Guarding with
    a module-level flag and calling this explicitly, once, from run_cli()
    avoids that regardless of how many times this module's top level runs.
    """
    global _dc_cli_hooks_registered
    if _dc_cli_hooks_registered:
        return
    _dc_cli_hooks_registered = True
    dc_cli.on_init(on_init)
    dc_cli.on_start(on_start)
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



# ---------------------------------------------------------
# DELTA CHAT HANDLERS
# ---------------------------------------------------------


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


# Save references for Telegram to use and print QR
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
    _register_dc_cli_hooks()
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
