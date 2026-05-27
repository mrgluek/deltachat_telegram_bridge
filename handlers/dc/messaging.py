import asyncio
import html
import logging
import os
import re
import tempfile

from deltachat2 import MsgData, events

import database
from app_context import app_ctx
from constants import TG_MAX_MSG_LEN, DC_FALLBACK_PATTERN
from channel_history import channel_history_service
from permissions import permission_checker
from rate_limiter import rate_limiter
from relay import message_relay
from message_queue import ordered_queue
from shared import get_tg_help_text

from handlers.dc.commands import get_dc_help_text

logger = logging.getLogger("tg_dc_bridge")


def _increment_transport(bot, accid, addr_key="configured_addr"):
    try:
        addr = bot.rpc.get_config(accid, addr_key) or bot.rpc.get_config(accid, "addr")
        if addr:
            database.increment_transport_received(addr)
    except Exception:
        pass


def _greet_new_user(bot, accid, event):
    msg = event.msg
    from_id = msg.from_id
    dc_chat_id = msg.chat_id
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, dc_chat_id)
        if chat_info.get("type") != 1:
            return
        greeted_key = f"greeted_{from_id}"
        if database.get_config(greeted_key):
            return
        sender_email = bot.rpc.get_contact(accid, from_id).address
        help_text = get_dc_help_text(bot, accid, sender_email, from_id)
        message_relay.dc_send_msg(bot, accid, dc_chat_id, MsgData(text=help_text))
        database.set_config(greeted_key, "1")
    except Exception as e:
        logger.warning(f"Greeting check failed: {e}")


def _resolve_channel_id(text: str):
    if not text:
        return None, False
    cmd = text.split()[0] if text else ""
    if not cmd.startswith("/channel") or not any(c.isdigit() for c in cmd):
        return None, False
    is_qr = cmd.endswith("qr")
    id_str = cmd[8:-2] if is_qr else cmd[8:]
    if id_str.isdigit():
        return int(id_str), is_qr
    match = re.search(r'(\d+)', cmd[8:])
    if match:
        return int(match.group(1)), is_qr
    return None, False


def _handle_channel_command(bot, accid, event):
    msg = event.msg
    dc_chat_id = msg.chat_id
    text = msg.text or ""
    channel_id, is_qr = _resolve_channel_id(text)
    if channel_id is None:
        return False

    ch = database.get_channel_by_id(channel_id)
    if not ch:
        message_relay.dc_send_msg(bot, accid, dc_chat_id,
                                   MsgData(text=f"❌ Channel #{channel_id} not found."))
        return True

    is_public = bool(ch.get('tg_channel_username'))
    if not is_public:
        if not permission_checker.is_dc_admin(bot, accid, msg.from_id):
            message_relay.dc_send_msg(bot, accid, dc_chat_id,
                                       MsgData(text=f"❌ Channel #{channel_id} not found."))
            return True

    invite_link = ch.get('invite_link', '')
    if not invite_link:
        message_relay.dc_send_msg(bot, accid, dc_chat_id,
                                   MsgData(text="❌ No invite link available for this channel."))
        return True

    if is_qr:
        _send_channel_qr(bot, accid, dc_chat_id, ch, channel_id)
    else:
        _send_channel_invite(bot, accid, dc_chat_id, ch, channel_id)
    return True


def _get_channel_title(bot, accid, ch):
    title = ch.get('title')
    if title:
        return title
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, ch['dc_chat_id'])
        return chat_info.get("name", "channel")
    except Exception:
        return "channel"


def _send_channel_qr(bot, accid, dc_chat_id, ch, channel_id):
    import qrcode
    invite_link = ch['invite_link']
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(invite_link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
        img.save(tmp_path)

    title = _get_channel_title(bot, accid, ch)
    tg_username = ch.get('tg_channel_username')
    link_part = f" (t.me/{tg_username})" if tg_username else ""

    message_relay.dc_send_msg(bot, accid, dc_chat_id, MsgData(
        text=f"📷 QR Code for **{title}**{link_part}",
        file=tmp_path
    ))
    try:
        os.unlink(tmp_path)
    except Exception:
        pass


def _send_channel_invite(bot, accid, dc_chat_id, ch, channel_id):
    invite_link = ch['invite_link']
    title = _get_channel_title(bot, accid, ch)
    tg_username = ch.get('tg_channel_username')
    link_part = f" (t.me/{tg_username})" if tg_username else ""
    message_relay.dc_send_msg(bot, accid, dc_chat_id,
                               MsgData(text=f"🔗 Join channel **{title}**{link_part}:\n\n{invite_link}"))

    _schedule_history_preview(bot, accid, dc_chat_id, ch)


def _schedule_history_preview(bot, accid, dc_chat_id, ch):
    if not (app_ctx.main_loop and app_ctx.userbot_client):
        return
    tg_channel_id = ch.get('tg_channel_id')
    ub_target = ch.get('tg_channel_username') or tg_channel_id
    ch_invite = ch.get('invite_link')
    if tg_channel_id and ub_target:
        logger.info(f"Sending channel history preview to private chat {dc_chat_id} for channel {tg_channel_id}")
        asyncio.run_coroutine_threadsafe(
            channel_history_service.relay_history_to_chat(
                dc_chat_id, tg_channel_id, ub_target, limit=3,
                invite_link=ch_invite, bot_ref=bot, accid=accid
            ),
            app_ctx.main_loop
        )


def _is_single_chat(bot, accid, dc_chat_id):
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, dc_chat_id)
        return chat_info.get("type") == 1
    except Exception:
        return True


def _send_bridge_message(bot, accid, dc_chat_id, msg, tg_chats):
    try:
        sender_contact = bot.rpc.get_contact(accid, msg.from_id)
        sender_name = msg.override_sender_name or sender_contact.display_name or sender_contact.address
    except Exception:
        sender_name = "Unknown"

    text = msg.text or ""
    text = DC_FALLBACK_PATTERN.sub('', text).strip()
    file_path = getattr(msg, 'file', None) or None
    viewtype = getattr(msg, 'viewtype', None) or ''
    is_media = viewtype in ('Image', 'Gif', 'Sticker', 'Video', 'Voice', 'Audio', 'Document', 'File')

    if not text and not file_path and not is_media:
        return

    safe_name = html.escape(sender_name)
    safe_text = html.escape(text) if text else ""

    reply_quote = None
    if hasattr(msg, 'quote') and msg.quote:
        quote_text = msg.quote.get('text', '') if isinstance(msg.quote, dict) else ''
        quote_text = DC_FALLBACK_PATTERN.sub('', quote_text).strip()
        if quote_text:
            short_quote = quote_text[:49] + "…" if len(quote_text) > 50 else quote_text
            reply_quote = f"<i>↩ {html.escape(short_quote)}</i>\n"

    is_image = viewtype in ('Image', 'Gif', 'Sticker') or (
        file_path and file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp'))
    )
    is_video = viewtype == 'Video' or (
        file_path and file_path.lower().endswith(('.mp4', '.mov', '.avi', '.mkv'))
    )
    is_voice = viewtype in ('Voice', 'Audio')

    for tg_chat_id in tg_chats:
        try:
            tg_reply_id = None
            if hasattr(msg, 'quote') and msg.quote:
                quote_msg_id = msg.quote.get('message_id') if isinstance(msg.quote, dict) else None
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
            if len(formatted_msg) > TG_MAX_MSG_LEN:
                formatted_msg = formatted_msg[:TG_MAX_MSG_LEN - 1] + "…"

            if app_ctx.main_loop:
                asyncio.run_coroutine_threadsafe(
                    ordered_queue.enqueue(f"tg:{tg_chat_id}",
                        message_relay.relay_to_tg(tg_chat_id, dc_chat_id, msg.id, file_path, formatted_msg,
                                                  tg_reply_id, is_image, is_video, is_voice, viewtype)),
                    app_ctx.main_loop
                )
            bot.logger.info(f"Relayed DC msg {msg.id} to TG chat {tg_chat_id}")
        except Exception as e:
            bot.logger.error(f"Failed to relay msg to TG chat {tg_chat_id}: {e}")


def _send_channel_subscriber_message(bot, accid, dc_chat_id, msg, dc_ch_info):
    text = msg.text or ""
    text = DC_FALLBACK_PATTERN.sub('', text).strip()
    file_path = getattr(msg, 'file', None) or None
    viewtype = getattr(msg, 'viewtype', None) or ''
    is_media = viewtype in ('Image', 'Gif', 'Sticker', 'Video', 'Voice', 'Audio', 'Document', 'File')

    tg_channel_id = dc_ch_info['tg_channel_id']
    if rate_limiter.check_per_chat(tg_channel_id):
        return
    channel_text = html.escape(text) if text else ""
    if not channel_text and not file_path and not is_media:
        return
    message_relay.register_pending_relay(dc_chat_id, msg.id)
    asyncio.run_coroutine_threadsafe(
        ordered_queue.enqueue(f"tg_channel:{tg_channel_id}",
            message_relay.relay_dc_to_tg_channel(tg_channel_id, dc_chat_id, msg.id, channel_text, file_path, viewtype)),
        app_ctx.main_loop
    )
    bot.logger.info(f"Relayed DC msg {msg.id} to DC→TG channel {tg_channel_id}")


@app_ctx.dc_cli.on(events.NewMessage(is_info=False))
def handle_dc_message(bot, accid, event):
    msg = event.msg
    dc_chat_id = msg.chat_id
    from_id = msg.from_id

    _increment_transport(bot, accid)
    _greet_new_user(bot, accid, event)

    if bot.has_command(event.command):
        return

    if _handle_channel_command(bot, accid, event):
        return

    if app_ctx.bot_contact_id and msg.from_id == app_ctx.bot_contact_id:
        return

    if _is_single_chat(bot, accid, dc_chat_id):
        return

    tg_chats = database.get_tg_chats(dc_chat_id)
    dc_ch_info = database.get_dc_channel_by_dc_chat_id(dc_chat_id)
    if not app_ctx.tg_app:
        return
    if not tg_chats and not dc_ch_info:
        return

    if rate_limiter.check_per_chat(dc_chat_id):
        return

    _send_bridge_message(bot, accid, dc_chat_id, msg, tg_chats)

    if dc_ch_info and dc_ch_info.get('tg_channel_id') and app_ctx.main_loop:
        _send_channel_subscriber_message(bot, accid, dc_chat_id, msg, dc_ch_info)
