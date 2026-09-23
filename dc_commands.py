"""All Delta Chat /command handlers (setprimary, resilient, richmode,
help, initadmin, transports, addtransport, rmtransport, donate,
channeladd/remove, botsend, filters/filteradd/filterdel, bridge/
unbridge, cleanup, locupdate, userbotsync/userbotjoin, status,
catchup/reconcile, stats, channels, channelssync/channelsync), plus
the dc_cli object itself and VERSION.

dc_cli is created here (not in bot.py) because these ~26 handlers are
decorated with @dc_cli.on(...) at module-load time, and a decorator
target must exist before the decoration runs — the same reasoning
dc_events.py's docstring explains for why ITS handlers are registered
explicitly in bot.py instead. Since dc_cli and all of its decorated
handlers live together in this one file, the decorators work
normally; bot.py imports dc_cli from here instead of creating it,
and does so early enough (right where dc_cli used to be defined) to
support its own @dc_cli.on_init/@dc_cli.on_start decorators and the
explicit dc_events.py handler registrations added in that extraction
step.

Every reference to the pervasive bot.py singletons and to functions
the test suite patches directly on bot.py (or that still live in
bot.py, like cleanup_stale_bridges/update_tg_channel_stats) goes
through a function-local `import bot as _bot_module`, same pattern as
the rest of this refactor.
"""
import asyncio
import html
import json
import logging
import os
import re
import time
from typing import Optional

import database
from deltachat2 import EventType, MsgData, SystemMessageType, events
from deltabot_cli import BotCli

try:
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
except ImportError:
    JoinChannelRequest = None
    ImportChatInviteRequest = None
    CheckChatInviteRequest = None

from formatting import get_dc_help_text, to_dc_markdown
from security import _reload_filter_cache
from caching import _clear_dc_caches, _invalidate_dc_channel_cache, invalidate_channels_cache, _get_cached_last_msg_id
from live_locations import LIVE_LOCATIONS
from dc_helpers import async_update_channels_dc, _userbot_leave_chat
from tg_events import _add_channel_bridge, _send_bot_message
from userbot import sync_userbot_channels, run_channel_catchup, _channel_workers, _channel_queues

logger = logging.getLogger("tg_dc_bridge")

VERSION = "2.25.7"
dc_cli = BotCli("tgbridge")


@dc_cli.on(events.NewMessage(command="/setprimary"))
def setprimary_command(bot, accid, event):
    """Set a specific transport as primary. Admin only."""
    import bot as _bot_module
    msg = event.msg
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /setprimary."))
        return

    addr = event.payload.strip()
    if not addr:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: /setprimary user@example.com"))
        return

    try:
        bot.rpc.set_config(accid, "configured_addr", addr)
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Primary address (`configured_addr`) is now `{addr}`."))
    except Exception as e:
        logger.error(f"Failed to set primary address: {e}")
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to set primary address."))


@dc_cli.on(events.NewMessage(command="/resilient"))
def resilient_command(bot, accid, event):
    import bot as _bot_module
    msg = event.msg
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /resilient."))
        return

    arg = event.payload.strip().lower() if event.payload else ""

    try:
        current = database.get_config("resilient") == "1"
        if not arg:
            status = "enabled" if current else "disabled"
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"ℹ️ Resilient sending mode is currently {status}."))
            return

        if arg in ("on", "1", "true"):
            database.set_config("resilient", "1")
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="✅ Resilient sending mode enabled. Each outgoing message will be sent via all connected transports."))
        elif arg in ("off", "0", "false"):
            database.set_config("resilient", "0")
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="ℹ️ Resilient sending mode disabled."))
        else:
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Invalid argument. Use '/resilient on', '/resilient off', or '/resilient' to get status."))
    except Exception as e:
        logger.error(f"Failed to update resilient mode: {e}")
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to update resilient mode."))


@dc_cli.on(events.NewMessage(command="/richmode"))
def richmode_command(bot, accid, event):
    """Configure Telegram rich post handling mode (webxdc, split, both, off)."""
    import bot as _bot_module
    msg = event.msg
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /richmode."))
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
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=status_text))
            return

        if arg in ("webxdc", "split", "both", "off"):
            database.set_rich_mode(arg)
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Telegram rich post relay mode set to **{arg}**."))
        else:
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Invalid mode. Use `/richmode webxdc`, `/richmode split`, `/richmode both`, or `/richmode off`."))
    except Exception as e:
        logger.error(f"Failed to update richmode: {e}")
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to update richmode."))


@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    """Reply with help text."""
    import bot as _bot_module
    msg = event.msg
    
    # Get sender info
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    
    help_msg = get_dc_help_text(bot, accid, sender_email, msg.from_id)
    _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=help_msg))


@dc_cli.on(events.NewMessage(command="/initadmin"))
def initadmin_command(bot, accid, event):
    """Claim bot ownership in private chat (binds email & cryptographic fingerprint)."""
    import bot as _bot_module
    msg = event.msg
    if not _bot_module._is_private_chat(bot, accid, msg.chat_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ For security reasons, /initadmin can only be used in a private 1:1 chat with the bot."))
        return

    admin_email = database.get_admin_email()
    admin_fp = database.get_admin_fingerprint()

    if admin_email or admin_fp:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Admin is already set. Use `set_admin.py` on the server to change."))
        return

    contact = bot.rpc.get_contact(accid, msg.from_id)
    email = contact.address
    database.set_admin_email(email)

    fp = _bot_module._get_contact_fingerprint(bot, accid, msg.from_id, contact=contact)
    if fp:
        first_fp = fp.split(',')[0]
        database.set_admin_fingerprint(first_fp)
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=
            f"✅ You are now the admin!\n\nEmail: `{email}`\nFingerprint: `{first_fp[-8:]}`"))
    else:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=
            f"✅ You are now the admin!\n\nEmail: `{email}`\n⚠️ Fingerprint not available yet (will be used after key exchange)."))


@dc_cli.on(events.NewMessage(command="/transports"))
def transports_command(bot, accid, event):
    """Show configured transports (mail relays) and their status."""
    import bot as _bot_module
    msg = event.msg
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /transports."))
        return

    try:
        transports = bot.rpc.list_transports(accid)
    except Exception as e:
        logger.error(f"Failed to list transports: {e}")
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to list transports."))
        return

    if not transports:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="No transports configured."))
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
    _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=reply))


@dc_cli.on(events.NewMessage(command="/addtransport"))
def addtransport_command(bot, accid, event):
    """Add a backup mail relay (transport). Admin only."""
    import bot as _bot_module
    msg = event.msg
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /addtransport."))
        return

    if not _bot_module._is_private_chat(bot, accid, msg.chat_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ For security reasons, /addtransport can only be used in a private 1:1 chat with the bot."))
        return

    payload = event.payload.strip()
    if not payload:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(
            text="Usage:\n"
                 "/addtransport DCACCOUNT:server.example\n"
                 "/addtransport user@example.com password123"
        ))
        return

    try:
        if payload.startswith("DCACCOUNT:"):
            bot.rpc.add_transport_from_qr(accid, payload)
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Backup transport added via chatmail URI."))
        else:
            parts = payload.split(None, 1)
            if len(parts) < 2:
                _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(
                    text="❌ For email accounts, provide both address and password:\n"
                         "/addtransport user@example.com password123"
                ))
                return
            addr, password = parts[0], parts[1]
            bot.rpc.add_or_update_transport(accid, {"addr": addr, "password": password})
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Backup transport `{addr}` added."))
    except Exception as e:
        logger.error(f"Failed to add transport: {e}")
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to add transport."))


@dc_cli.on(events.NewMessage(command="/rmtransport"))
def rmtransport_command(bot, accid, event):
    """Remove a mail relay (transport). Admin only."""
    import bot as _bot_module
    msg = event.msg
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /rmtransport."))
        return

    addr = event.payload.strip()
    if not addr:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: /rmtransport user@example.com"))
        return

    try:
        transports = bot.rpc.list_transports(accid)
        transport_addrs = []
        for t in transports:
            a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
            transport_addrs.append(a)
        if len(transport_addrs) <= 1:
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Cannot remove the last transport."))
            return
        if addr not in transport_addrs:
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"❌ Transport `{addr}` not found."))
            return
    except Exception as e:
        logger.error(f"Failed to check transports: {e}")
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to check transports."))
        return

    try:
        bot.rpc.delete_transport(accid, addr)
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Transport `{addr}` removed."))
    except Exception as e:
        logger.error(f"Failed to remove transport: {e}")
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Failed to remove transport."))


@dc_cli.on(events.NewMessage(command="/donate"))
def dc_donate_command(bot, accid, event):
    """Reply with donate link."""
    import bot as _bot_module
    msg = event.msg
    support_msg = (
        "❤️ Support Bot Development\n\n"
        "If you find this bridge useful, you can support its development and server costs here:\n\n"
        "☕️ Ko-fi: https://ko-fi.com/gluek (🌍 world cards, paypal, no commissions)\n"
        "🚀 Tribute: https://web.tribute.tg/d/IWb (🇷🇺 russian cards, SBP, high commissions)\n\n"
        "Thank you! 🙏"
    )
    _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=support_msg))


@dc_cli.on(events.NewMessage(command="/channeladd"))
def dc_channeladd_command(bot, accid, event):
    """Add a channel bridge from Delta Chat. Admin only."""
    import bot as _bot_module
    msg = event.msg
    payload = event.payload.strip()
    
    # Admin check
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can manage channels."))
        return

    if not payload:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: /channeladd @username or t.me link"))
        return

    # Use run_coroutine_threadsafe since dc_cli hooks might run in a separate thread
    async def run_add():
        status_id = _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="⏳ Processing channel bridge..."))
        result = await _add_channel_bridge(payload)
        # Convert HTML response to Markdown for DC
        result_md = to_dc_markdown(result)
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=result_md))
    
    if _bot_module.main_loop:
        asyncio.run_coroutine_threadsafe(run_add(), _bot_module.main_loop)
    else:
        logger.error("Main loop not found, cannot run channeladd")


@dc_cli.on(events.NewMessage(command="/channelremove"))
@dc_cli.on(events.NewMessage(command="/channeldelete"))
def dc_channelremove_command(bot, accid, event):
    """Remove a channel bridge from Delta Chat. Admin only."""
    import bot as _bot_module
    msg = event.msg
    payload = event.payload.strip()
    
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can manage channels."))
        return

    if not payload:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: /channelremove N (channel number)"))
        return

    try:
        channel_id = int(payload)
        ch = database.get_channel_by_id(channel_id)
        if not ch:
             _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"❌ Channel #{channel_id} not found."))
             return
             
        tg_channel_id = _notify_and_remove_channel_bridge(ch)
        if tg_channel_id:
             _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Channel bridge #{channel_id} removed."))
        else:
             _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to remove channel #{channel_id}."))
    except ValueError:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Invalid channel number."))


@dc_cli.on(events.NewMessage(command="/botsend"))
def dc_botsend_command(bot, accid, event):
    """Send a command or message to a Telegram bot via Userbot. Admin only."""
    import bot as _bot_module
    msg = event.msg
    payload = event.payload.strip()

    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /botsend."))
        return

    parts = payload.split(maxsplit=1)
    if len(parts) < 2:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: /botsend @bot_username <message> or /botsend <channel_id> <message>"))
        return

    target, cmd_text = parts[0], parts[1]

    async def run_botsend():
        res = await _send_bot_message(target, cmd_text)
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=to_dc_markdown(res)))

    if _bot_module.main_loop:
        asyncio.run_coroutine_threadsafe(run_botsend(), _bot_module.main_loop)
    else:
        logger.error("Main loop not found, cannot run botsend")


@dc_cli.on(events.NewMessage(command="/filters"))
def dc_filters_command(bot, accid, event):
    """List all active message filters. Admin only."""
    import bot as _bot_module
    msg = event.msg
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can manage filters."))
        return

    filters = database.get_all_filters()
    if not filters:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="📋 **Message Filters**\nNo message filters configured.\n\nTo add a filter:\n`/filteradd <word or phrase>`"))
        return

    lines = [f"📋 **Message Filters ({len(filters)})**:"]
    for f in filters:
        lines.append(f"{f['id']}. `{f['pattern']}`")
    lines.append("\nTo add: `/filteradd <word or phrase>`")
    lines.append("To remove: `/filterdel <number or phrase>`")
    _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="\n".join(lines)))


@dc_cli.on(events.NewMessage(command="/filteradd"))
def dc_filteradd_command(bot, accid, event):
    """Add a message filter. Admin only."""
    import bot as _bot_module
    msg = event.msg
    payload = event.payload.strip()
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can manage filters."))
        return

    if not payload:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: `/filteradd <word or phrase>`\nExample: `/filteradd #реклама`"))
        return

    row_id = database.add_filter(payload)
    if row_id is not None:
        _reload_filter_cache()
        clean = payload.strip().strip('"\'')
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Filter added (#{row_id}): `{clean}`"))
    else:
        clean = payload.strip().strip('"\'')
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"⚠️ Filter `{clean}` already exists or is invalid."))


@dc_cli.on(events.NewMessage(command="/filterdel"))
@dc_cli.on(events.NewMessage(command="/filterremove"))
def dc_filterdel_command(bot, accid, event):
    """Remove a message filter. Admin only."""
    import bot as _bot_module
    msg = event.msg
    payload = event.payload.strip()
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can manage filters."))
        return

    if not payload:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: `/filterdel <number or phrase>`\nExample: `/filterdel 1` or `/filterdel #реклама`"))
        return

    success, deleted_pattern = database.remove_filter(payload)
    if success:
        _reload_filter_cache()
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Filter `{deleted_pattern}` removed."))
    else:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"❌ Filter `{payload}` not found."))


@dc_cli.on(events.NewMessage(command="/bridge"))
def bridge_command(bot, accid, event):
    """Bridge a Delta Chat group to a Telegram group. Admin only."""
    import bot as _bot_module
    msg = event.msg
    chat_id = msg.chat_id

    # Check if it's a group chat
    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    if chat_info.get("type") == 1:
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ You must send that command in a Delta Chat group, not here."))
        return

    # Admin check: if a global admin is set, only they can manage bridges
    if database.get_config("admin_dc_fingerprint") or database.get_config("admin_dc_email"):
        if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
            _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only the configured bot administrator can use /bridge."))
            return
    else:
        # Fallback to group creator check
        try:
            contacts = bot.rpc.get_chat_contacts(accid, chat_id)
            # In Delta Chat, the first contact in the list is the group creator/admin
            # Also check if the sender is the bot owner (contact ID 1 = self)
            if msg.from_id not in contacts[:1] and msg.from_id != 1:
                _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only group admins can use /bridge. (Or set a global admin via /initadmin)"))
                return
        except Exception as e:
            logger.warning(f"Could not verify DC chat contacts for /bridge: {e}")
            _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Could not verify group administrator status."))
            return

    try:
        payload = event.payload.strip()
        if not payload:
            raise ValueError()
        tg_chat_id = int(payload)
    except ValueError:
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ You must provide the Telegram chat ID. Example: /bridge -123456789"))
        return

    if database.add_bridge(chat_id, tg_chat_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=f"✔️ Bridged with Telegram group {tg_chat_id}."))
    else:
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ This chat is already bridged."))


@dc_cli.on(events.NewMessage(command="/unbridge"))
def unbridge_command(bot, accid, event):
    """Remove the bridge for this Delta Chat group. Admin only."""
    import bot as _bot_module
    msg = event.msg
    chat_id = msg.chat_id

    # Check if it's a group chat
    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    if chat_info.get("type") == 1:
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ You must send that command in a Delta Chat group, not here."))
        return

    # Admin check
    if database.get_config("admin_dc_fingerprint") or database.get_config("admin_dc_email"):
        if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
            _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only the configured bot administrator can use /unbridge."))
            return
    else:
        try:
            contacts = bot.rpc.get_chat_contacts(accid, chat_id)
            if msg.from_id not in contacts[:1] and msg.from_id != 1:
                _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only group admins can use /unbridge. (Or set a global admin via /initadmin)"))
                return
        except Exception as e:
            logger.warning(f"Could not verify DC chat contacts for /unbridge: {e}")
            _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Could not verify group administrator status."))
            return

    tg_chat_ids = database.remove_bridge(chat_id)
    if tg_chat_ids:
        _clear_dc_caches(chat_id)
        if _bot_module.main_loop:
            for tid in tg_chat_ids:
                asyncio.run_coroutine_threadsafe(_userbot_leave_chat(tid), _bot_module.main_loop)
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="✔️ Bridge removed."))
    else:
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ This chat is not bridged."))


@dc_cli.on(events.NewMessage(command="/cleanup"))
def dc_cleanup_command(bot, accid, event):
    """Trigger manual cleanup of stale/orphaned/duplicate bridges (Admin only)."""
    import bot as _bot_module
    msg = event.msg
    chat_id = msg.chat_id
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only the bot administrator can use /cleanup."))
        return

    if _bot_module.main_loop and _bot_module.main_loop.is_running():
        async def _do_cleanup():
            try:
                stats = await _bot_module.cleanup_stale_bridges(dc_bot=bot, accid=accid)
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
                _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=res_text))
            except Exception as e:
                logger.error(f"DC manual cleanup failed: {e}")
                _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=f"❌ Cleanup failed: {e}"))

        asyncio.run_coroutine_threadsafe(_do_cleanup(), _bot_module.main_loop)
    else:
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Main event loop is not ready."))


@dc_cli.on(events.NewMessage(command="/locupdate"))
def locupdate_command(bot, accid, event):
    """Fetch the latest coordinates for a live location message."""
    import bot as _bot_module
    msg = event.msg
    chat_id = msg.chat_id

    # Check if this is a reply to another message
    if not hasattr(msg, 'quote') or not msg.quote:
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Please reply to a Live Location message with /locupdate."))
        return

    quote_msg_id = msg.quote.get('message_id') if isinstance(msg.quote, dict) else None
    if not quote_msg_id:
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Please reply to a Live Location message with /locupdate."))
        return

    # Check group chat mappings
    tg_chats = database.get_tg_chats(chat_id)
    found = False
    
    if tg_chats:
        for tg_chat_id in tg_chats:
            tg_msg_id = database.get_tg_msg_id(quote_msg_id, chat_id, tg_chat_id)
            if tg_msg_id and tg_msg_id in LIVE_LOCATIONS:
                lat, lon = LIVE_LOCATIONS[tg_msg_id]
                _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=f"📍 Updated Location: https://maps.google.com/?q={lat},{lon}", quoted_message_id=quote_msg_id))
                found = True
                break
                
    if not found:
        # Check channel mapping if needed (very rare case for locupdate but just in case)
        # We don't track channel reverse mapping in memory easily here, so we just return not found.
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ No active live location found for this message. It may have expired or not be a live location.", quoted_message_id=msg.id))


@dc_cli.on(events.NewMessage(command="/userbotsync"))
def dc_userbotsync_command(bot, accid, event):
    """Force Userbot sync from Delta Chat. Admin only."""
    import bot as _bot_module
    msg = event.msg
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can trigger synchronization."))
        return

    async def run_sync():
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="⏳ Starting Userbot synchronization..."))
        await sync_userbot_channels(force=True)
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="✅ Userbot synchronization completed (subscriber counts updated)."))
    
    if _bot_module.main_loop:
        asyncio.run_coroutine_threadsafe(run_sync(), _bot_module.main_loop)
    else:
        logger.error("Main loop not found, cannot run userbotsync")


@dc_cli.on(events.NewMessage(command="/userbotjoin"))
def dc_userbotjoin_command(bot, accid, event):
    """Join a channel/group via Userbot using an invite link. Admin only."""
    import bot as _bot_module
    msg = event.msg
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use this command."))
        return

    text = msg.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(
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

        if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Userbot is not connected."))
            return

        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"⏳ Attempting to join via Userbot: {link}..."))

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
                    _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Telethon not properly installed."))
                    return

                try:
                    check = await asyncio.wait_for(_bot_module.userbot_client(CheckChatInviteRequest(invite_hash)), timeout=15.0)
                    if hasattr(check, 'chat'):
                        joined_entity = check.chat
                        joined_title = getattr(joined_entity, 'title', 'Unknown')
                    else:
                        result = await asyncio.wait_for(_bot_module.userbot_client(ImportChatInviteRequest(invite_hash)), timeout=15.0)
                        if hasattr(result, 'chats') and result.chats:
                            joined_entity = result.chats[0]
                            joined_title = getattr(joined_entity, 'title', 'Unknown')
                except Exception as e:
                    if "already" in str(e).lower() or "USER_ALREADY_PARTICIPANT" in str(e):
                        try:
                            joined_entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(link), timeout=15.0)
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

                entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(target), timeout=15.0)
                is_bot = getattr(entity, 'bot', False)
                if is_bot:
                    try:
                        await asyncio.wait_for(_bot_module.userbot_client.send_message(entity, "/start"), timeout=10.0)
                    except Exception:
                        pass
                    first_name = getattr(entity, 'first_name', '') or ''
                    last_name = getattr(entity, 'last_name', '') or ''
                    full_name = f"{first_name} {last_name}".strip()
                    joined_title = full_name or getattr(entity, 'title', None) or (f"@{entity.username}" if getattr(entity, 'username', None) else target)
                else:
                    if getattr(entity, 'left', True):
                        if JoinChannelRequest:
                            await asyncio.wait_for(_bot_module.userbot_client(JoinChannelRequest(entity)), timeout=15.0)
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
                    _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(
                        text=f"✅ Userbot joined *{joined_title}* (ID: {tg_channel_id}).\n"
                             f"Invite link saved for future syncs.\n\n"
                             f"Matched to bridged channel #{matched_channel['id']}."
                    ))
                else:
                    _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(
                        text=f"✅ Userbot joined *{joined_title}* (ID: {tg_channel_id}).\n\n"
                             f"⚠️ This channel is not yet bridged."
                    ))

                try:
                    chan_id = matched_channel['id'] if matched_channel else None
                    if chan_id:
                        await _bot_module.update_tg_channel_stats(chan_id, joined_entity)
                except Exception:
                    pass
            else:
                _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="⚠️ Joined but could not determine channel details."))

        except Exception as e:
            logger.error(f"DC userbotjoin failed for {link}: {e}")
            _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to join: {str(e)[:500]}"))

    if _bot_module.main_loop:
        asyncio.run_coroutine_threadsafe(do_join(), _bot_module.main_loop)
    else:
        logger.error("Main loop not found, cannot run userbotjoin")


@dc_cli.on(events.NewMessage(command="/status"))
def dc_status_command(bot, accid, event):
    """Show detailed bot and userbot status (admin only)."""
    import bot as _bot_module
    msg = event.msg
    chat_id = msg.chat_id

    # Check if sender is admin
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only the bot administrator can use /status."))
        return

    async def do_status():
        try:
            report = await generate_status_report(is_html=False, bot_ref=bot, accid=accid)
            _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=report))
        except Exception as e:
            logger.error(f"Error generating status report for DC: {e}")
            _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=f"❌ Error generating status report: {e}"))

    if _bot_module.main_loop:
        asyncio.run_coroutine_threadsafe(do_status(), _bot_module.main_loop)
    else:
        logger.error("Main loop not found, cannot run /status")


@dc_cli.on(events.NewMessage(command="/catchup"))
@dc_cli.on(events.NewMessage(command="/reconcile"))
def dc_catchup_command(bot, accid, event):
    """Catch up missed channel posts (admin only)."""
    import bot as _bot_module
    msg = event.msg
    chat_id = msg.chat_id

    # Check if sender is admin
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only the bot administrator can use /catchup."))
        return

    text = msg.text or ""
    parts = text.strip().split()
    target = parts[1] if len(parts) > 1 else None

    async def do_catchup():
        try:
            res_text = await run_channel_catchup(target, is_html=False)
            _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=res_text))
        except Exception as e:
            logger.error(f"Error during DC /catchup: {e}")
            _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=f"❌ Catchup error: {e}"))

    if _bot_module.main_loop:
        asyncio.run_coroutine_threadsafe(do_catchup(), _bot_module.main_loop)
    else:
        logger.error("Main loop not found, cannot run /catchup")


@dc_cli.on(events.NewMessage(command="/stats"))
def stats_command(bot, accid, event):
    """Show bridge statistics."""
    import bot as _bot_module
    msg = event.msg
    chat_id = msg.chat_id

    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    is_private = chat_info.get("type") == 1

    if is_private:
        # In private chat: show all bridges (admin only)
        if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
            _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only the bot admin can view all stats."))
            return

        bridges = database.get_all_bridges()
        if not bridges:
            _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="📊 No bridges configured."))
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
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="\n".join(lines)))
    else:
        # In group chat: show stats for this bridge only
        tg_chats = database.get_tg_chats(chat_id)
        if not tg_chats:
            _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="📊 This group is not bridged."))
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
                
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="\n".join(lines)))


@dc_cli.on(events.NewMessage(command="/channels"))
def channels_command_dc(bot, accid, event):
    """List public bridged channels to Delta Chat users, or trigger sync if requested."""
    import bot as _bot_module
    msg = event.msg
    chat_id = msg.chat_id

    # Check if requester is admin
    is_admin = _bot_module._is_dc_admin(bot, accid, msg.from_id)

    # Check for sync sub-commands (e.g. "/channels sync" or "/channels sync 14")
    payload = event.payload.strip().lower()
    if payload.startswith("sync") or payload.startswith("update") or payload.startswith("refresh"):
        if not is_admin:
            _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only administrators can sync channels."))
            return
        
        parts = payload.split()
        target_channel_id = None
        if len(parts) > 1 and parts[1].isdigit():
            target_channel_id = int(parts[1])

        if _bot_module.main_loop:
            asyncio.run_coroutine_threadsafe(
                async_update_channels_dc(bot, accid, chat_id, target_channel_id),
                _bot_module.main_loop
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
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="📺 No public channels are currently available."))
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
    _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="\n".join(lines)))


@dc_cli.on(events.NewMessage(command="/channelssync"))
@dc_cli.on(events.NewMessage(command="/channelsync"))
def channelssync_command_dc(bot, accid, event):
    """Sync/update bridged Telegram channel names and avatars in Delta Chat."""
    import bot as _bot_module
    msg = event.msg
    chat_id = msg.chat_id

    # Check if requester is admin
    is_admin = _bot_module._is_dc_admin(bot, accid, msg.from_id)
    if not is_admin:
        _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text="❌ Only administrators can sync channels."))
        return

    payload = event.payload.strip()
    target_channel_id = None
    if payload.isdigit():
        target_channel_id = int(payload)

    if _bot_module.main_loop:
        asyncio.run_coroutine_threadsafe(
            async_update_channels_dc(bot, accid, chat_id, target_channel_id),
            _bot_module.main_loop
        )


def _notify_and_remove_channel_bridge(ch: dict) -> int | None:
    """Send unbridge notice to DC channel, remove from DB, clear caches, and leave TG chat."""
    import bot as _bot_module
    ch_id = ch.get('id')
    dc_chat_id = ch.get('dc_chat_id')
    tg_channel_id = ch.get('tg_channel_id')

    # 1. Notify Delta Chat Broadcast Channel before unbridging
    if dc_chat_id and _bot_module.dc_bot_instance and _bot_module.dc_accid:
        try:
            dc_notice = (
                "⚠️ **Channel Disconnected**\n"
                "This broadcast channel has been unbridged from Telegram by the administrator and will no longer receive updates."
            )
            _bot_module.dc_bot_instance.rpc.send_msg(_bot_module.dc_accid, dc_chat_id, MsgData(text=dc_notice))
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
        if _bot_module.main_loop and _bot_module.main_loop.is_running():
            asyncio.run_coroutine_threadsafe(_userbot_leave_chat(removed_tg_id), _bot_module.main_loop)
    return removed_tg_id


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
    import bot as _bot_module
    # 1. Telegram Bot API
    tg_bot_status = "🔴 Off"
    if _bot_module.tg_app:
        try:
            bot_me = await _bot_module.tg_app.bot.get_me()
            tg_bot_status = f"🟢 Connected (as @{bot_me.username})"
        except Exception as e:
            tg_bot_status = f"🟡 Connected (error fetching info: {e})"
    
    # 2. Userbot Status
    ub_status = "🔴 Disconnected"
    if _bot_module.userbot_client:
        if _bot_module.userbot_client.is_connected():
            try:
                ub_me = await asyncio.wait_for(_bot_module.userbot_client.get_me(), timeout=3.0)
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
    elif _bot_module.dc_bot_instance and _bot_module.dc_accid:
        try:
            dc_addr = _bot_module.dc_bot_instance.rpc.get_config(_bot_module.dc_accid, "configured_addr") or _bot_module.dc_bot_instance.rpc.get_config(_bot_module.dc_accid, "addr") or "Unknown"
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

