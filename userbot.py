"""Telethon userbot subsystem: channel entity resolution, per-channel
reconciliation/catchup, membership sync, and the event handlers that
relay Telethon messages/edits/deletions into Delta Chat.

References to the pervasive bot.py singletons (userbot_client,
dc_bot_instance, dc_accid, tg_app) and to update_tg_channel_stats
(stays in bot.py) / _relay_userbot_message (relay.py, patched directly
on bot.py by the test suite) go through a function-local
`import bot as _bot_module`, same pattern as the rest of this refactor.

start_userbot() is the one function that *writes* userbot_client
(it's what creates the Telethon client), so every read AND write of it
there goes through `_bot_module.userbot_client` attribute access
rather than a bare name — attribute assignment on the imported module
avoids the local/global ambiguity a bare `global userbot_client;
userbot_client = ...` would otherwise still have needed, and correctly
shares the value with bot.py instead of creating a separate local.
"""
import asyncio
import html
import logging
import random
import threading
import time
from typing import Optional

import database
from deltachat2 import MsgData

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

from caching import (
    _get_cached_last_msg_id,
    _update_cached_last_msg_id,
    _get_cached_dc_channel_chat_id,
    _invalidate_dc_channel_cache,
    _get_content_hash,
)
from security import _is_deletion_rate_limited, DELETE_SYNC_MAX, DELETE_SYNC_WINDOW, _mark_processed, _is_edit_debounced
from logging_setup import _handle_channel_access_revoked, _send_admin_dc_message_bg

logger = logging.getLogger("tg_dc_bridge")


_userbot_tasks: set[asyncio.Task] = set()


_channel_queues: dict[int, asyncio.Queue] = {}


_channel_workers: dict[int, asyncio.Task] = {}


_is_starting_userbot = False


_is_syncing_userbot = False


async def _resolve_userbot_entity(tg_id: int | None = None, username: str | None = None, invite_link: str | None = None):
    """Robustly resolve a Telethon entity with fallbacks: numeric ID -> @username -> invite link."""
    import bot as _bot_module
    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
        return None
    
    entity = None
    # 1. Try numeric ID first (uses session cache)
    if tg_id:
        try:
            entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(tg_id), timeout=15.0)
        except Exception:
            pass
    
    # 2. Fallback to @username
    if not entity and username:
        try:
            target = f"@{username.lstrip('@')}"
            entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(target), timeout=15.0)
            await asyncio.sleep(1.0)
        except Exception:
            pass

    # 3. Fallback to invite link
    if not entity and invite_link and ("t.me/" in invite_link or "telegram.me/" in invite_link):
        try:
            entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(invite_link), timeout=15.0)
            await asyncio.sleep(1.0)
        except Exception:
            pass

    return entity


async def reconcile_channel(chan: dict, force: bool = False) -> tuple[int, int]:
    """
    Check a single bridged channel for missed posts, auto-join if needed, and queue catchup.
    Returns (messages_queued, total_missed).
    """
    import bot as _bot_module
    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
        return 0, 0

    tg_id = chan.get('tg_channel_id')
    username = chan.get('tg_channel_username')
    invite_link = chan.get('invite_link')
    dc_chat_id = chan.get('dc_chat_id')
    chan_db_id = chan.get('id')

    if not dc_chat_id:
        return 0, 0

    entity = await _resolve_userbot_entity(tg_id, username, invite_link)
    if not entity:
        desc = f"@{username}" if username else str(tg_id)
        logger.warning(f"Reconciliation: Could not resolve entity for channel {desc} (ID: {tg_id})")
        return 0, 0

    # Auto-join if userbot is not a member (so that MTProto live updates start arriving)
    is_user_or_bot = getattr(entity, 'bot', False) or (type(entity).__name__ in ('User', 'InputPeerUser', 'PeerUser'))
    if not is_user_or_bot and getattr(entity, 'left', True):
        try:
            if JoinChannelRequest:
                logger.info(f"Reconciliation: Userbot auto-joining channel {username or tg_id}...")
                await asyncio.wait_for(_bot_module.userbot_client(JoinChannelRequest(entity)), timeout=15.0)
                if chan_db_id:
                    await _bot_module.update_tg_channel_stats(chan_db_id, entity)
        except Exception as join_e:
            err_str = str(join_e)
            if any(k in err_str for k in ("UserBannedInChannelError", "ChannelPrivateError", "ChatAdminRequiredError", "USER_BANNED_IN_CHANNEL", "CHANNEL_PRIVATE", "Account is now banned")):
                asyncio.create_task(_handle_channel_access_revoked(tg_id or username, reason=err_str))
            logger.warning(f"Reconciliation: Userbot failed to join channel {username or tg_id}: {join_e}")

    # Update channel metadata if we now have more info
    if chan_db_id:
        try:
            await _bot_module.update_tg_channel_stats(chan_db_id, entity)
        except Exception:
            pass

    # Update numeric ID in DB if it was missing or resolved differently
    if not tg_id:
        try:
            from telethon.utils import get_peer_id
            resolved_tg_id = get_peer_id(entity)
            if resolved_tg_id and username:
                database.update_channel_tg_id(username, resolved_tg_id)
                tg_id = resolved_tg_id
        except Exception:
            pass

    if not tg_id:
        return 0, 0

    last_id = _get_cached_last_msg_id(tg_id)

    # Fetch latest post
    try:
        history = await asyncio.wait_for(_bot_module.userbot_client.get_messages(entity, limit=1), timeout=15.0)
    except Exception as e:
        err_str = str(e)
        if any(k in err_str for k in ("UserBannedInChannelError", "ChannelPrivateError", "ChatAdminRequiredError", "USER_BANNED_IN_CHANNEL", "CHANNEL_PRIVATE", "Account is now banned")):
            asyncio.create_task(_handle_channel_access_revoked(tg_id or username, reason=err_str))
        logger.warning(f"Reconciliation: Failed to get latest message for channel {username or tg_id}: {e}")
        return 0, 0

    if not history:
        return 0, 0

    latest_msg = history[0]
    latest_id = latest_msg.id

    if last_id <= 0:
        # Initialize last_id to the latest post if not initialized yet
        logger.info(f"Reconciliation: Initializing channel {tg_id} (@{username}) last_msg_id to {latest_id}")
        _update_cached_last_msg_id(tg_id, latest_id)
        return 0, 0

    if latest_id <= last_id:
        return 0, 0

    missed_count = latest_id - last_id
    fetch_limit = min(missed_count, 100)
    logger.info(f"Reconciliation: Channel {tg_id} (@{username}) missed {missed_count} messages (last={last_id}, latest={latest_id}). Fetching {fetch_limit} oldest-first.")

    # Fetch in ascending chronological order with reverse=True
    try:
        missed_msgs = await asyncio.wait_for(
            _bot_module.userbot_client.get_messages(entity, min_id=last_id, limit=fetch_limit, reverse=True),
            timeout=20.0
        )
    except Exception as e:
        logger.warning(f"Reconciliation: Failed to fetch missed messages for {username or tg_id}: {e}")
        return 0, missed_count

    queued = 0
    if missed_msgs:
        for msg in sorted(missed_msgs, key=lambda m: m.id):
            if dc_chat_id and database.get_dc_msg_id(msg.id, tg_id, dc_chat_id):
                logger.info(f"Reconciliation: Message {msg.id} in channel {tg_id} already exists in message_map, advancing watermark.")
                _update_cached_last_msg_id(tg_id, msg.id)
                continue
            class MockEvent:
                def __init__(self, message, cid):
                    self.message = message
                    self.chat_id = cid
            await _bot_module._queue_userbot_event(tg_id, 'new', MockEvent(msg, tg_id))
            queued += 1

    return queued, missed_count


async def run_channel_catchup(target: str | None = None, is_html: bool = False) -> str:
    """Run reconciliation for a single channel or all channels on demand."""
    import bot as _bot_module
    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
        return "❌ Userbot is not connected." if not is_html else "❌ <b>Userbot is not connected.</b>"

    channels = database.get_all_channels()
    if not channels:
        return "No channels configured." if not is_html else "<i>No channels configured.</i>"

    if target:
        clean_target = target.strip().lstrip('@').lower()
        matched = []
        for ch in channels:
            ch_uname = (ch.get('tg_channel_username') or '').lower()
            ch_id = str(ch.get('tg_channel_id') or '')
            ch_db_id = str(ch.get('id') or '')
            if clean_target in (ch_uname, ch_id, ch_id.replace('-100', ''), ch_db_id):
                matched.append(ch)
        if not matched:
            return f"❌ Channel '{target}' not found." if not is_html else f"❌ Channel '<code>{html.escape(target)}</code>' not found."
        target_channels = matched
    else:
        target_channels = channels

    results = []
    total_queued = 0
    total_missed = 0
    caught_up_channels = 0

    for chan in target_channels:
        tg_id = chan.get('tg_channel_id')
        username = chan.get('tg_channel_username')
        display_name = f"@{username}" if username else f"ID {tg_id}"

        try:
            queued, missed = await reconcile_channel(chan, force=True)
            if queued > 0:
                caught_up_channels += 1
                total_queued += queued
                total_missed += missed
                results.append(f"• {display_name}: queued {queued} missed posts (out of {missed})")
            elif missed > 0:
                results.append(f"• {display_name}: {missed} posts missed, but 0 queued")
        except Exception as e:
            results.append(f"• {display_name}: error {e}")
        
        await asyncio.sleep(0.5)

    summary_header = f"📡 **Catchup Finished** ({len(target_channels)} channels checked)\n" if not is_html else f"📡 <b>Catchup Finished</b> ({len(target_channels)} channels checked)\n"
    if caught_up_channels == 0:
        summary_body = "✅ All checked channels are already up to date." if not is_html else "✅ <i>All checked channels are already up to date.</i>"
    else:
        summary_body = f"📥 Caught up {caught_up_channels} channel(s), queued {total_queued} missed posts.\n\n" + "\n".join(results)

    return summary_header + summary_body


async def reconcile_channels_loop():
    """Periodically check all bridged channels for any missed messages."""
    import bot as _bot_module
    await asyncio.sleep(60)  # Wait 60 seconds on startup
    while True:
        try:
            if _bot_module.userbot_client and _bot_module.userbot_client.is_connected():
                channels = database.get_all_channels()
                for chan in channels:
                    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
                        logger.info("Userbot disconnected during channel reconciliation. Aborting pass.")
                        break

                    tg_id = chan.get('tg_channel_id')
                    username = chan.get('tg_channel_username')
                    try:
                        await reconcile_channel(chan)
                    except Exception as e:
                        err_str = str(e)
                        if "NoneType" in err_str or "disconnected" in err_str.lower() or "not connected" in err_str.lower():
                            logger.info(f"Userbot disconnected during channel reconciliation pass for channel {tg_id}: {e}. Aborting pass.")
                            break
                        logger.warning(f"Reconciliation failed for channel {tg_id} (@{username}): {e}")
                    
                    # Small delay between channels to avoid rate limits
                    await asyncio.sleep(2.0)
            
            # Sleep 3 minutes before checking again
            await asyncio.sleep(180)
        except Exception as e:
            logger.error(f"Reconciliation loop error: {e}")
            await asyncio.sleep(60)


async def sync_userbot_channels(force=False):
    """
    Ensures the userbot is a member of all bridged channels.
    Runs when a new account is detected or manually triggered.
    """
    import bot as _bot_module
    global _is_syncing_userbot
    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
        return
    
    if _is_syncing_userbot and not force:
        logger.info("Userbot sync is already in progress.")
        return

    _is_syncing_userbot = True
    try:
        me = await asyncio.wait_for(_bot_module.userbot_client.get_me(), timeout=15.0)
        if not me:
            return

        logger.info(f"Starting Userbot sync for account {me.id} (@{getattr(me, 'username', 'N/A')})...")
        channels = database.get_all_channels()
        
        joined_count = 0
        failed_reports = []
        
        for chan in channels:
            tg_id = chan.get('tg_channel_id')
            username = chan.get('tg_channel_username')
            invite_link = chan.get('invite_link')
            target = f"@{username}" if username else str(tg_id)

            try:
                entity = await _resolve_userbot_entity(tg_id, username, invite_link)
                if not entity:
                    raise Exception(f"Could not resolve {target} (and no working invite link).")

                is_user_or_bot = getattr(entity, 'bot', False) or (type(entity).__name__ in ('User', 'InputPeerUser', 'PeerUser'))
                if not is_user_or_bot and getattr(entity, 'left', True):
                    logger.info(f"Userbot: Joining channel {target}...")
                    if JoinChannelRequest:
                        await asyncio.wait_for(_bot_module.userbot_client(JoinChannelRequest(entity)), timeout=15.0)
                        joined_count += 1
                        # Update stats after joining
                        chan_id = chan.get('id')
                        if chan_id:
                            await _bot_module.update_tg_channel_stats(chan_id, entity)
                        # Human-like delay
                        delay = random.uniform(5, 20)
                        await asyncio.sleep(delay)
                else:
                    # Already a member, update stats
                    chan_id = chan.get('id')
                    if chan_id:
                        await _bot_module.update_tg_channel_stats(chan_id, entity)
            except Exception as e:
                logger.warning(f"Userbot: Could not sync/join channel {target}: {e}")
                chan_id = chan.get('id', '?')
                tg_id = chan.get('tg_channel_id', '?')
                username = chan.get('tg_channel_username')
                # Try to get title from DB if we know it
                title = "Unknown Channel"
                try:
                    # We might have stored the title in our DB previously
                    # But if not, we just use the ID
                    title = f"Channel #{chan_id}"
                except Exception:
                    pass
                
                # Make ID clickable/searchable
                clean_id = str(tg_id).replace("-100", "")
                report = f"• <b>{html.escape(title)}</b> (ID: <code>{tg_id}</code>)"
                if username:
                    report += f" (@{html.escape(username)})"
                else:
                    report += f" — <a href='https://t.me/c/{clean_id}/1'>Search Link</a>"
                failed_reports.append(report)
        
        # Save last successful sync user ID
        database.set_config("userbot_last_user_id", str(me.id))
        logger.info(f"Userbot sync completed. Joined {joined_count} channels.")
        
        # Notify owner if there are failed channels (skip repeating warnings on periodic syncs)
        if failed_reports and (force or joined_count > 0):
            admin_tg_id = database.get_config("admin_tg_id")
            if admin_tg_id and _bot_module.tg_app:
                try:
                    report_text = (
                        f"⚠️ <b>Userbot Sync Summary</b>\n\n"
                        f"Completed. Joined {joined_count} channels.\n"
                        f"The following {len(failed_reports)} channels require <b>manual join</b> on your new technical account:\n\n"
                        + "\n".join(failed_reports) +
                        f"\n\n<i>Note: Click the ID links to find the group if you are already a member, or search for these IDs in your Telegram history.</i>"
                    )
                    # Chunk it if too long
                    if len(report_text) > 4000:
                        report_text = report_text[:3900] + "...\n(List too long)"
                    
                    await _bot_module.tg_app.bot.send_message(chat_id=int(admin_tg_id), text=report_text, parse_mode='HTML')
                except Exception as notify_e:
                    logger.error(f"Failed to send sync report to owner: {notify_e}")
        
    finally:
        _is_syncing_userbot = False


async def _process_userbot_deletion_internal(event):
    """Handle Telethon MessageDeleted events and sync deletions to Delta Chat."""
    import bot as _bot_module

    if not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        return

    # event.deleted_ids: list of deleted TG message IDs
    # event.chat_id: the chat where messages were deleted (may be None for private chats)
    chat_id = getattr(event, 'chat_id', None)
    deleted_ids = getattr(event, 'deleted_ids', []) or []

    if not deleted_ids or not chat_id:
        return

    for tg_msg_id in deleted_ids:
        # Look up all DC messages that mirror this TG message
        dc_pairs = database.get_dc_msgs_by_tg_msg_id(tg_msg_id, chat_id)
        if not dc_pairs:
            continue

        for dc_msg_id, dc_chat_id in dc_pairs:
            # Clean up the mapping immediately to prevent echo loops
            database.delete_message_map_entry_by_tg(tg_msg_id, chat_id)

            if _is_deletion_rate_limited():
                logger.warning(
                    f"TG→DC deletion sync rate limit hit ({DELETE_SYNC_MAX}/{DELETE_SYNC_WINDOW}s). "
                    f"Skipping deletion of DC msg {dc_msg_id}."
                )
                admin_tg_id = database.get_config("admin_tg_id")
                if admin_tg_id and _bot_module.tg_app:
                    try:
                        await _bot_module.tg_app.bot.send_message(
                            chat_id=int(admin_tg_id),
                            text=f"⚠️ <b>Bulk TG→DC deletion blocked</b>\nMore than {DELETE_SYNC_MAX} messages deleted in {DELETE_SYNC_WINDOW}s.\nTG msg {tg_msg_id} → DC msg {dc_msg_id} was <b>NOT</b> removed from Delta Chat.",
                            parse_mode='HTML'
                        )
                    except Exception:
                        pass
                return

            try:
                _bot_module.dc_bot_instance.rpc.delete_messages(_bot_module.dc_accid, [dc_msg_id])
                logger.info(f"TG→DC: Deleted DC msg {dc_msg_id} (mirrored TG msg {tg_msg_id} in {chat_id})")
            except Exception as e:
                logger.warning(f"TG→DC: Could not delete DC msg {dc_msg_id}: {e}")


async def _queue_userbot_event(tg_channel_id: int, event_type: str, event_data: any):
    """Queue a channel event to process it sequentially."""
    global _channel_queues, _channel_workers
    if tg_channel_id not in _channel_queues:
        _channel_queues[tg_channel_id] = asyncio.Queue()
    
    if tg_channel_id not in _channel_workers or _channel_workers[tg_channel_id].done():
        worker_task = asyncio.create_task(_channel_queue_worker(tg_channel_id))
        _channel_workers[tg_channel_id] = worker_task
        
    await _channel_queues[tg_channel_id].put((event_type, event_data))


async def _channel_queue_worker(tg_channel_id: int):
    """Worker task that processes a single channel's events sequentially."""
    global _channel_queues, _userbot_tasks
    queue = _channel_queues.get(tg_channel_id)
    if not queue:
        return
    try:
        while True:
            event_type, event_data = await queue.get()
            current_task = asyncio.current_task()
            if current_task:
                _userbot_tasks.add(current_task)
            try:
                if event_type == 'new':
                    await _process_userbot_event_internal(event_data, is_edit=False)
                elif event_type == 'edit':
                    await _process_userbot_event_internal(event_data, is_edit=True)
                elif event_type == 'delete':
                    await _process_userbot_deletion_internal(event_data)
            except Exception as e:
                logger.error(f"Error processing queued event for channel {tg_channel_id}: {e}")
            finally:
                if current_task:
                    _userbot_tasks.discard(current_task)
                queue.task_done()
    except asyncio.CancelledError:
        pass


async def _process_userbot_deletion(event):
    """Handle Telethon MessageDeleted events and sync deletions to Delta Chat (queues events)."""
    import bot as _bot_module
    chat_id = getattr(event, 'chat_id', None)
    if chat_id:
        await _bot_module._queue_userbot_event(chat_id, 'delete', event)


async def _process_userbot_event(event, is_edit=False):
    """Common logic for userbot new and edited posts (queues events per channel)."""
    import bot as _bot_module
    chat_id = getattr(event, 'chat_id', None)
    if chat_id:
        await _bot_module._queue_userbot_event(chat_id, 'edit' if is_edit else 'new', event)


async def _process_userbot_event_internal(event, is_edit=False):
    """Internal logic for userbot events."""
    import bot as _bot_module
    msg = event.message
    if not msg:
        return

    # Safety check: ensure client is still active
    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
        return

    # Forward login/verification codes from Telegram's service account (ID 777000) to admin
    sender_id = getattr(msg, 'sender_id', None) or getattr(msg, 'from_id', None)
    if sender_id == 777000 and msg.text:
        logger.info("Userbot: Received message from Telegram service account (login code)")
        code_text = f"🔐 *Login code for technical account:*\n\n`{msg.text}`"
        admin_tg_id = database.get_config("admin_tg_id")
        if admin_tg_id and _bot_module.tg_app:
            try:
                await _bot_module.tg_app.bot.send_message(
                    chat_id=int(admin_tg_id),
                    text=code_text,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Failed to forward login code to TG admin: {e}")
        # Also send to DC admin
        admin_dc_email = database.get_config("admin_dc_email")
        if admin_dc_email and _bot_module.dc_bot_instance and _bot_module.dc_accid:
            dc_code_text = f"🔐 Login code for technical account:\n\n{msg.text}"
            threading.Thread(target=_send_admin_dc_message_bg, args=(dc_code_text,), daemon=True).start()
        return  # Don't process further

    # Ignore outgoing messages sent by the userbot itself (e.g. /start or /botsend)
    if getattr(msg, 'out', False):
        return

    tg_channel_id = msg.chat_id
    if _mark_processed(tg_channel_id, msg.id):
        return

    if is_edit:
        if _is_edit_debounced(tg_channel_id, msg.id):
            return

        # Check message age (older than 7 days)
        try:
            from datetime import datetime, timezone
            msg_date = getattr(msg, 'date', None)
            if msg_date:
                now = datetime.now(timezone.utc)
                age = now - msg_date
                if age.days > 7:
                    logger.info(f"Userbot: Skipping edit relay for post {msg.id} in channel {tg_channel_id} because it is older than 7 days ({age.days} days).")
                    return
        except Exception as e:
            logger.warning(f"Userbot: Failed to check message age for post {msg.id}: {e}")

    # Check database
    dc_chat_id = _get_cached_dc_channel_chat_id(tg_channel_id)

    # Fallback: if not found by ID, try finding by username (useful if added via Bot API without ID)
    if not dc_chat_id:
        chat_username = getattr(msg.chat, 'username', None)
        if chat_username:
            chan_data = database.get_channel_by_tg_username(chat_username)
            if chan_data:
                dc_chat_id = chan_data['dc_chat_id']
                logger.info(f"USERBOT: Found matching channel by username @{chat_username}. Updating numeric ID to {tg_channel_id}...")
                database.update_channel_tg_id(chat_username, tg_channel_id)
                _invalidate_dc_channel_cache(tg_channel_id)

    if not dc_chat_id or not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        return

    # De-duplication: check message_map directly (skip for edits)
    if not is_edit:
        existing_dc_msg_id = database.get_dc_msg_id(msg.id, tg_channel_id, dc_chat_id)
        if existing_dc_msg_id:
            logger.info(f"USERBOT: Post {msg.id} in channel {tg_channel_id} already exists in message_map (dc_msg_id={existing_dc_msg_id}). Updating watermark and skipping.")
            _update_cached_last_msg_id(tg_channel_id, msg.id)
            return

        last_msg_id = _get_cached_last_msg_id(tg_channel_id)
        if last_msg_id > 0 and msg.id <= last_msg_id:
            logger.info(f"USERBOT: Skipping already relayed/old post {msg.id} in channel {tg_channel_id} (last_msg_id is {last_msg_id})")
            return

    # Content-based change detection (prevent ghost edits from reactions/views)
    new_hash = _get_content_hash(msg)
    old_hash = database.get_message_content_hash(msg.id, tg_channel_id, dc_chat_id)
    
    if is_edit and old_hash and old_hash == new_hash:
        # Content hasn't changed, ignore metadata update (reactions, views)
        return

    # For broadcast channel edits: if post was never relayed to DC before, relay cleanly as a fresh post
    is_broadcast_channel = bool(database.get_channel_by_tg_id(tg_channel_id) or database.get_channel_by_dc_chat_id(dc_chat_id) or (getattr(msg, 'is_channel', False) and not getattr(msg, 'is_group', False)))
    if is_edit and is_broadcast_channel:
        existing_dc_msg_id = database.get_dc_msg_id(msg.id, tg_channel_id, dc_chat_id)
        if not existing_dc_msg_id:
            # Post was never relayed to DC before; relay cleanly as a fresh post without [Edited]
            is_edit = False

    # Relay the message using the shared helper
    await _bot_module._relay_userbot_message(dc_chat_id, msg, is_edit=is_edit)


async def start_userbot():
    """Initialize and start the Telethon Userbot client."""
    import bot as _bot_module
    global _userbot_tasks, _is_starting_userbot, _channel_workers, _channel_queues
    if not TelegramClient:
        return
    
    if _is_starting_userbot:
        logger.info("Userbot start/restart is already in progress, skipping.")
        return

    _is_starting_userbot = True
    api_id = database.get_config("api_id")
    api_hash = database.get_config("api_hash")
    if not (api_id and api_hash):
        _is_starting_userbot = False
        return

    try:
        # 1. Stop and clear all pending tasks and queues from the old client
        if _channel_workers:
            logger.info(f"Cancelling {len(_channel_workers)} active channel queue worker tasks...")
            for task in _channel_workers.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*_channel_workers.values(), return_exceptions=True)
            _channel_workers.clear()
            _channel_queues.clear()

        if _userbot_tasks:
            logger.info(f"Cancelling {len(_userbot_tasks)} pending Userbot tasks during restart...")
            for task in _userbot_tasks:
                if not task.done():
                    task.cancel()
            
            # Wait for tasks to actually exit
            await asyncio.gather(*_userbot_tasks, return_exceptions=True)
            _userbot_tasks.clear()

        # 2. Disconnect existing client
        if _bot_module.userbot_client:
            try:
                session = getattr(_bot_module.userbot_client, 'session', None)
                # Use a timeout to ensure we don't hang during shutdown
                await asyncio.wait_for(_bot_module.userbot_client.disconnect(), timeout=10)
                if session and hasattr(session, 'close'):
                    session.close()
            except Exception as e:
                logger.warning(f"Error disconnecting old Userbot client: {e}")
                # Force close the SQLite session database connection if it exists
                session = getattr(_bot_module.userbot_client, 'session', None)
                if session and hasattr(session, 'close'):
                    try:
                        session.close()
                    except Exception:
                        pass
            finally:
                _bot_module.userbot_client = None

        # Force garbage collection to clean up any unreferenced client/connection tasks
        import gc
        gc.collect()
        await asyncio.sleep(1.0)
        
        logger.info("Initializing new Telethon Userbot client...")
        _bot_module.userbot_client = TelegramClient(
            _bot_module.USERBOT_SESSION_PATH,
            int(api_id),
            api_hash,
            sequential_updates=False,
            connection_retries=5,
            auto_reconnect=True,
            flood_sleep_threshold=60,
            retry_delay=1
        )
        
        @_bot_module.userbot_client.on(tg_events.NewMessage())
        async def on_new_userbot_msg(event):
            asyncio.create_task(_process_userbot_event(event, is_edit=False))
            
        @_bot_module.userbot_client.on(tg_events.MessageEdited())
        async def on_edited_userbot_msg(event):
            asyncio.create_task(_process_userbot_event(event, is_edit=True))

        @_bot_module.userbot_client.on(tg_events.MessageDeleted())
        async def on_deleted_userbot_msg(event):
            asyncio.create_task(_process_userbot_deletion(event))

        await asyncio.wait_for(_bot_module.userbot_client.start(), timeout=60.0)
        logger.info("Telethon Userbot client started successfully.")
        
        # Auto-sync detection
        me = await asyncio.wait_for(_bot_module.userbot_client.get_me(), timeout=15.0)
        if me:
            last_id = database.get_config("userbot_last_user_id")
            if last_id != str(me.id):
                logger.info(f"New Userbot account detected ({me.id}). Scheduling auto-sync in 10s to allow connection to stabilize...")
                database.set_config("userbot_last_user_id", str(me.id))
                async def _delayed_sync():
                    await asyncio.sleep(10)
                    await sync_userbot_channels()
                asyncio.create_task(_delayed_sync())
    except Exception as e:
        logger.warning(f"Failed to start Userbot client: {e}")
        # Explicitly close the new client's session to release the lock!
        if _bot_module.userbot_client:
            session = getattr(_bot_module.userbot_client, 'session', None)
            if session and hasattr(session, 'close'):
                try:
                    session.close()
                except Exception:
                    pass
        _bot_module.userbot_client = None
    finally:
        _is_starting_userbot = False

