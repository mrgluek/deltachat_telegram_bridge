import asyncio
import html
import logging
import os
from typing import Optional

from deltachat2 import MsgData, SystemMessageType, events

import database
from app_context import app_ctx
from channel_history import channel_history_service
from permissions import permission_checker
from rate_limiter import rate_limiter
from relay import message_relay
from shared import get_tg_help_text, to_dc_markdown
from userbot_manager import userbot_manager

logger = logging.getLogger("tg_dc_bridge")


def get_dc_help_text(bot, accid, sender_email, from_id):
    admin_dc_fingerprint = database.get_config("admin_dc_fingerprint")
    admin_dc_email = database.get_config("admin_dc_email")
    is_admin = permission_checker.is_dc_admin(bot, accid, from_id)
    mode = "Private (bot owner only)" if (admin_dc_fingerprint or admin_dc_email) else "Public (group admins only)"
    help_text = (
        f"👋 Hi {sender_email}!\n\n"
        f"I'm the TG Bridge bot. Current mode: {mode}\n\n"
        f"I relay messages between Delta Chat and Telegram groups.\n\n"
        f"Commands:\n"
        f"/channels — List public Telegram channels to subscribe to\n"
        f"/channelN — Get invite link for channel #N\n"
        f"/channelNqr — Get QR code invite for channel #N\n"
        f"/stats — Show bridge statistics for current chat\n"
        f"/help — Show this help message\n"
        f"/donate — Support bot development ❤️\n"
    )
    if not admin_dc_fingerprint and not admin_dc_email:
        help_text += "\n**Management:**\n"
        help_text += "/initadmin — Claim bot ownership (securely link your identity)\n"
    elif is_admin:
        fp_suffix = f" ({admin_dc_fingerprint[-8:].upper()})" if admin_dc_fingerprint else ""
        help_text += f"\n👑 **Admin:** `{admin_dc_email}`{fp_suffix}\n"
        help_text += (
            f"\n**Channel Management (Owner only):**\n"
            f"/channeladd @name or ID — Bridge a TG channel/group (bot as admin or Userbot)\n"
            f"/channelremove N [tg|dc] — Remove a channel bridge (add dc for DC→TG channels)\n"
            f"/channels — List bridged channels\n"
            f"/userbotjoin <link> — Join channel via Userbot (no admin needed)\n"
            f"\n**Bridge Management (Owner only):**\n"
            f"/bridge <tg_group_id> — Link DC group to a Telegram group\n"
            f"/unbridge — Remove the bridge from the group\n"
            f"/userbotsync — Force Userbot re-sync\n"
            f"/transports — Show configured mail relays & stats\n"
            f"/addtransport — Add a backup mail relay\n"
            f"/rmtransport <addr> — Remove a mail relay\n"
            f"/setprimary <addr> — Switch the primary mail relay\n"
        )
    help_text += (
        f"\nTo get started, add me to a Delta Chat group and a Telegram group, then use /bridge to connect them.\n\n"
        f"Run your own bot: https://github.com/mrgluek/deltachat_telegram_bridge"
    )
    return help_text


async def _add_dc_to_tg_bridge(bot, accid, dc_chat_id, dc_chat_name, tg_target, creator_tg_id=None):
    existing = database.get_dc_channel_by_dc_chat_id(dc_chat_id)
    if existing:
        return f"❌ This DC chat is already bridged to TG channel ID {existing['tg_channel_id']}.", None

    tg_channel_id = None
    tg_invite_link = None

    if tg_target:
        try:
            tg_target = tg_target.strip()
            is_numeric = False
            try:
                tg_channel_id = int(tg_target)
                is_numeric = True
            except ValueError:
                pass

            if not is_numeric:
                if tg_target.startswith('@'):
                    tg_target = tg_target[1:]
                if 't.me/' in tg_target:
                    tg_target = tg_target.split('/')[-1]

            chat_arg = tg_channel_id if is_numeric else f"@{tg_target}"
            tg_chat_info = await app_ctx.tg_app.bot.get_chat(chat_arg)
            if tg_chat_info.type not in ("channel", "supergroup"):
                return f"❌ <code>{html.escape(str(tg_target))}</code> is not a channel.", None
            tg_channel_id = tg_chat_info.id

            bot_member = await tg_chat_info.get_member(app_ctx.tg_app.bot.id)
            if bot_member.status not in ("administrator", "creator"):
                return f"❌ Bot is not an admin in channel <code>{html.escape(str(tg_target))}</code>.", None

            try:
                tg_invite_link = (await tg_chat_info.export_invite_link())
            except Exception:
                tg_invite_link = f"https://t.me/c/{str(tg_channel_id).replace('-100', '')}"
        except Exception as e:
            err_msg = str(e)
            if "Chat not found" in err_msg:
                err_msg = "Bot is not a member of this TG channel. Add the bot as admin first."
            return f"❌ Could not resolve TG channel: {html.escape(err_msg)}", None
    else:
        if not (app_ctx.userbot_client and app_ctx.userbot_client.is_connected()):
            return (
                "❌ No Telegram channel specified and Userbot is not available.\n\n"
                "Provide an existing TG channel: <code>/channeladd DC_LINK @channelname</code>\n"
                "Or set up Userbot for automatic channel creation.",
                None
            )

        try:
            bot_username = app_ctx.tg_app.bot.username
            tg_channel_id, tg_invite_link = await userbot_manager.create_channel(
                dc_chat_name or "DC Bridge", bot_username
            )
        except Exception as e:
            logger.error(f"Failed to create TG channel via Userbot: {e}")
            return f"❌ Failed to create TG channel: {html.escape(str(e))}", None

    row_id = database.add_dc_channel(
        dc_chat_id, dc_chat_name, tg_channel_id, tg_invite_link, creator_tg_id
    )
    if not row_id:
        return "❌ Failed to save bridge (may already exist).", None

    result = (
        f"✅ Bridge created! DC <b>{html.escape(dc_chat_name or str(dc_chat_id))}</b> → TG channel.\n\n"
        f"🔗 TG Channel invite link:\n{tg_invite_link or 'Not available'}\n\n"
        f"<i>Messages from this DC chat will now be relayed to the TG channel.</i>"
    )
    return result, tg_invite_link


def _process_dc_invite_link(bot, accid, dc_link):
    """Process a DC invite link, join the chat if needed.

    Returns (dc_chat_id, dc_chat_name) on success.
    Raises ValueError with a user-friendly message on failure.
    """
    if dc_link.startswith("OPEN-CHAT:"):
        dc_link = "https://i.delta.chat/#" + dc_link[len("OPEN-CHAT:"):]
    elif dc_link.startswith("OPEN:"):
        dc_link = "https://i.delta.chat/#" + dc_link[len("OPEN:"):]

    qr_info = bot.rpc.check_qr(accid, dc_link)
    if not qr_info:
        raise ValueError("Invalid DC invite link.")

    kind = qr_info.get('kind')
    if kind in ('askJoinBroadcast', 'askVerifyGroup'):
        dc_chat_id = bot.rpc.secure_join(accid, dc_link)
    elif kind == 'askVerifyContact':
        raise ValueError("This is a contact invite link, not a group/channel invite.")
    elif kind:
        raise ValueError(f"Unsupported link type: {kind}.")
    else:
        dc_chat_id = qr_info.get('chat_id') or qr_info.get('id')
        if not dc_chat_id:
            raise ValueError("Link does not point to a chat.")

    dc_chat_id = int(dc_chat_id)

    dc_chat_name = str(dc_chat_id)
    try:
        info = bot.rpc.get_basic_chat_info(accid, dc_chat_id)
        dc_chat_name = info.get("name") or dc_chat_name
    except Exception:
        pass

    return dc_chat_id, dc_chat_name


async def _add_channel_bridge(target: str, creator_tg_id: Optional[int] = None) -> str:
    if not app_ctx.dc_bot or not app_ctx.dc_accid:
        return "❌ Error: Delta Chat bot not initialized."

    username = target.strip()
    is_numeric = False
    numeric_id = 0

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
        channel_title = ""
        tg_channel_id = None
        resolved_username = username
        avatar_file = None
        bot_api_ok = False

        try:
            chat_arg = numeric_id if is_numeric else f"@{username}"
            tg_chat_info = await app_ctx.tg_app.bot.get_chat(chat_arg)
            if tg_chat_info.type not in ("channel", "group", "supergroup"):
                return f"❌ <code>{html.escape(display_name)}</code> is not a channel or group."

            channel_title = tg_chat_info.title or display_name
            tg_channel_id = tg_chat_info.id
            resolved_username = tg_chat_info.username
            if tg_chat_info.photo:
                avatar_file = await tg_chat_info.photo.get_big_file()
            bot_api_ok = True
        except Exception:
            pass

        if app_ctx.userbot_client and app_ctx.userbot_client.is_connected():
            try:
                from telethon.tl.functions.channels import JoinChannelRequest
                ub_arg = numeric_id if is_numeric else (resolved_username or username)
                entity = await app_ctx.userbot_client.get_entity(ub_arg)
                if getattr(entity, 'left', True):
                    if JoinChannelRequest:
                        logger.info(f"Userbot: Joining {channel_title or display_name} for history/relay...")
                        await app_ctx.userbot_client(JoinChannelRequest(entity))

                if not bot_api_ok:
                    from telethon.utils import get_peer_id
                    tg_channel_id = get_peer_id(entity)
                    channel_title = getattr(entity, 'title', display_name)
                    resolved_username = getattr(entity, 'username', username)
                    bot_api_ok = True
            except Exception as e:
                if not bot_api_ok:
                    return f"❌ Failed to resolve chat (Userbot error): {html.escape(str(e))}"
                logger.debug(f"Userbot could not resolve/join channel (already resolved by Bot API): {e}")

        if not bot_api_ok:
            return f"❌ Could not resolve <code>{html.escape(display_name)}</code> (Bot API failed and Userbot not connected)."

        existing = database.get_dc_channel_chat_id(tg_channel_id)
        if existing:
            return f"⚠️ Channel {html.escape(channel_title)} is already bridged to DC Chat ID {existing}."

        dc_chat_id = app_ctx.dc_bot.rpc.create_broadcast(app_ctx.dc_accid, channel_title)

        try:
            if avatar_file:
                avatar_path = f"tmp_avatar_{tg_channel_id}.jpg"
                await avatar_file.download_to_drive(custom_path=avatar_path)
                app_ctx.dc_bot.rpc.set_chat_profile_image(app_ctx.dc_accid, dc_chat_id, avatar_path)
                try:
                    os.unlink(avatar_path)
                except Exception:
                    pass
            elif app_ctx.userbot_client:
                photo = await app_ctx.userbot_client.download_profile_photo(tg_channel_id)
                if photo:
                    app_ctx.dc_bot.rpc.set_chat_profile_image(app_ctx.dc_accid, dc_chat_id, photo)
                    try:
                        os.unlink(photo)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Could not copy avatar for {channel_title}: {e}")

        invite_link = app_ctx.dc_bot.rpc.get_chat_securejoin_qr_code(app_ctx.dc_accid, dc_chat_id)
        if invite_link.startswith("OPEN-CHAT:"):
            invite_link = "https://i.delta.chat/#" + invite_link[10:]
        elif invite_link.startswith("OPEN:"):
            invite_link = "https://i.delta.chat/#" + invite_link[5:]

        row_id = database.add_channel_by_id(tg_channel_id, dc_chat_id, invite_link,
                                            username=resolved_username, created_by_tg_id=creator_tg_id)

        if row_id:
            rate_limiter.register_history_cooldown(dc_chat_id)

            ub_target = resolved_username if resolved_username else tg_channel_id
            await channel_history_service.relay_channel_history(
                dc_chat_id, tg_channel_id, ub_target, limit=3, invite_link=invite_link
            )

            try:
                ub_target = resolved_username if resolved_username else tg_channel_id
                ub_entity = await app_ctx.userbot_client.get_entity(ub_target)
                await userbot_manager.update_channel_stats(row_id, ub_entity)
            except Exception as e:
                logger.debug(f"Failed to sync immediate stats for {channel_title}: {e}")

            title_display = f"<b>{html.escape(channel_title)}</b>"
            if resolved_username:
                title_display += f" (@{html.escape(resolved_username)})"

            return (
                f"✅ Channel {title_display} bridged!\n\n"
                f"📺 DC Channel: <b>{html.escape(channel_title)}</b>\n"
                f"🆔 Channel #{row_id}\n\n"
                f"🔗 Subscribe in Delta Chat:\n{invite_link}\n\n"
                f"<i>(The last 3 posts have been relayed as history)</i>"
            )
        else:
            return "❌ Failed to save channel to database (may already exist)."

    except Exception as e:
        logger.error(f"Failed to bridge channel: {e}")
        return f"❌ Internal Error: {html.escape(str(e))}"


@app_ctx.dc_cli.on(events.NewMessage(command="/setprimary"))
def setprimary_command(bot, accid, event):
    msg = event.msg
    if not permission_checker.require_dc_admin(bot, accid, event):
        return

    addr = event.payload.strip()
    if not addr:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="Usage: /setprimary user@example.com"))
        return

    try:
        bot.rpc.set_config(accid, "configured_addr", addr)
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"✅ Primary address (`configured_addr`) is now `{addr}`."))
    except Exception as e:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to set primary address: {e}"))


@app_ctx.dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    msg = event.msg
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    help_msg = get_dc_help_text(bot, accid, sender_email, msg.from_id)
    message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=help_msg))


@app_ctx.dc_cli.on(events.NewMessage(command="/transports"))
def transports_command(bot, accid, event):
    msg = event.msg
    if not permission_checker.require_dc_admin(bot, accid, event):
        return

    try:
        transports = bot.rpc.list_transports(accid)
    except Exception as e:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to list transports: {e}"))
        return

    if not transports:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="No transports configured."))
        return

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

    stats_map = {}
    for s in database.get_all_transport_stats():
        stats_map[s['addr']] = s

    active_addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
    transport_addrs = []
    for t in transports:
        addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
        transport_addrs.append(addr)

    reply = f"🔌 **Mail Relays (Transports)**\n\nStatus: {connectivity_label}\n\n"

    for addr in transport_addrs:
        role = "🏠 Primary" if addr == active_addr else "🔄 Backup"
        reply += f"**{role}:** `{addr}`\n"

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
    message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=reply))


@app_ctx.dc_cli.on(events.NewMessage(command="/rmtransport"))
def rmtransport_command(bot, accid, event):
    msg = event.msg
    if not permission_checker.require_dc_admin(bot, accid, event):
        return

    addr = event.payload.strip()
    if not addr:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="Usage: /rmtransport user@example.com"))
        return

    try:
        transports = bot.rpc.list_transports(accid)
        transport_addrs = []
        for t in transports:
            a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
            transport_addrs.append(a)
        if len(transport_addrs) <= 1:
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="❌ Cannot remove the last transport."))
            return
        if addr not in transport_addrs:
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ Transport `{addr}` not found."))
            return
    except Exception as e:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to check transports: {e}"))
        return

    try:
        bot.rpc.delete_transport(accid, addr)
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"✅ Transport `{addr}` removed."))
    except Exception as e:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to remove transport: {e}"))


@app_ctx.dc_cli.on(events.NewMessage(command="/addtransport"))
def addtransport_command(bot, accid, event):
    msg = event.msg
    if not permission_checker.require_dc_admin(bot, accid, event):
        return

    payload = event.payload.strip()
    if not payload:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(
            text="Usage:\n"
                 "/addtransport DCACCOUNT:server.example\n"
                 "/addtransport user@example.com password123"
        ))
        return

    try:
        if payload.startswith("DCACCOUNT:"):
            bot.rpc.add_transport_from_qr(accid, payload)
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"✅ Backup transport added via chatmail URI."))
        else:
            parts = payload.split(None, 1)
            if len(parts) < 2:
                message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(
                    text="❌ For email accounts, provide both address and password:\n"
                         "/addtransport user@example.com password123"
                ))
                return
            addr, password = parts[0], parts[1]
            bot.rpc.add_or_update_transport(accid, {"addr": addr, "password": password})
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"✅ Backup transport `{addr}` added."))
    except Exception as e:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to add transport: {e}"))


@app_ctx.dc_cli.on(events.NewMessage(command="/donate"))
def dc_donate_command(bot, accid, event):
    msg = event.msg
    support_msg = (
        "❤️ Support Bot Development\n\n"
        "If you find this bridge useful, you can support its development and server costs here:\n\n"
        "☕️ Ko-fi: https://ko-fi.com/gluek (🌍 world cards, paypal, no commissions)\n"
        "🚀 Tribute: https://web.tribute.tg/d/IWb (🇷🇺 russian cards, SBP, high commissions)\n\n"
        "Thank you! 🙏"
    )
    message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=support_msg))


@app_ctx.dc_cli.on(events.NewMessage(command="/channeladd"))
def dc_channeladd_command(bot, accid, event):
    msg = event.msg
    payload = event.payload.strip()

    if not permission_checker.require_dc_admin(bot, accid, event):
        return

    if not payload:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="Usage: /channeladd @username or t.me link"))
        return

    is_dc_link = payload.startswith("https://i.delta.chat/") or payload.startswith("OPEN-CHAT:") or payload.startswith("OPEN:")

    if is_dc_link:
        async def run_dc_add():
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="⏳ Processing DC→TG channel bridge..."))
            parts = payload.split(None, 1)
            dc_link = parts[0]
            tg_target = parts[1] if len(parts) > 1 else None

            try:
                dc_chat_id, dc_chat_name = _process_dc_invite_link(bot, accid, dc_link)
            except ValueError as e:
                message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ {e}"))
                return

            result_html, _ = await _add_dc_to_tg_bridge(
                bot, accid, dc_chat_id, dc_chat_name, tg_target,
                creator_tg_id=None
            )
            result_md = to_dc_markdown(result_html)
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=result_md))

        if app_ctx.main_loop:
            asyncio.run_coroutine_threadsafe(run_dc_add(), app_ctx.main_loop)
        else:
            logger.error("Main loop not found, cannot run channeladd")
    else:
        async def run_add():
            result = await _add_channel_bridge(payload)
            result_md = to_dc_markdown(result)
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=result_md))

        if app_ctx.main_loop:
            asyncio.run_coroutine_threadsafe(run_add(), app_ctx.main_loop)
        else:
            logger.error("Main loop not found, cannot run channeladd")


@app_ctx.dc_cli.on(events.NewMessage(command="/channelremove"))
def dc_channelremove_command(bot, accid, event):
    msg = event.msg
    payload = event.payload.strip()

    if not permission_checker.require_dc_admin(bot, accid, event):
        return

    if not payload:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="Usage: /channelremove N [tg|dc] (channel number, optionally specify tg or dc direction)"))
        return

    parts = payload.split()
    try:
        channel_id = int(parts[0])
    except ValueError:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="❌ Invalid channel number."))
        return

    direction = parts[1].lower() if len(parts) > 1 else None
    if direction and direction not in ("tg", "dc"):
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="❌ Unknown direction. Use `/channelremove N tg` or `/channelremove N dc`."))
        return

    if direction == "tg":
        ch = database.get_channel_by_id(channel_id)
        if not ch:
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ TG→DC channel #{channel_id} not found."))
            return
        tg_channel_id = database.remove_channel(channel_id)
        if tg_channel_id:
            rate_limiter.clear_caches(ch['dc_chat_id'])
            if app_ctx.main_loop:
                asyncio.run_coroutine_threadsafe(userbot_manager.leave_chat(tg_channel_id), app_ctx.main_loop)
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"✅ TG→DC channel bridge #{channel_id} removed."))
        else:
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to remove TG→DC channel #{channel_id}."))
        return

    if direction == "dc":
        dc_ch = database.get_dc_channel_by_id(channel_id)
        if not dc_ch:
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ DC→TG channel #{channel_id} not found."))
            return
        dc_chat_id = dc_ch['dc_chat_id']
        tg_channel_id = database.remove_dc_channel(dc_chat_id)
        if tg_channel_id:
            rate_limiter.clear_caches(dc_chat_id)
            if app_ctx.main_loop:
                asyncio.run_coroutine_threadsafe(userbot_manager.leave_chat(tg_channel_id), app_ctx.main_loop)
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"✅ DC→TG channel bridge #{channel_id} removed."))
        else:
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to remove DC→TG channel #{channel_id}."))
        return

    ch = database.get_channel_by_id(channel_id)
    if ch:
        tg_channel_id = database.remove_channel(channel_id)
        if tg_channel_id:
            rate_limiter.clear_caches(ch['dc_chat_id'])
            if app_ctx.main_loop:
                asyncio.run_coroutine_threadsafe(userbot_manager.leave_chat(tg_channel_id), app_ctx.main_loop)
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"✅ TG→DC channel bridge #{channel_id} removed."))
        else:
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to remove channel #{channel_id}."))
        return

    dc_ch = database.get_dc_channel_by_id(channel_id)
    if dc_ch:
        dc_chat_id = dc_ch['dc_chat_id']
        tg_channel_id = database.remove_dc_channel(dc_chat_id)
        if tg_channel_id:
            rate_limiter.clear_caches(dc_chat_id)
            if app_ctx.main_loop:
                asyncio.run_coroutine_threadsafe(userbot_manager.leave_chat(tg_channel_id), app_ctx.main_loop)
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"✅ DC→TG channel bridge #{channel_id} removed."))
        else:
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to remove DC→TG channel #{channel_id}."))
        return

    message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ Channel #{channel_id} not found."))


@app_ctx.dc_cli.on(events.NewMessage(command="/bridge"))
def bridge_command(bot, accid, event):
    msg = event.msg
    chat_id = msg.chat_id

    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    if chat_info.get("type") == 1:
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ You must send that command in a Delta Chat group, not here."))
        return

    if database.get_config("admin_dc_fingerprint") or database.get_config("admin_dc_email"):
        if not permission_checker.is_dc_admin(bot, accid, msg.from_id):
            message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ Only the configured bot administrator can use /bridge."))
            return
    else:
        try:
            contacts = bot.rpc.get_chat_contacts(accid, chat_id)
            if msg.from_id not in contacts[:1] and msg.from_id != 1:
                message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ Only group admins can use /bridge. (Or set a global admin via /initadmin)"))
                return
        except Exception:
            pass

    try:
        payload = event.payload.strip()
        if not payload:
            raise ValueError()
        tg_chat_id = int(payload)
    except ValueError:
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ You must provide the Telegram chat ID. Example: /bridge -123456789"))
        return

    if database.add_bridge(chat_id, tg_chat_id):
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text=f"✔️ Bridged with Telegram group {tg_chat_id}."))
    else:
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ This chat is already bridged."))


@app_ctx.dc_cli.on(events.NewMessage(command="/unbridge"))
def unbridge_command(bot, accid, event):
    msg = event.msg
    chat_id = msg.chat_id

    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    if chat_info.get("type") == 1:
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ You must send that command in a Delta Chat group, not here."))
        return

    if database.get_config("admin_dc_fingerprint") or database.get_config("admin_dc_email"):
        if not permission_checker.is_dc_admin(bot, accid, msg.from_id):
            message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ Only the configured bot administrator can use /unbridge."))
            return
    else:
        try:
            contacts = bot.rpc.get_chat_contacts(accid, chat_id)
            if msg.from_id not in contacts[:1] and msg.from_id != 1:
                message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ Only group admins can use /unbridge. (Or set a global admin via /initadmin)"))
                return
        except Exception:
            pass

    tg_chat_ids = database.remove_bridge(chat_id)
    if tg_chat_ids:
        rate_limiter.clear_caches(chat_id)
        if app_ctx.main_loop:
            for tid in tg_chat_ids:
                asyncio.run_coroutine_threadsafe(userbot_manager.leave_chat(tid), app_ctx.main_loop)
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="✔️ Bridge removed."))
    else:
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ This chat is not bridged."))


@app_ctx.dc_cli.on(events.NewMessage(command="/locupdate"))
def locupdate_command(bot, accid, event):
    msg = event.msg
    chat_id = msg.chat_id

    if not hasattr(msg, 'quote') or not msg.quote:
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ Please reply to a Live Location message with /locupdate."))
        return

    quote_msg_id = msg.quote.get('message_id') if isinstance(msg.quote, dict) else None
    if not quote_msg_id:
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ Please reply to a Live Location message with /locupdate."))
        return

    tg_chats = database.get_tg_chats(chat_id)
    found = False

    if tg_chats:
        for tg_chat_id in tg_chats:
            tg_msg_id = database.get_tg_msg_id(quote_msg_id, chat_id, tg_chat_id)
            if tg_msg_id and tg_msg_id in message_relay.live_locations:
                lat, lon = message_relay.live_locations[tg_msg_id]
                message_relay.dc_send_msg(bot, accid, chat_id, MsgData(
                    text=f"📍 Updated Location: https://maps.google.com/?q={lat},{lon}",
                    quoted_message_id=quote_msg_id
                ))
                found = True
                break

    if not found:
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(
            text="❌ No active live location found for this message. It may have expired or not be a live location.",
            quoted_message_id=msg.id
        ))


@app_ctx.dc_cli.on(events.NewMessage(command="/userbotsync"))
def dc_userbotsync_command(bot, accid, event):
    msg = event.msg
    if not permission_checker.require_dc_admin(bot, accid, event):
        return

    async def run_sync():
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="⏳ Starting Userbot synchronization..."))
        await userbot_manager.sync_channels(force=True)
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="✅ Userbot synchronization completed (subscriber counts updated)."))

    if app_ctx.main_loop:
        asyncio.run_coroutine_threadsafe(run_sync(), app_ctx.main_loop)
    else:
        logger.error("Main loop not found, cannot run userbotsync")


@app_ctx.dc_cli.on(events.NewMessage(command="/userbotjoin"))
def dc_userbotjoin_command(bot, accid, event):
    msg = event.msg
    if not permission_checker.require_dc_admin(bot, accid, event):
        return

    text = msg.text.strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(
            text="/userbotjoin <link>\n\n"
                 "Examples:\n"
                 "• /userbotjoin https://t.me/+AbCdEfGhIjK\n"
                 "• /userbotjoin https://t.me/channelname\n"
                 "• /userbotjoin @channelname"
        ))
        return

    link = parts[1].strip()

    async def do_join():
        if not (app_ctx.userbot_client and app_ctx.userbot_client.is_connected()):
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="❌ Userbot is not connected."))
            return

        message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"⏳ Attempting to join via Userbot: {link}..."))

        try:
            joined_entity, joined_title, tg_channel_id = await userbot_manager.join_chat(link)

            if joined_entity:
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
                    message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(
                        text=f"✅ Userbot joined *{joined_title}* (ID: {tg_channel_id}).\n"
                             f"Invite link saved for future syncs.\n\n"
                             f"Matched to bridged channel #{matched_channel['id']}."
                    ))
                else:
                    message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(
                        text=f"✅ Userbot joined *{joined_title}* (ID: {tg_channel_id}).\n\n"
                             f"⚠️ This channel is not yet bridged."
                    ))

                try:
                    chan_id = matched_channel['id'] if matched_channel else None
                    if chan_id:
                        await userbot_manager.update_channel_stats(chan_id, joined_entity)
                except Exception:
                    pass
            else:
                message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="⚠️ Joined but could not determine channel details."))

        except Exception as e:
            logger.error(f"DC userbotjoin failed for {link}: {e}")
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to join: {str(e)[:500]}"))

    if app_ctx.main_loop:
        asyncio.run_coroutine_threadsafe(do_join(), app_ctx.main_loop)
    else:
        logger.error("Main loop not found, cannot run userbotjoin")


@app_ctx.dc_cli.on(events.NewMessage(command="/stats"))
def stats_command(bot, accid, event):
    msg = event.msg
    chat_id = msg.chat_id

    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    is_private = chat_info.get("type") == 1

    if is_private:
        if not permission_checker.is_dc_admin(bot, accid, msg.from_id):
            message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ Only the bot admin can view all stats."))
            return

        bridges = database.get_all_bridges()
        if not bridges:
            message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="📊 No bridges configured."))
            return

        lines = [f"📊 Bridge Statistics ({len(bridges)} bridge{'s' if len(bridges) != 1 else ''})\n"]
        for row in bridges:
            dc_cid, tg_cid, r_count = row if len(row) == 3 else (row[0], row[1], 0)
            m_count = database.get_bridge_message_count(dc_cid, tg_cid)
            try:
                title = bot.rpc.get_basic_chat_info(accid, dc_cid).get("name", "Unknown Group")
            except Exception:
                title = "Unknown Group"
            lines.append(f"• DC {dc_cid} ↔ TG {tg_cid} ({title}) — {m_count} 💬 {r_count} 🙂")

        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="\n".join(lines)))
    else:
        tg_chats = database.get_tg_chats(chat_id)
        if not tg_chats:
            message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="📊 This group is not bridged."))
            return

        lines = ["📊 Bridge Statistics\n"]
        for tg_cid in tg_chats:
            m_count = database.get_bridge_message_count(chat_id, tg_cid)
            try:
                title = bot.rpc.get_basic_chat_info(accid, chat_id).get("name", "this group")
            except Exception:
                title = "this group"
            r_count = database.get_bridge_reaction_count(chat_id, tg_cid)
            lines.append(f"• TG Group {tg_cid} ({title}) — {m_count} 💬 {r_count} 🙂")

        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="\n".join(lines)))


@app_ctx.dc_cli.on(events.NewMessage(is_info=True))
def handle_dc_info_message(bot, accid, event):
    msg = event.msg
    dc_chat_id = msg.chat_id

    smt = msg.system_message_type
    smt_str = str(smt).lower() if smt is not None else ""

    logger.debug(f"DC info msg in chat {dc_chat_id}: system_message_type={smt!r} ({smt_str!r})")

    is_member_event = False
    try:
        if smt == SystemMessageType.MEMBER_ADDED_TO_GROUP:
            is_member_event = True
        elif hasattr(SystemMessageType, 'MEMBER_JOINED_GROUP') and smt == SystemMessageType.MEMBER_JOINED_GROUP:
            is_member_event = True
    except Exception:
        pass

    if not is_member_event:
        is_member_event = any(kw in smt_str.replace(" ", "").replace("_", "") for kw in
                              ("memberadded", "memberjoined"))

    if is_member_event:
        logger.info(f"Member event detected in DC chat {dc_chat_id} (type={smt!r})")
        ch = database.get_channel_by_dc_chat_id(dc_chat_id)
        if ch and app_ctx.main_loop:
            tg_channel_id = ch['tg_channel_id']
            ub_target = ch['tg_channel_username'] if ch['tg_channel_username'] else tg_channel_id

            if ub_target:
                logger.info(f"New member joined bridged DC channel {dc_chat_id}. Relaying history...")
                invite_link = ch.get('invite_link')
                asyncio.run_coroutine_threadsafe(
                    channel_history_service.relay_channel_history(
                        dc_chat_id, tg_channel_id, ub_target, limit=3, invite_link=invite_link
                    ),
                    app_ctx.main_loop
                )
        elif not ch:
            logger.debug(f"DC chat {dc_chat_id} is not a bridged channel, skipping history relay.")


@app_ctx.dc_cli.on(events.NewMessage(command="/channels"))
def channels_command_dc(bot, accid, event):
    msg = event.msg
    chat_id = msg.chat_id
    payload = event.payload.strip().lower()

    if not payload:
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(
            text="📺 **Channels help**\n\n"
                 "• `/channels tg` — Show TG→DC channels (TG channels bridged to DC)\n"
                 "• `/channels dc` — Show DC→TG channels (DC chats bridged to TG)"
        ))
        return

    is_admin = permission_checker.is_dc_admin(bot, accid, msg.from_id)

    if payload == "tg":
        channels = database.get_all_channels()
        if is_admin:
            display_channels = channels
        else:
            display_channels = [c for c in channels if c.get('tg_channel_username')]

        if not display_channels:
            message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="📺 No TG→DC channels configured."))
            return

        lines = [f"📺 **{'All' if is_admin else 'Public'} TG→DC Channels:**\n"]
        for ch in display_channels:
            dc_cid = ch['dc_chat_id']
            tg_username = ch.get('tg_channel_username')
            tg_id = ch.get('tg_channel_id', 0)
            m_count = database.get_bridge_message_count(dc_cid, tg_id)
            tg_sub_count = ch.get('tg_participants_count', 0)
            try:
                chat_info = bot.rpc.get_basic_chat_info(accid, dc_cid)
                title = chat_info.get("name", "Unknown Channel")
                contacts = bot.rpc.get_chat_contacts(accid, dc_cid)
                dc_sub_count = len(contacts) - 1 if contacts else 0
            except Exception:
                title = "Unknown Channel"
                dc_sub_count = "?"
            tg_ref = f"(t.me/{tg_username})" if tg_username else f"(ID: {tg_id})"
            stats_str = f"👤 {tg_sub_count:,} TG / {dc_sub_count} DC — 💬 {m_count}"
            lines.append(f"/channel{ch['id']} — {title} {tg_ref} — {stats_str}")
        lines.append("\nUse /channelN for link or /channelNqr for QR.")
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="\n".join(lines)))

    elif payload == "dc":
        if not is_admin:
            message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="❌ Only the bot administrator can view DC→TG channels."))
            return
        dc_channels = database.get_all_dc_channels()
        if not dc_channels:
            message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="📺 No DC→TG channels configured."))
            return
        lines = [f"📺 **DC→TG Channels:**\n"]
        for ch in dc_channels:
            dc_cid = ch['dc_chat_id']
            name = ch.get('dc_chat_name', f"DC Chat {dc_cid}")
            tg_id = ch.get('tg_channel_id', '?')
            m_count = database.get_bridge_message_count(dc_cid, tg_id)
            try:
                contacts = bot.rpc.get_chat_contacts(accid, dc_cid)
                dc_sub_count = len(contacts) - 1 if contacts else 0
            except Exception:
                dc_sub_count = "?"
            link = ch.get('tg_invite_link', 'No link')
            lines.append(f"**#{ch['id']}** — **{name}** — 💬 {m_count} — 👤 {dc_sub_count} DC\n  🔗 {link}")
        lines.append("\nUse `/channelremove N dc` to remove.")
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(text="\n".join(lines)))

    else:
        message_relay.dc_send_msg(bot, accid, chat_id, MsgData(
            text="❌ Unknown argument. Use `/channels tg` or `/channels dc`."
        ))
