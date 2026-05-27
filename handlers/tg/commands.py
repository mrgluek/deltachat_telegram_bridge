import asyncio
import html
import logging
import os
import re
import tempfile
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

import database
from app_context import app_ctx
from permissions import permission_checker
from rate_limiter import rate_limiter
from userbot_manager import userbot_manager
from channel_history import channel_history_service
from qrcode_gen import qr_generator
from handlers.dc.commands import _add_dc_to_tg_bridge, _process_dc_invite_link
from shared import get_tg_help_text

logger = logging.getLogger("tg_dc_bridge")


async def tg_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = html.escape(user.first_name)
    greeting = get_tg_help_text(name, user.id)
    await update.effective_message.reply_text(greeting, parse_mode='HTML')


async def tg_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = html.escape(user.first_name)
    help_msg = get_tg_help_text(name, user.id)
    await update.effective_message.reply_text(help_msg, parse_mode='HTML')


async def tg_donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    support_msg = (
        "❤️ <b>Support Bot Development</b>\n\n"
        "If you find this bridge useful, you can support its development and server costs here:\n\n"
        "🔗 <a href='https://t.me/tribute/app?startapp=dIWb'>Support via Tribute</a>\n\n"
        "Thank you! 🙏"
    )
    await update.effective_message.reply_html(support_msg, disable_web_page_preview=True)


async def tg_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.effective_message.reply_text("❌ You must send that command in a Telegram group, not here.")
        return

    try:
        member = await chat.get_member(user.id)
        if member.status not in ("administrator", "creator"):
            await update.effective_message.reply_text("❌ Only group admins can use /id.")
            return
    except Exception:
        pass

    await update.effective_message.reply_text(f"Group ID: <code>{chat.id}</code>", parse_mode='HTML')


async def tg_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        admin_tg_id = database.get_config("admin_tg_id")
        if admin_tg_id:
            if database.is_owner(user.id):
                bridges = database.get_all_bridges()
            elif database.is_admin(user.id):
                bridges = database.get_bridges_by_creator(user.id)
            else:
                await update.effective_message.reply_text("❌ Only the bot admin can view stats.")
                return
        else:
            bridges = database.get_all_bridges()

        if not bridges:
            await update.effective_message.reply_text("📊 No bridges configured.")
            return

        lines = [f"📊 <b>Bridge Statistics</b> ({len(bridges)} bridge{'s' if len(bridges) != 1 else ''})\n"]
        for row in bridges:
            dc_cid, tg_cid = row[0], row[1]
            r_count = row[2] if len(row) > 2 else 0
            m_count = database.get_bridge_message_count(dc_cid, tg_cid)
            try:
                chat_info = app_ctx.dc_bot.rpc.get_basic_chat_info(app_ctx.dc_accid, dc_cid)
                title = chat_info.get("name", "Unknown Group")
                contacts = app_ctx.dc_bot.rpc.get_chat_contacts(app_ctx.dc_accid, dc_cid)
                sub_count = len(contacts) - 1 if contacts else 0
            except Exception:
                title = "Unknown Group"
                sub_count = "?"
            lines.append(f"• DC <code>{dc_cid}</code> ↔ TG <code>{tg_cid}</code> ({html.escape(title)}) — {sub_count} 👤 {m_count} 💬 {r_count} 🙂")

        await update.effective_message.reply_text("\n".join(lines), parse_mode='HTML')
    else:
        admin_tg_id = database.get_config("admin_tg_id")
        if admin_tg_id:
            is_privileged = database.is_owner(user.id)
            if not is_privileged and database.is_admin(user.id):
                creator = database.get_bridge_creator_by_tg(chat.id)
                is_privileged = (creator is not None and creator == user.id)
            if not is_privileged:
                try:
                    member = await chat.get_member(user.id)
                    if member.status not in ("administrator", "creator"):
                        await update.effective_message.reply_text("❌ Only group admins can view stats.")
                        return
                except Exception:
                    pass

        dc_chats = database.get_dc_chats(chat.id)
        if not dc_chats:
            await update.effective_message.reply_text("📊 This group is not bridged.")
            return

        lines = ["📊 <b>Bridge Statistics</b>\n"]
        for dc_cid in dc_chats:
            m_count = database.get_bridge_message_count(dc_cid, chat.id)
            r_count = database.get_bridge_reaction_count(dc_cid, chat.id)
            try:
                title = app_ctx.dc_bot.rpc.get_basic_chat_info(app_ctx.dc_accid, dc_cid).get("name", "this group")
            except Exception:
                title = "this group"
            lines.append(f"• DC <code>{dc_cid}</code> ({html.escape(title)}) — {m_count} 💬 {r_count} 🙂")
        await update.effective_message.reply_text("\n".join(lines), parse_mode='HTML')


async def tg_bridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.effective_message.reply_text("❌ You must send /bridge in a Telegram group, not here.")
        return

    admin_tg_id = database.get_config("admin_tg_id")
    if admin_tg_id:
        if not database.is_owner_or_admin(user.id):
            await update.effective_message.reply_text("❌ Only the bot admin can use /bridge.")
            return
    else:
        try:
            member = await chat.get_member(user.id)
            if member.status not in ("administrator", "creator"):
                await update.effective_message.reply_text("❌ Only group admins can use /bridge.")
                return
        except Exception:
            pass

    existing = database.get_dc_chats(chat.id)
    if existing:
        await update.effective_message.reply_text("❌ This group is already bridged.")
        return

    if not app_ctx.dc_bot or not app_ctx.dc_accid:
        await update.effective_message.reply_text("❌ Delta Chat bot is not ready yet.")
        return

    tg_chat_id = chat.id
    tg_title = chat.title or f"TG Group {tg_chat_id}"

    await update.effective_message.reply_text(f"⏳ Setting up bridge for <b>{html.escape(tg_title)}</b>...", parse_mode='HTML')

    try:
        dc_chat_id = app_ctx.dc_bot.rpc.create_group_chat(app_ctx.dc_accid, tg_title, False)

        try:
            tg_chat_info = await context.bot.get_chat(tg_chat_id)
            if tg_chat_info.photo:
                avatar_file = await tg_chat_info.photo.get_big_file()
                tmp_fd, avatar_path = tempfile.mkstemp(suffix=".jpg")
                os.close(tmp_fd)
                await avatar_file.download_to_drive(custom_path=avatar_path)
                app_ctx.dc_bot.rpc.set_chat_profile_image(app_ctx.dc_accid, dc_chat_id, avatar_path)
                try:
                    os.unlink(avatar_path)
                except Exception:
                    pass
                logger.info(f"Copied avatar from TG group {tg_chat_id} to DC group {dc_chat_id}")
        except Exception as e:
            logger.warning(f"Could not copy group avatar: {e}")

        database.add_bridge(dc_chat_id, tg_chat_id, created_by_tg_id=user.id)

        invite_link = app_ctx.dc_bot.rpc.get_chat_securejoin_qr_code(app_ctx.dc_accid, dc_chat_id)
        if invite_link.startswith("OPEN-CHAT:"):
            invite_link = "https://i.delta.chat/#" + invite_link[10:]
        elif invite_link.startswith("OPEN:"):
            invite_link = "https://i.delta.chat/#" + invite_link[5:]

        await update.effective_message.reply_text(
            f"✅ Bridged! DC group <b>{html.escape(tg_title)}</b> created.\n\n"
            f"🔗 Join in Delta Chat:\n{html.escape(invite_link)}",
            parse_mode='HTML',
            disable_web_page_preview=True
        )

        logger.info(f"TG bridge: user {user.id} bridged TG {tg_chat_id} -> DC {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to create TG bridge: {e}")
        await update.effective_message.reply_text(f"❌ Error: {html.escape(str(e))}", parse_mode='HTML')


async def tg_unbridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.effective_message.reply_text("❌ You must send /unbridge in a Telegram group, not here.")
        return

    admin_tg_id = database.get_config("admin_tg_id")
    if admin_tg_id:
        if database.is_owner(user.id):
            pass
        elif database.is_admin(user.id):
            creator = database.get_bridge_creator_by_tg(chat.id)
            if creator != user.id:
                await update.effective_message.reply_text("❌ You can only unbridge groups you created.")
                return
        else:
            await update.effective_message.reply_text("❌ Only the bot admin can use /unbridge.")
            return
    else:
        try:
            member = await chat.get_member(user.id)
            if member.status not in ("administrator", "creator"):
                await update.effective_message.reply_text("❌ Only group admins can use /unbridge.")
                return
        except Exception:
            pass

    dc_chat_ids = database.get_dc_chats(chat.id)

    if database.remove_bridge_by_tg(chat.id):
        for dc_cid in dc_chat_ids:
            rate_limiter.clear_caches(dc_cid)

        asyncio.create_task(userbot_manager.leave_chat(chat.id))

        await update.effective_message.reply_text("✔️ Bridge removed.")
    else:
        await update.effective_message.reply_text("❌ This group is not bridged.")


async def tg_adminadd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "private":
        await update.effective_message.reply_text("❌ This command can only be used in a private chat with the bot.")
        return

    if not database.is_owner(user.id):
        await update.effective_message.reply_text("❌ Only the bot owner can manage admins.")
        return

    if not context.args or len(context.args) < 1:
        await update.effective_message.reply_text(
            "Usage: <code>/adminadd user_id</code>\n\n"
            "The user should send /start to the bot first to get their user ID.",
            parse_mode='HTML'
        )
        return

    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Please provide a valid numeric user ID.")
        return

    if str(new_admin_id) == str(database.get_config("admin_tg_id")):
        await update.effective_message.reply_text("❌ You are already the owner.")
        return

    if database.add_admin(new_admin_id):
        await update.effective_message.reply_text(f"✅ User <code>{new_admin_id}</code> added as sub-admin.", parse_mode='HTML')
    else:
        await update.effective_message.reply_text(f"❌ User <code>{new_admin_id}</code> is already a sub-admin.", parse_mode='HTML')


async def tg_adminremove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "private":
        await update.effective_message.reply_text("❌ This command can only be used in a private chat with the bot.")
        return

    if not database.is_owner(user.id):
        await update.effective_message.reply_text("❌ Only the bot owner can manage admins.")
        return

    if not context.args or len(context.args) < 1:
        await update.effective_message.reply_text("Usage: <code>/adminremove user_id</code>", parse_mode='HTML')
        return

    try:
        admin_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Please provide a valid numeric user ID.")
        return

    if database.remove_admin(admin_id):
        await update.effective_message.reply_text(f"✅ User <code>{admin_id}</code> removed from sub-admins.", parse_mode='HTML')
    else:
        await update.effective_message.reply_text(f"❌ User <code>{admin_id}</code> is not a sub-admin.", parse_mode='HTML')


async def tg_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "private":
        await update.effective_message.reply_text("❌ This command can only be used in a private chat with the bot.")
        return

    if not database.is_owner(user.id):
        await update.effective_message.reply_text("❌ Only the bot owner can view admins.")
        return

    admins = database.get_all_admins()
    if not admins:
        await update.effective_message.reply_text("👥 No sub-admins configured.\n\nUse <code>/adminadd user_id</code> to add one.", parse_mode='HTML')
        return

    lines = [f"👥 <b>Sub-admins</b> ({len(admins)})\n"]
    for admin_id in admins:
        lines.append(f"• <code>{admin_id}</code>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode='HTML')


async def tg_invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await permission_checker.check_tg_invite_permissions(update):
        return

    chat = update.effective_chat

    if chat.type == "private":
        try:
            qrdata = app_ctx.dc_bot.rpc.get_chat_securejoin_qr_code(app_ctx.dc_accid, None)
            qrdata = qr_generator.normalize_qr_data(qrdata)
            await update.effective_message.reply_text(f"🔗 <b>Bot Setup Link</b>\n{html.escape(qrdata)}", parse_mode='HTML', disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Failed to generate bot setup link: {e}")
            await update.effective_message.reply_text("❌ Error generating bot setup link.")
        return

    dc_chats = database.get_dc_chats(chat.id)
    if not dc_chats:
        await update.effective_message.reply_text("❌ This group is not bridged to any Delta Chat group.")
        return

    lines = []
    for dc_cid in dc_chats:
        try:
            chat_info = app_ctx.dc_bot.rpc.get_basic_chat_info(app_ctx.dc_accid, dc_cid)
            chat_name = chat_info.get("name") or str(dc_cid)

            qrdata = app_ctx.dc_bot.rpc.get_chat_securejoin_qr_code(app_ctx.dc_accid, dc_cid)
            qrdata = qr_generator.normalize_qr_data(qrdata)

            lines.append(f"🔗 Click to join bridged DC Group <b>{html.escape(chat_name)}</b>:\n{html.escape(qrdata)}\n")
        except Exception as e:
            logger.error(f"Failed to generate invite link for DC chat {dc_cid}: {e}")
            lines.append(f"❌ Error generating link for DC Group {dc_cid}.\n")

    await update.effective_message.reply_text("\n".join(lines).strip(), parse_mode='HTML', disable_web_page_preview=True)


async def tg_inviteqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await permission_checker.check_tg_invite_permissions(update):
        return

    chat = update.effective_chat

    if not qr_generator.is_qrcode_available():
        await update.effective_message.reply_text("❌ QR code generation is not supported. Please install 'qrcode[pil]' python package.")
        return

    if chat.type == "private":
        try:
            qrdata = app_ctx.dc_bot.rpc.get_chat_securejoin_qr_code(app_ctx.dc_accid, None)
            bio = qr_generator.generate_qr_image(qrdata)
            if bio:
                await update.effective_message.reply_photo(photo=bio, caption="Scan to add bot in Delta Chat")
            else:
                await update.effective_message.reply_text("❌ Cannot generate image. Ensure 'Pillow' is installed (`pip install Pillow`).")
        except Exception as e:
            logger.error(f"Failed to generate bot setup QR: {e}")
            await update.effective_message.reply_text("❌ Error generating QR code.")
        return

    dc_chats = database.get_dc_chats(chat.id)
    if not dc_chats:
        return

    for dc_cid in dc_chats:
        try:
            chat_info = app_ctx.dc_bot.rpc.get_basic_chat_info(app_ctx.dc_accid, dc_cid)
            chat_name = chat_info.get("name") or str(dc_cid)

            qrdata = app_ctx.dc_bot.rpc.get_chat_securejoin_qr_code(app_ctx.dc_accid, dc_cid)
            bio = qr_generator.generate_qr_image(qrdata)
            if bio:
                await update.effective_message.reply_photo(photo=bio, caption=f"Scan to join bridged DC Group {chat_name}")
            else:
                await update.effective_message.reply_text("❌ Cannot generate image. Ensure 'Pillow' is installed (`pip install Pillow`).")
        except Exception as e:
            logger.error(f"Failed to generate invite QR for DC chat {dc_cid}: {e}")
            await update.effective_message.reply_text(f"❌ Error generating QR code for DC Group {dc_cid}.", parse_mode='HTML')


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
                ub_arg = numeric_id if is_numeric else (resolved_username or username)
                entity = await app_ctx.userbot_client.get_entity(ub_arg)
                if getattr(entity, 'left', True):
                    from telethon.tl.functions.channels import JoinChannelRequest
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

        existing = database.get_channel_by_tg_id(tg_channel_id)
        if existing:
            return f"⚠️ Channel {html.escape(channel_title)} is already bridged to DC Chat ID {existing['dc_chat_id']}."

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
        invite_link = qr_generator.normalize_qr_data(invite_link)

        if resolved_username:
            row_id = database.add_channel(resolved_username, dc_chat_id, invite_link, creator_tg_id)
        else:
            row_id = database.add_channel_by_id(tg_channel_id, dc_chat_id, invite_link, None, creator_tg_id)

        if row_id:
            rate_limiter.register_history_cooldown(dc_chat_id)

            ub_target = resolved_username if resolved_username else tg_channel_id
            await channel_history_service.relay_channel_history(dc_chat_id, tg_channel_id, ub_target, limit=3, invite_link=invite_link)

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


async def tg_channeladd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await permission_checker.check_channel_admin(update):
        return

    if not context.args or len(context.args) < 1:
        await update.effective_message.reply_text(
            "Usage: <code>/channeladd @name</code>\n"
            "or: <code>/channeladd https://t.me/name</code>\n"
            "or: <code>/channeladd -1001234567890</code>\n\n"
            "Example: <code>/channeladd @ia_panorama</code>\n"
            "Supports both <b>channels</b> and <b>groups</b> in read-only mode.",
            parse_mode='HTML'
        )
        return

    raw_arg = context.args[0].strip()

    is_dc_link = raw_arg.startswith("https://i.delta.chat/") or raw_arg.startswith("OPEN-CHAT:") or raw_arg.startswith("OPEN:")

    if is_dc_link:
        status_msg = await update.effective_message.reply_text("⏳ Processing DC→TG channel bridge...")
        try:
            dc_chat_id, dc_chat_name = _process_dc_invite_link(
                app_ctx.dc_bot, app_ctx.dc_accid, raw_arg
            )
        except ValueError as e:
            await status_msg.edit_text(f"❌ {e}")
            return

        tg_target = context.args[1].strip() if len(context.args) > 1 else None
        result_html, _ = await _add_dc_to_tg_bridge(
            app_ctx.dc_bot, app_ctx.dc_accid, dc_chat_id, dc_chat_name,
            tg_target, creator_tg_id=update.effective_user.id
        )
        await status_msg.edit_text(result_html, parse_mode='HTML', disable_web_page_preview=True)
        return

    status_msg = await update.effective_message.reply_text("⏳ Processing bridge request...")

    result = await _add_channel_bridge(raw_arg, creator_tg_id=update.effective_user.id)
    await status_msg.edit_text(result, parse_mode='HTML', disable_web_page_preview=True)


async def tg_userbotsync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not database.is_owner(update.effective_user.id):
        return

    if not (app_ctx.userbot_client and app_ctx.userbot_client.is_connected()):
        await update.effective_message.reply_text("❌ Userbot is not connected.")
        return

    await update.effective_message.reply_text("⏳ Starting Userbot synchronization...")
    asyncio.create_task(userbot_manager.sync_channels(force=True))


async def tg_userbotjoin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not database.is_owner(update.effective_user.id):
        return

    if not (app_ctx.userbot_client and app_ctx.userbot_client.is_connected()):
        await update.effective_message.reply_text("❌ Userbot is not connected.")
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: <code>/userbotjoin &lt;invite_link_or_username&gt;</code>\n\n"
            "Examples:\n"
            "• <code>/userbotjoin https://t.me/+AbCdEfGhIjK</code>\n"
            "• <code>/userbotjoin https://t.me/channelname</code>\n"
            "• <code>/userbotjoin @channelname</code>",
            parse_mode='HTML'
        )
        return

    link = context.args[0].strip()
    status_msg = await update.effective_message.reply_text(
        f"⏳ Attempting to join via Userbot: <code>{html.escape(link)}</code>...",
        parse_mode='HTML'
    )

    try:
        joined_entity, joined_title, tg_channel_id = await userbot_manager.join_chat(link)

        if joined_entity:
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
                await status_msg.edit_text(
                    f"✅ Userbot joined <b>{html.escape(joined_title)}</b> "
                    f"(ID: <code>{tg_channel_id}</code>).\n"
                    f"Invite link saved for future syncs.\n\n"
                    f"Matched to bridged channel #{matched_channel['id']}.",
                    parse_mode='HTML'
                )
            else:
                await status_msg.edit_text(
                    f"✅ Userbot joined <b>{html.escape(joined_title)}</b> "
                    f"(ID: <code>{tg_channel_id}</code>).\n\n"
                    f"⚠️ This channel is not yet bridged. Use <code>/channeladd {tg_channel_id}</code> to bridge it.",
                    parse_mode='HTML'
                )

            try:
                chan_id = matched_channel['id'] if matched_channel else None
                if chan_id:
                    await userbot_manager.update_channel_stats(chan_id, joined_entity)
            except Exception:
                pass
        else:
            await status_msg.edit_text("⚠️ Joined successfully but could not determine channel details.")

    except Exception as e:
        error_msg = str(e)
        logger.error(f"userbotjoin failed for {link}: {e}")
        await status_msg.edit_text(
            f"❌ Failed to join: <code>{html.escape(error_msg[:500])}</code>",
            parse_mode='HTML'
        )


async def tg_groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not database.is_owner(update.effective_user.id):
        return

    if not (app_ctx.userbot_client and app_ctx.userbot_client.is_connected()):
        await update.effective_message.reply_text("❌ Userbot is not connected.")
        return

    status_msg = await update.effective_message.reply_text("⏳ Fetching your groups from Telegram...")

    try:
        bridged_channels = database.get_all_channels()
        bridged_ids = {c['tg_channel_id'] for c in bridged_channels if c['tg_channel_id']}

        regular_bridges = database.get_all_bridges()
        for b in regular_bridges:
            if len(b) > 1 and b[1]:
                bridged_ids.add(b[1])

        groups = []
        async for dialog in app_ctx.userbot_client.iter_dialogs():
            if dialog.is_group:
                if dialog.id not in bridged_ids:
                    groups.append(dialog)

        if not groups:
            await status_msg.edit_text("✅ No new groups found (all are already bridged or you aren't in any groups).")
            return

        lines = ["<b>Your Telegram Groups:</b>\n"]
        for g in groups:
            line = f"• <b>{html.escape(g.name)}</b> (ID: <code>{g.id}</code>)\n"
            line += f"  └ Command: <code>/channeladd {g.id}</code>\n"
            lines.append(line)

        full_text = "".join(lines)
        if len(full_text) > 4000:
            current_chunk = ""
            for line in lines:
                if len(current_chunk) + len(line) > 4000:
                    await update.effective_message.reply_text(current_chunk, parse_mode='HTML')
                    current_chunk = ""
                current_chunk += line
            await update.effective_message.reply_text(current_chunk, parse_mode='HTML')
            await status_msg.delete()
        else:
            await status_msg.edit_text(full_text, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Failed to fetch groups: {e}")
        await status_msg.edit_text(f"❌ Error fetching groups: {html.escape(str(e))}", parse_mode='HTML')


async def tg_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await permission_checker.check_channel_admin(update):
        return

    user = update.effective_user
    if database.is_owner(user.id):
        channels = database.get_all_channels()
    else:
        channels = database.get_channels_by_creator(user.id)

    if not channels:
        await update.effective_message.reply_text("📺 No channels are bridged.")
        return

    lines = [f"📺 <b>Bridged Channels</b> ({len(channels)})\n"]
    for ch in channels:
        dc_cid = ch['dc_chat_id']
        tg_id = ch.get('tg_channel_id', 0)
        r_count = ch.get('reactions_count', 0)
        m_count = database.get_bridge_message_count(dc_cid, tg_id)
        tg_sub_count = ch.get('tg_participants_count', 0)

        try:
            chat_info = app_ctx.dc_bot.rpc.get_basic_chat_info(app_ctx.dc_accid, dc_cid)
            title = chat_info.get("name", "Unknown Channel")
            contacts = app_ctx.dc_bot.rpc.get_chat_contacts(app_ctx.dc_accid, dc_cid)
            dc_sub_count = len(contacts) - 1 if contacts else 0
        except Exception:
            title = "Unknown Channel"
            dc_sub_count = "?"

        tg_ref = f"t.me/{ch['tg_channel_username']}" if ch['tg_channel_username'] else f"ID: {ch['tg_channel_id']}"
        stats_str = f"👤 {tg_sub_count:,} TG / {dc_sub_count} DC — 💬 {m_count}"
        lines.append(f"/channel{ch['id']} — <b>{html.escape(title)}</b> ({tg_ref}) — {stats_str}")
    lines.append(f"\nUse <code>/channel N</code> for invite link")
    await update.effective_message.reply_text("\n".join(lines), parse_mode='HTML')


async def tg_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await permission_checker.check_channel_admin(update):
        return

    channel_id = None
    cmd = update.effective_message.text.split()[0].lower()
    if len(cmd) > 8 and cmd.startswith("/channel"):
        try:
            channel_id = int(cmd[8:])
        except ValueError:
            pass

    if channel_id is None:
        if not context.args or len(context.args) < 1:
            await update.effective_message.reply_text("Usage: <code>/channel N</code> (use /channels to see numbers)", parse_mode='HTML')
            return
        try:
            channel_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("❌ Please provide a valid channel number.")
            return

    ch = database.get_channel_by_id(channel_id)
    if not ch:
        await update.effective_message.reply_text(f"❌ Channel #{channel_id} not found.")
        return

    user = update.effective_user
    if not database.is_owner(user.id):
        creator = database.get_channel_creator(channel_id)
        if creator != user.id:
            await update.effective_message.reply_text(f"❌ Channel #{channel_id} not found.")
            return

    invite_link = ch['invite_link'] or "No invite link available"
    if ch['tg_channel_username']:
        name_str = f"@{html.escape(ch['tg_channel_username'])}"
    else:
        name_str = f"ID <code>{ch['tg_channel_id']}</code>"

    await update.effective_message.reply_text(
        f"📺 Channel #{ch['id']} — <b>{name_str}</b>\n\n"
        f"🔗 Subscribe in Delta Chat:\n{html.escape(invite_link)}",
        parse_mode='HTML',
        disable_web_page_preview=True
    )


async def tg_channelqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await permission_checker.check_channel_admin(update):
        return

    if not qr_generator.is_qrcode_available():
        await update.effective_message.reply_text("❌ QR code generation is not supported. Please install 'qrcode[pil]' python package.")
        return

    channel_id = None
    cmd = update.effective_message.text.split()[0].lower()
    if "qr" in cmd:
        digits = re.findall(r'\d+', cmd)
        if digits:
            try:
                channel_id = int(digits[0])
            except ValueError:
                pass

    if channel_id is None:
        if not context.args or len(context.args) < 1:
            await update.effective_message.reply_text("Usage: <code>/channelqr N</code>", parse_mode='HTML')
            return
        try:
            channel_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("❌ Please provide a valid channel number.")
            return

    ch = database.get_channel_by_id(channel_id)
    if not ch:
        await update.effective_message.reply_text(f"❌ Channel #{channel_id} not found.")
        return

    user = update.effective_user
    if not database.is_owner(user.id):
        creator = database.get_channel_creator(channel_id)
        if creator != user.id:
            await update.effective_message.reply_text(f"❌ Channel #{channel_id} not found.")
            return

    if not ch['invite_link']:
        await update.effective_message.reply_text("❌ No invite link available for this channel.")
        return

    try:
        if app_ctx.dc_bot and app_ctx.dc_accid:
            qrdata = app_ctx.dc_bot.rpc.get_chat_securejoin_qr_code(app_ctx.dc_accid, ch['dc_chat_id'])
        else:
            qrdata = ch['invite_link']

        bio = qr_generator.generate_qr_image(qrdata)
        if not bio:
            await update.effective_message.reply_text("❌ Error generating QR code.")
            return

        if ch['tg_channel_username']:
            caption = f"Scan to subscribe to @{ch['tg_channel_username']} in Delta Chat"
        else:
            caption = f"Scan to subscribe to channel ID {ch['tg_channel_id']} in Delta Chat"

        await update.effective_message.reply_photo(photo=bio, caption=caption)
    except Exception as e:
        logger.error(f"Failed to generate channel QR: {e}")
        await update.effective_message.reply_text("❌ Error generating QR code.")


async def tg_channelremove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await permission_checker.check_channel_admin(update):
        return

    if not context.args or len(context.args) < 1:
        await update.effective_message.reply_text("Usage: <code>/channelremove N</code> (use /channels to see numbers)", parse_mode='HTML')
        return

    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Please provide a valid channel number.")
        return

    ch = database.get_channel_by_id(channel_id)
    if not ch:
        await update.effective_message.reply_text(f"❌ Channel #{channel_id} not found.")
        return

    user = update.effective_user
    if not database.is_owner(user.id):
        creator = database.get_channel_creator(channel_id)
        if creator != user.id:
            await update.effective_message.reply_text(f"❌ Channel #{channel_id} not found.")
            return

    channel_name = ch['tg_channel_username']
    if channel_name:
        display_name = f"@{html.escape(channel_name)}"
    else:
        display_name = f"ID <code>{ch['tg_channel_id']}</code>"

    if database.remove_channel(channel_id):
        await update.effective_message.reply_text(f"✅ Channel #{channel_id} (<b>{display_name}</b>) removed.", parse_mode='HTML')
    else:
        await update.effective_message.reply_text("❌ Failed to remove channel.")
