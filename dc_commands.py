"""All Delta Chat /command handlers (deprecated setprimary/resilient, richmode,
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
import security
import group_watch
import userbot
from caching import _clear_dc_caches, _invalidate_dc_channel_cache, invalidate_channels_cache, _get_cached_last_msg_id
from live_locations import LIVE_LOCATIONS
from dc_helpers import async_update_channels_dc, _userbot_leave_chat
from tg_events import _add_channel_bridge, _send_bot_message
from userbot import sync_userbot_channels, run_channel_catchup, _channel_workers, _channel_queues

logger = logging.getLogger("tg_dc_bridge")

VERSION = "2.29.3"
dc_cli = BotCli("tgbridge")


RELAY_SELECTION_NOTE = (
    "Since core 2.61 the core picks the sending relay itself: it tries the newest relay first "
    "and falls back to the next one if a relay is unreachable. "
    "Use /addtransport and /rmtransport to control relays."
)


@dc_cli.on(events.NewMessage(command="/setprimary"))
def setprimary_command(bot, accid, event):
    """Deprecated: the core no longer uses configured_addr to pick the SMTP relay."""
    import bot as _bot_module
    msg = event.msg
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /setprimary."))
        return
    _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"⚠️ /setprimary is deprecated and disabled. {RELAY_SELECTION_NOTE}"))


@dc_cli.on(events.NewMessage(command="/resilient"))
def resilient_command(bot, accid, event):
    """Deprecated: relay failover is handled by the core."""
    import bot as _bot_module
    msg = event.msg
    if not _bot_module._is_dc_admin(bot, accid, msg.from_id):
        _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /resilient."))
        return
    _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"⚠️ /resilient is deprecated and disabled. {RELAY_SELECTION_NOTE}"))


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


HELP_PRIVATE_NOTE = "\n\n💬 Sent privately because you asked in a group. Use /help@tg there to show it to everyone."


def _get_help_chat_id(bot, accid, msg):
    """Plain /help in a group is answered privately to the sender so several bots
    don't flood the group; /help@<bot> is still answered in the group itself."""
    import bot as _bot_module
    cmd = msg.text.split(maxsplit=1)[0] if msg.text else ""
    if "@" in cmd or _bot_module._is_private_chat(bot, accid, msg.chat_id):
        return msg.chat_id
    return bot.rpc.create_chat_by_contact_id(accid, msg.from_id)


@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    """Reply with help text."""
    import bot as _bot_module
    msg = event.msg

    # Get sender info
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address

    help_msg = get_dc_help_text(bot, accid, sender_email, msg.from_id)
    chat_id = _get_help_chat_id(bot, accid, msg)
    if chat_id != msg.chat_id:
        help_msg += HELP_PRIVATE_NOTE
    _bot_module._dc_send_msg_with_stats(bot, accid, chat_id, MsgData(text=help_msg))


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


def _read_core_transport_state(bot, accid) -> Optional[dict]:
    """Relay details that the JSON-RPC API does not expose, read from the core database.

    Since core 2.61 the relay used for sending is not configured_addr: the core
    keeps sending through the relay that last worked (smtp_success) and switches
    only when it cannot connect. The account database is opened read-only; the
    core keeps it in WAL mode, so this does not block it.
    """
    import sqlite3
    try:
        db_path = bot.rpc.get_info(accid).get("database_dir")
        if not db_path:
            return None
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = conn.execute(
                """SELECT t.id, t.addr, t.add_timestamp, t.is_published, t.last_rcvd_timestamp, s.id
                   FROM transports t LEFT JOIN smtp_success s ON s.transport_id = t.id
                   ORDER BY IFNULL(s.id, 0) DESC, t.id ASC"""
            ).fetchall()
            outbox = conn.execute("SELECT COUNT(*), IFNULL(MAX(retries), 0) FROM smtp2").fetchone()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Could not read transport state from the core database: {e}")
        return None
    transports = [
        {"id": r[0], "addr": r[1], "added": r[2] or 0, "published": bool(r[3]),
         "last_rcvd": r[4] or 0, "sending": r[5] is not None}
        for r in rows
    ]
    # Only the first one in this order can be the current sending relay.
    for t in transports[1:]:
        t["sending"] = False
    return {"transports": transports, "outbox": outbox[0], "outbox_max_retries": outbox[1]}


def _parse_connectivity_html(html_text: str) -> dict:
    """Pull per-relay incoming status/quota and the outgoing status out of get_connectivity_html().

    Relays are returned in the core's order (transports by id).
    """
    def plain(fragment: str) -> str:
        return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split()

    result = {"relays": [], "outgoing": None}
    if not html_text:
        return result
    parts = html_text.split('<li class="transport">')
    tail = parts[-1]
    out_idx = tail.rfind("<h3>")
    outgoing_html = tail[out_idx:] if out_idx != -1 else ""
    if out_idx != -1:
        parts[-1] = tail[:out_idx]
    for seg in parts[1:]:
        m = re.search(r'class="(\w+) dot"></span>\s*<b>([^<]+):</b>\s*([^<]*)', seg)
        relay = {"domain": None, "color": None, "status": None, "quota": None, "quota_pct": None}
        if m:
            relay["color"], relay["domain"], relay["status"] = m.group(1), html.unescape(m.group(2)).strip(), html.unescape(m.group(3)).strip()
        q = re.search(r'<ul class="quota-list"><li>(.*?)<div class="bar"><div class="progress \w+"[^>]*>(\d+)%', seg, re.S)
        if q:
            relay["quota"] = " ".join(plain(q.group(1)))
            relay["quota_pct"] = int(q.group(2))
        result["relays"].append(relay)
    m = re.search(r'class="(\w+) dot"></span>\s*([^<]*)', outgoing_html)
    if m:
        result["outgoing"] = {"color": m.group(1), "status": html.unescape(m.group(2)).strip()}
    return result


_DOT = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def _ago(ts: int) -> str:
    if not ts:
        return "never"
    delta = max(0, int(time.time()) - int(ts))
    if delta < 120:
        return "just now"
    if delta < 7200:
        return f"{delta // 60} min ago"
    if delta < 172800:
        return f"{delta // 3600} h ago"
    return f"{delta // 86400} days ago"


@dc_cli.on(events.NewMessage(command="/transports"))
def transports_command(bot, accid, event):
    """Show configured transports (mail relays): which one sends, their incoming status and storage."""
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

    rpc_addrs = [t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '') for t in transports]
    state = _read_core_transport_state(bot, accid)
    try:
        conn_info = _parse_connectivity_html(bot.rpc.get_connectivity_html(accid))
    except Exception:
        conn_info = {"relays": [], "outgoing": None}

    if state:
        ordered = state["transports"]
        # The connectivity page lists relays by transport id.
        by_id_order = sorted(ordered, key=lambda t: t["id"])
        relay_info = {t["addr"]: (conn_info["relays"][i] if i < len(conn_info["relays"]) else None)
                      for i, t in enumerate(by_id_order)}
    else:
        ordered = [{"addr": a, "sending": False, "published": None, "last_rcvd": 0, "added": 0} for a in rpc_addrs]
        relay_info = {}
        for t in ordered:
            domain = t["addr"].split("@")[-1]
            relay_info[t["addr"]] = next((r for r in conn_info["relays"] if r.get("domain") == domain), None)

    lines = ["🔌 **Mail Relays (Transports)**", ""]
    sending = next((t for t in ordered if t["sending"]), None)
    if sending:
        lines.append(f"📤 **Sending via:** `{sending['addr']}`")
    elif state:
        lines.append(f"📤 **Sending via:** `{ordered[0]['addr']}` (nothing sent successfully yet)")
    out = conn_info.get("outgoing")
    if out:
        lines.append(f"   {_DOT.get(out['color'], '❓')} {out['status']}")
    if state:
        waiting = state["outbox"]
        if waiting:
            retry_note = f", retried up to {state['outbox_max_retries']}× (fails after 6)" if state["outbox_max_retries"] else ""
            lines.append(f"   📬 {waiting} message(s) waiting in the core's send queue{retry_note}")
        else:
            lines.append("   📬 Send queue is empty")
    lines.append("")

    for idx, t in enumerate(ordered, 1):
        addr = t["addr"]
        tags = []
        if t["sending"]:
            tags.append("sending")
        if t.get("published") is False:
            tags.append("not published")
        lines.append(f"{idx}. `{addr}`" + (f" — {', '.join(tags)}" if tags else ""))
        info = relay_info.get(addr)
        if info and info.get("status"):
            lines.append(f"   📥 {_DOT.get(info['color'], '❓')} {info['status']}"
                         + (f", last message {_ago(t['last_rcvd'])}" if state else ""))
        elif state:
            lines.append(f"   📥 Last message {_ago(t['last_rcvd'])}")
        if info and info.get("quota"):
            warn = " ⚠️" if (info.get("quota_pct") or 0) >= 80 else ""
            lines.append(f"   💾 {info['quota']} ({info['quota_pct']}%){warn}")
        if t.get("added"):
            lines.append(f"   ➕ Added {time.strftime('%Y-%m-%d', time.gmtime(t['added']))}")
        lines.append("")

    lines.append("ℹ️ The core keeps sending through the relay that last worked and switches to the next one "
                 "only if it cannot connect. Relays are tried in the order above. "
                 "\"Not published\" relays are not announced to contacts.")
    if not state:
        lines.append("(Sending relay unknown: could not read the core database.)")
    _bot_module._dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="\n".join(lines)))


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


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def _dc_chat_label(dc, accid, dc_chat_id: int) -> str:
    ch = database.get_channel_by_dc_chat_id(dc_chat_id)
    if ch and ch.get('tg_channel_username'):
        return f"@{ch['tg_channel_username']}"
    try:
        name = dc.rpc.get_basic_chat_info(accid, dc_chat_id).name
        if name:
            return name
    except Exception:
        pass
    return f"DC chat {dc_chat_id}"


async def _build_send_load_section(is_html: bool, dc, accid) -> str:
    """Send queue state and load history, to see when the bot runs out of capacity.

    One Delta Chat account can send at most GLOBAL_DC_RATE_LIMIT messages per
    window (the relay limits each sender address); the core uses one relay at a
    time, so the only way to grow beyond it is to spread chats over more bots.
    """
    b = (lambda t: f"<b>{t}</b>") if is_html else (lambda t: f"**{t}**")
    esc = html.escape if is_html else (lambda t: t)
    per_hour = security.GLOBAL_DC_RATE_LIMIT * 3600 // security.GLOBAL_DC_RATE_WINDOW
    now = time.time()
    lines = [f"📮 {b('Send load')} (limit {security.GLOBAL_DC_RATE_LIMIT}/min = {per_hour}/h)"]

    # Right now
    waiting = security.get_dc_send_waiting()
    pending_events = userbot.get_pending_event_count()
    backlog = security.get_dc_backlog()
    try:
        outbox = await asyncio.to_thread(security.get_core_outbox_pending) if dc and accid else 0
    except Exception:
        outbox = 0
    if backlog:
        eta = _fmt_duration(backlog * security.GLOBAL_DC_RATE_WINDOW / security.GLOBAL_DC_RATE_LIMIT)
        lines.append(f"• Now: {backlog} waiting ({waiting[security.PRIORITY_GROUP]} group msgs, "
                     f"{max(pending_events, waiting[security.PRIORITY_CHANNEL])} channel events), ~{eta} to send")
    else:
        lines.append("• Now: nothing waiting")
    held = " — channel posts held back" if security.is_core_outbox_full() else ""
    lines.append(f"• Core outbox: {outbox} not yet sent{held}")
    busy_queues = sorted(((q.qsize(), cid) for cid, q in userbot._channel_queues.items() if q.qsize()), reverse=True)[:3]
    if busy_queues:
        parts = []
        for size, cid in busy_queues:
            ch = database.get_channel_by_tg_id(cid)
            name = f"@{ch['tg_channel_username']}" if ch and ch.get('tg_channel_username') else str(cid)
            parts.append(f"{esc(name)} {size}")
        lines.append("• Largest channel queues: " + ", ".join(parts))

    # History from the hourly table (survives restarts, kept 30 days)
    rows = database.get_send_stats(now - 7 * 86400)
    # Counters not flushed yet belong to the current hour.
    unflushed = security._send_stats
    cur_hour = int(now // 3600 * 3600)
    by_hour = {r['hour_ts']: dict(r) for r in rows}
    cur = by_hour.setdefault(cur_hour, {'hour_ts': cur_hour, 'group_sent': 0, 'channel_sent': 0, 'rate_wait_sec': 0.0, 'outbox_wait_sec': 0.0, 'max_backlog': 0})
    cur['group_sent'] += unflushed['group']
    cur['channel_sent'] += unflushed['channel']
    cur['rate_wait_sec'] += unflushed['rate_wait']
    cur['outbox_wait_sec'] += unflushed['outbox_wait']
    cur['max_backlog'] = max(cur['max_backlog'], backlog)
    hours = sorted(by_hour.values(), key=lambda r: r['hour_ts'])

    def sent(r):
        return r['group_sent'] + r['channel_sent']

    def pct(n):
        return round(100 * n / per_hour)

    day = [r for r in hours if r['hour_ts'] >= now - 86400]
    day_sent = sum(sent(r) for r in day)
    day_groups = sum(r['group_sent'] for r in day)
    day_wait = sum(r['rate_wait_sec'] for r in day)
    day_held = sum(r['outbox_wait_sec'] for r in day)
    peak_day = max(day, key=sent)
    peak_week = max(hours, key=sent)
    week_backlog = max(r['max_backlog'] for r in hours)
    week_wait = sum(r['rate_wait_sec'] for r in hours)
    oldest = hours[0]['hour_ts']

    lines.append(f"• This hour: {sent(cur)} sent ({pct(sent(cur) * 3600 / max(60, now - cur_hour))}% of limit at this pace)")
    lines.append(f"• Last 24h: {day_sent} sent ({day_groups} group, {day_sent - day_groups} channel), "
                 f"average {pct(day_sent / 24)}%, busiest hour {sent(peak_day)} ({pct(sent(peak_day))}%) "
                 f"at {time.strftime('%H:00', time.gmtime(peak_day['hour_ts']))} UTC")
    lines.append(f"• Last 24h at the limit: {_fmt_duration(day_wait)}; channel posts held for the core: {_fmt_duration(day_held)}")
    # A catchup or restart makes one very busy hour; the 90th percentile shows
    # how loaded the bot is in normal busy hours, which is what matters for sizing.
    week_sends = sorted(sent(r) for r in hours)
    p90 = week_sends[int(0.9 * (len(week_sends) - 1))]
    days_covered = max(1.0, (now - oldest) / 86400)
    lines.append(f"• Last 7 days: busy hours (top 10%) {p90}+ ({pct(p90)}%), busiest {sent(peak_week)} ({pct(sent(peak_week))}%), "
                 f"at the limit {_fmt_duration(week_wait / days_covered)}/day, largest backlog {week_backlog}")
    if now - oldest < 6 * 86400:
        lines.append(f"  (statistics collected since {time.strftime('%Y-%m-%d %H:00', time.gmtime(oldest))} UTC)")

    # Who produces the traffic
    try:
        top = database.get_top_dc_chats_by_volume(now - 86400, limit=5)
    except Exception:
        top = []
    if top:
        labels = await asyncio.to_thread(lambda: [(_dc_chat_label(dc, accid, cid), n) for cid, n in top])
        # Shares of all relayed messages (message_map also has group messages
        # relayed from Delta Chat to Telegram, so it differs from the send counters).
        total = max(1, database.get_relayed_message_count(now - 86400))
        lines.append("• Busiest chats 24h: " + ", ".join(f"{esc(name)} {n} ({round(100 * n / total)}%)" for name, n in labels))

    # Verdict: load in normal busy hours and daily time spent at the limit
    wait_per_day = week_wait / days_covered
    if pct(p90) >= 85 or wait_per_day > 2 * 3600:
        verdict = ("🔴 At capacity: posts are regularly delayed by the send limit. Move some channels to a second "
                   "bot account (each address has its own relay limit; a second transport on the same account "
                   "does not add capacity, the core sends through one relay at a time).")
    elif pct(p90) >= 60 or wait_per_day > 15 * 60:
        verdict = (f"🟡 Getting busy: busy hours use {pct(p90)}% of the send limit, posts wait at the limit "
                   f"{_fmt_duration(wait_per_day)}/day. Watch this before adding big channels.")
    else:
        verdict = f"🟢 Headroom: busy hours use {pct(p90)}% of the send limit."
    lines.append(verdict)
    return "\n".join(lines)


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

    # 4. Delta Chat send load (capacity planning)
    dc = bot_ref or _bot_module.dc_bot_instance
    dc_accid = accid or _bot_module.dc_accid
    send_load = await _build_send_load_section(is_html, dc, dc_accid)

    # 5. What is bridged (counts only; the per-channel list was not useful)
    channels = database.get_all_channels()
    try:
        groups_count = len(database.get_all_bridges())
    except Exception:
        groups_count = 0

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
            f"📡 <b>Bridged:</b> {len(channels)} channels, {groups_count} groups\n"
            f"💬 <b>Group messages:</b> {html.escape(group_watch.status_line())}\n\n"
            f"{send_load}\n\n"
            f"📝 <b>Recent Activity:</b>\n" + ("\n".join(activity_lines) if activity_lines else "None")
        )
    else:
        report = (
            "🤖 **Telegram Bridge Bot Status**\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🔌 **Bot API:** {tg_bot_status}\n"
            f"👤 **Userbot:** {ub_status}\n"
            f"📧 **Delta Chat:** {dc_status}\n\n"
            f"📡 **Bridged:** {len(channels)} channels, {groups_count} groups\n"
            f"💬 **Group messages:** {group_watch.status_line()}\n\n"
            f"{send_load}\n\n"
            f"📝 **Recent Activity:**\n" + ("\n".join(activity_lines) if activity_lines else "None")
        )
    return report

