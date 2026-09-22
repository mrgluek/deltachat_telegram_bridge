"""All Telegram-side /command handlers: start/help/donate/id/stats/status,
bridge/unbridge, sub-admin management, invite links, channel add/list/
remove/QR, message filters, and manual cleanup.

References to the pervasive bot.py singletons and to functions that
either stay in bot.py (generate_status_report, update_tg_channel_stats,
_notify_and_remove_channel_bridge, cleanup_stale_bridges) or live in a
sibling module go through a function-local `import bot as _bot_module`,
same pattern as the rest of this refactor.

tg_channels_command now reads/writes the /channels report cache via
caching.get_cached_channels_text/set_cached_channels_text instead of
touching the _channels_cache dict directly.
"""
import asyncio
import html
import logging
import os
import re
import tempfile
import time
from typing import Optional

import database
from deltachat2 import MsgData
from telegram import Update
from telegram.ext import ContextTypes

try:
    import qrcode
except ImportError:
    qrcode = None

try:
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
except ImportError:
    JoinChannelRequest = None
    ImportChatInviteRequest = None
    CheckChatInviteRequest = None

from security import retry_async, _reload_filter_cache
from caching import (
    _clear_dc_caches,
    invalidate_channels_cache,
    get_cached_channels_text,
    set_cached_channels_text,
)
from formatting import get_tg_help_text
from dc_helpers import _userbot_leave_chat
from tg_events import _check_invite_permissions, _check_channel_admin, _add_channel_bridge, _send_bot_message
from userbot import run_channel_catchup, sync_userbot_channels

logger = logging.getLogger("tg_dc_bridge")


async def tg_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply to /start in private chat with help text and user ID."""
    user = update.effective_user
    name = html.escape(user.first_name)
    greeting = get_tg_help_text(name, user.id)
    await retry_async(update.message.reply_text, greeting, parse_mode='HTML', max_retries=3, delay=2.0)


async def tg_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply to /help with help text."""
    user = update.effective_user
    name = html.escape(user.first_name)
    help_msg = get_tg_help_text(name, user.id)
    await retry_async(update.message.reply_text, help_msg, parse_mode='HTML', max_retries=3, delay=2.0)


async def tg_donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply with donate link."""
    support_msg = (
        "❤️ <b>Support Bot Development</b>\n\n"
        "If you find this bridge useful, you can support its development and server costs here:\n\n"
        "🔗 <a href='https://t.me/tribute/app?startapp=dIWb'>Support via Tribute</a>\n\n"
        "Thank you! 🙏"
    )
    await update.message.reply_html(support_msg, disable_web_page_preview=True)


async def tg_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the Telegram chat ID. Only responds to group admins."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("❌ You must send that command in a Telegram group, not here.")
        return

    # Admin check for Telegram
    try:
        member = await chat.get_member(user.id)
        if member.status not in ("administrator", "creator"):
            await update.message.reply_text("❌ Only group admins can use /id.")
            return
    except Exception as e:
        logger.warning(f"Could not verify group admin permissions for /id: {e}")
        await update.message.reply_text("❌ Could not verify your group admin permissions.")
        return

    await update.message.reply_text(f"Group ID: <code>{chat.id}</code>", parse_mode='HTML')


async def tg_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bridge statistics on Telegram side."""
    import bot as _bot_module
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        # In private chat: owner sees all, sub-admin sees own, others denied
        admin_tg_id = database.get_config("admin_tg_id")
        if admin_tg_id:
            if database.is_owner(user.id):
                bridges = database.get_all_bridges()
            elif database.is_admin(user.id):
                bridges = database.get_bridges_by_creator(user.id)
            else:
                await update.message.reply_text("❌ Only the bot admin can view stats.")
                return
        else:
            bridges = database.get_all_bridges()

        if not bridges:
            await update.message.reply_text("📊 No bridges configured.")
            return

        lines = [f"📊 <b>Bridge Statistics</b> ({len(bridges)} bridge{'s' if len(bridges) != 1 else ''})\n"]
        for row in bridges:
            # row: (dc_cid, tg_cid, reactions_count)
            dc_cid, tg_cid, r_count = row if len(row) == 3 else (row[0], row[1], 0)
            m_count = database.get_bridge_message_count(dc_cid, tg_cid)
            try:
                chat_info = _bot_module.dc_bot_instance.rpc.get_basic_chat_info(_bot_module.dc_accid, dc_cid)
                title = chat_info.get("name", "Unknown Group")
                # Get member count (minus the bot itself)
                contacts = _bot_module.dc_bot_instance.rpc.get_chat_contacts(_bot_module.dc_accid, dc_cid)
                if contacts:
                    try:
                        self_id = _bot_module.dc_bot_instance.rpc.get_contact(_bot_module.dc_accid, 1).id
                    except Exception:
                        self_id = 1
                    sub_count = len(contacts) - 1 if self_id in contacts else len(contacts)
                else:
                    sub_count = 0
            except Exception:
                title = "Unknown Group"
                sub_count = "?"
            lines.append(f"• DC <code>{dc_cid}</code> ↔ TG <code>{tg_cid}</code> ({html.escape(title)}) — {sub_count} 👤 {m_count} 💬 {r_count} 🙂")
        
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    else:
        # In group chat: owner, sub-admin (if they created this bridge), or TG group admin
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
                        await update.message.reply_text("❌ Only group admins can view stats.")
                        return
                except Exception:
                    pass

        dc_chats = database.get_dc_chats(chat.id)
        if not dc_chats:
            await update.message.reply_text("📊 This group is not bridged.")
            return

        lines = ["📊 <b>Bridge Statistics</b>\n"]
        for dc_cid in dc_chats:
            m_count = database.get_bridge_message_count(dc_cid, chat.id)
            r_count = database.get_bridge_reaction_count(dc_cid, chat.id)
            try:
                title = _bot_module.dc_bot_instance.rpc.get_basic_chat_info(_bot_module.dc_accid, dc_cid).get("name", "this group")
            except Exception:
                title = "this group"
            lines.append(f"• DC <code>{dc_cid}</code> ({html.escape(title)}) — {m_count} 💬 {r_count} 🙂")
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')


async def tg_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed bot and userbot status on Telegram (admin only)."""
    import bot as _bot_module
    user = update.effective_user
    chat = update.effective_chat

    # Only allow in private chat, and only for owner or admin
    if chat.type != "private":
        await update.message.reply_text("❌ For security reasons, the /status command can only be used in a private chat.")
        return

    admin_tg_id = database.get_config("admin_tg_id")
    if admin_tg_id:
        if not database.is_owner_or_admin(user.id):
            await update.message.reply_text("❌ Only the bot admin can view status.")
            return

    try:
        report = await _bot_module.generate_status_report(is_html=True)
        await retry_async(update.message.reply_text, report, parse_mode='HTML', disable_web_page_preview=True, max_retries=3, delay=2.0)
    except Exception as e:
        logger.error(f"Error generating status report for TG: {e}")
        await retry_async(update.message.reply_text, f"❌ Error generating status report: {e}", max_retries=3, delay=2.0)


async def tg_bridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bridge a TG group to a new DC group. Auto-creates the DC group."""
    import bot as _bot_module
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("❌ You must send /bridge in a Telegram group, not here.")
        return

    # Permission check: owner, sub-admin, or (in public mode) TG group admin
    admin_tg_id = database.get_config("admin_tg_id")
    if admin_tg_id:
        if not database.is_owner_or_admin(user.id):
            await update.message.reply_text("❌ Only the bot admin can use /bridge.")
            return
    else:
        try:
            member = await chat.get_member(user.id)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text("❌ Only group admins can use /bridge.")
                return
        except Exception as e:
            logger.warning(f"Could not verify group admin permissions for /bridge: {e}")
            await update.message.reply_text("❌ Could not verify your group admin permissions.")
            return

    # Check if already bridged
    existing = database.get_dc_chats(chat.id)
    if existing:
        await update.message.reply_text("❌ This group is already bridged.")
        return

    if not _bot_module.dc_bot_instance or not _bot_module.dc_accid:
        await update.message.reply_text("❌ Delta Chat bot is not ready yet.")
        return

    tg_chat_id = chat.id
    tg_title = chat.title or f"TG Group {tg_chat_id}"

    await update.message.reply_text(f"⏳ Setting up bridge for <b>{html.escape(tg_title)}</b>...", parse_mode='HTML')

    try:
        # Create DC group with same name
        dc_chat_id = _bot_module.dc_bot_instance.rpc.create_group_chat(_bot_module.dc_accid, tg_title, False)

        # Copy TG group avatar to DC group
        try:
            tg_chat_info = await context.bot.get_chat(tg_chat_id)
            if tg_chat_info.photo:
                avatar_file = await tg_chat_info.photo.get_big_file()
                tmp_fd, avatar_path = tempfile.mkstemp(suffix=".jpg")
                os.close(tmp_fd)
                await avatar_file.download_to_drive(custom_path=avatar_path)
                _bot_module.dc_bot_instance.rpc.set_chat_profile_image(_bot_module.dc_accid, dc_chat_id, avatar_path)
                try:
                    os.unlink(avatar_path)
                except Exception:
                    pass
                logger.info(f"Copied avatar from TG group {tg_chat_id} to DC group {dc_chat_id}")
        except Exception as e:
            logger.warning(f"Could not copy group avatar: {e}")

        # Save bridge to DB
        database.add_bridge(dc_chat_id, tg_chat_id, created_by_tg_id=user.id)

        # Generate invite link
        invite_link = _bot_module.dc_bot_instance.rpc.get_chat_securejoin_qr_code(_bot_module.dc_accid, dc_chat_id)
        if invite_link.startswith("OPEN-CHAT:"):
            invite_link = "https://i.delta.chat/#" + invite_link[10:]
        elif invite_link.startswith("OPEN:"):
            invite_link = "https://i.delta.chat/#" + invite_link[5:]

        await update.message.reply_text(
            f"✅ Bridged! DC group <b>{html.escape(tg_title)}</b> created.\n\n"
            f"🔗 Join in Delta Chat:\n{html.escape(invite_link)}",
            parse_mode='HTML',
            disable_web_page_preview=True
        )
        logger.info(f"TG bridge: user {user.id} bridged TG {tg_chat_id} -> DC {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to create TG bridge: {e}")
        await update.message.reply_text("❌ Failed to create bridge. Please check logs for details.", parse_mode='HTML')


async def tg_unbridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove bridge for this TG group."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("❌ You must send /unbridge in a Telegram group, not here.")
        return

    # Permission check
    admin_tg_id = database.get_config("admin_tg_id")
    if admin_tg_id:
        if database.is_owner(user.id):
            pass  # owner can unbridge anything
        elif database.is_admin(user.id):
            creator = database.get_bridge_creator_by_tg(chat.id)
            if creator != user.id:
                await update.message.reply_text("❌ You can only unbridge groups you created.")
                return
        else:
            await update.message.reply_text("❌ Only the bot admin can use /unbridge.")
            return
    else:
        try:
            member = await chat.get_member(user.id)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text("❌ Only group admins can use /unbridge.")
                return
        except Exception as e:
            logger.warning(f"Could not verify group admin permissions for /unbridge: {e}")
            await update.message.reply_text("❌ Could not verify your group admin permissions.")
            return

    # Get dc_chat_ids before deletion to clear caches
    dc_chat_ids = database.get_dc_chats(chat.id)
    
    if database.remove_bridge_by_tg(chat.id):
        for dc_cid in dc_chat_ids:
            _clear_dc_caches(dc_cid)
        
        # Userbot leave
        asyncio.create_task(_userbot_leave_chat(chat.id))
        
        await update.message.reply_text("✔️ Bridge removed.")
    else:
        await update.message.reply_text("❌ This group is not bridged.")


async def tg_adminadd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a sub-admin. Owner only, private chat only."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "private":
        await update.message.reply_text("❌ This command can only be used in a private chat with the bot.")
        return

    if not database.is_owner(user.id):
        await update.message.reply_text("❌ Only the bot owner can manage admins.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: <code>/adminadd user_id</code>\n\n"
            "The user should send /start to the bot first to get their user ID.",
            parse_mode='HTML'
        )
        return

    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid numeric user ID.")
        return

    # Don't allow adding self
    if str(new_admin_id) == str(database.get_config("admin_tg_id")):
        await update.message.reply_text("❌ You are already the owner.")
        return

    if database.add_admin(new_admin_id):
        await update.message.reply_text(f"✅ User <code>{new_admin_id}</code> added as sub-admin.", parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ User <code>{new_admin_id}</code> is already a sub-admin.", parse_mode='HTML')


async def tg_adminremove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a sub-admin. Owner only, private chat only."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "private":
        await update.message.reply_text("❌ This command can only be used in a private chat with the bot.")
        return

    if not database.is_owner(user.id):
        await update.message.reply_text("❌ Only the bot owner can manage admins.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Usage: <code>/adminremove user_id</code>", parse_mode='HTML')
        return

    try:
        admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid numeric user ID.")
        return

    if database.remove_admin(admin_id):
        await update.message.reply_text(f"✅ User <code>{admin_id}</code> removed from sub-admins.", parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ User <code>{admin_id}</code> is not a sub-admin.", parse_mode='HTML')


async def tg_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all sub-admins. Owner only, private chat only."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "private":
        await update.message.reply_text("❌ This command can only be used in a private chat with the bot.")
        return

    if not database.is_owner(user.id):
        await update.message.reply_text("❌ Only the bot owner can view admins.")
        return

    admins = database.get_all_admins()
    if not admins:
        await update.message.reply_text("👥 No sub-admins configured.\n\nUse <code>/adminadd user_id</code> to add one.", parse_mode='HTML')
        return

    lines = [f"👥 <b>Sub-admins</b> ({len(admins)})\n"]
    for admin_id in admins:
        lines.append(f"• <code>{admin_id}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


async def tg_invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate invite link for the bridged Delta Chat group or the bot itself."""
    import bot as _bot_module
    if not await _check_invite_permissions(update):
        return

    chat = update.effective_chat
    
    if chat.type == "private":
        try:
            qrdata = _bot_module.dc_bot_instance.rpc.get_chat_securejoin_qr_code(_bot_module.dc_accid, None)
            if qrdata.startswith("OPEN-CHAT:"):
                qrdata = "https://i.delta.chat/#" + qrdata[10:]
            elif qrdata.startswith("OPEN:"):
                qrdata = "https://i.delta.chat/#" + qrdata[5:]
            await update.message.reply_text(f"🔗 <b>Bot Setup Link</b>\n{html.escape(qrdata)}", parse_mode='HTML', disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Failed to generate bot setup link: {e}")
            await update.message.reply_text("❌ Error generating bot setup link.")
        return

    dc_chats = database.get_dc_chats(chat.id)
    if not dc_chats:
        await update.message.reply_text("❌ This group is not bridged to any Delta Chat group.")
        return

    lines = []
    for dc_cid in dc_chats:
        try:
            chat_info = _bot_module.dc_bot_instance.rpc.get_basic_chat_info(_bot_module.dc_accid, dc_cid)
            chat_name = chat_info.get("name") or str(dc_cid)
            
            qrdata = _bot_module.dc_bot_instance.rpc.get_chat_securejoin_qr_code(_bot_module.dc_accid, dc_cid)
            # Make sure it's a clickable i.delta.chat link if using OPEN-CHAT/OPEN protocol
            if qrdata.startswith("OPEN-CHAT:"):
                qrdata = "https://i.delta.chat/#" + qrdata[10:]
            elif qrdata.startswith("OPEN:"):
                qrdata = "https://i.delta.chat/#" + qrdata[5:]
                
            lines.append(f"🔗 Click to join bridged DC Group <b>{html.escape(chat_name)}</b>:\n{html.escape(qrdata)}\n")
        except Exception as e:
            logger.error(f"Failed to generate invite link for DC chat {dc_cid}: {e}")
            lines.append(f"❌ Error generating link for DC Group {dc_cid}.\n")

    await update.message.reply_text("\n".join(lines).strip(), parse_mode='HTML', disable_web_page_preview=True)


async def tg_inviteqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate invite QR code for the bridged Delta Chat group or the bot itself."""
    import bot as _bot_module
    if not await _check_invite_permissions(update):
        return

    chat = update.effective_chat

    if not qrcode:
        await update.message.reply_text("❌ QR code generation is not supported. Please install 'qrcode[pil]' python package.")
        return

    if chat.type == "private":
        try:
            qrdata = _bot_module.dc_bot_instance.rpc.get_chat_securejoin_qr_code(_bot_module.dc_accid, None)
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(qrdata)
            qr.make(fit=True)
            
            try:
                img = qr.make_image(fill_color="black", back_color="white")
                import io
                bio = io.BytesIO()
                bio.name = 'bot_invite_qr.png'
                img.save(bio, 'PNG')
                bio.seek(0)
                await update.message.reply_photo(
                    photo=bio, 
                    caption="Scan to add bot in Delta Chat"
                )
            except Exception as e:
                logger.error(f"Image generation failed (Pillow might be missing): {e}")
                await update.message.reply_text("❌ Cannot generate image. Ensure 'Pillow' is installed (`pip install Pillow`).")
        except Exception as e:
            logger.error(f"Failed to generate bot setup QR: {e}")
            await update.message.reply_text("❌ Error generating QR code.")
        return

    dc_chats = database.get_dc_chats(chat.id)
    if not dc_chats:
        return

    for dc_cid in dc_chats:
        try:
            chat_info = _bot_module.dc_bot_instance.rpc.get_basic_chat_info(_bot_module.dc_accid, dc_cid)
            chat_name = chat_info.get("name") or str(dc_cid)
            
            qrdata = _bot_module.dc_bot_instance.rpc.get_chat_securejoin_qr_code(_bot_module.dc_accid, dc_cid)
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(qrdata)
            qr.make(fit=True)
            
            try:
                img = qr.make_image(fill_color="black", back_color="white")
                import io
                bio = io.BytesIO()
                bio.name = 'invite_qr.png'
                img.save(bio, 'PNG')
                bio.seek(0)
                await update.message.reply_photo(
                    photo=bio,
                    caption=f"Scan to join bridged DC Group {chat_name}"
                )
            except Exception as e:
                logger.error(f"Image generation failed (Pillow might be missing): {e}")
                await update.message.reply_text("❌ Cannot generate image. Ensure 'Pillow' is installed (`pip install Pillow`).")
                
        except Exception as e:
            logger.error(f"Failed to generate invite QR for DC chat {dc_cid}: {e}")
            await update.message.reply_text(f"❌ Error generating QR code for DC Group {dc_cid}.", parse_mode='HTML')


async def tg_botsend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a command or message to a Telegram bot via Userbot (Telegram)."""
    user_id = update.effective_user.id
    if not database.is_owner(user_id):
        await update.message.reply_text("❌ Only the bot owner can use /botsend.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: <code>/botsend @bot_name &lt;message&gt;</code>\n"
            "or: <code>/botsend &lt;channel_id&gt; &lt;message&gt;</code>\n\n"
            "Example: <code>/botsend @weather_bot /today</code>",
            parse_mode='HTML'
        )
        return

    target = context.args[0]
    cmd_text = " ".join(context.args[1:])
    status_msg = await update.message.reply_text(f"⏳ Sending command to {html.escape(target)}...")
    res = await _send_bot_message(target, cmd_text)
    await status_msg.edit_text(res, parse_mode='HTML')


async def tg_channeladd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bridge a Telegram channel to a new DC broadcast channel."""
    if not await _check_channel_admin(update):
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: <code>/channeladd @name</code>\n"
            "or: <code>/channeladd https://t.me/name</code>\n"
            "or: <code>/channeladd -1001234567890</code>\n\n"
            "Example: <code>/channeladd @ia_panorama</code>\n"
            "Supports both <b>channels</b> and <b>groups</b> in read-only mode.",
            parse_mode='HTML'
        )
        return

    raw_arg = context.args[0].strip()
    status_msg = await update.message.reply_text("⏳ Processing bridge request...")
    
    result = await _add_channel_bridge(raw_arg, creator_tg_id=update.effective_user.id)
    invalidate_channels_cache()
    await status_msg.edit_text(result, parse_mode='HTML', disable_web_page_preview=True)


async def tg_catchup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch up missed channel posts (owner/admin only)."""
    if not database.is_owner(update.effective_user.id):
        return

    target = context.args[0].strip() if context.args else None
    target_desc = f" for <code>{html.escape(target)}</code>" if target else ""
    status_msg = await update.message.reply_text(f"⏳ Checking channels and catching up missed posts{target_desc}...", parse_mode='HTML')
    try:
        res_text = await run_channel_catchup(target, is_html=True)
        await status_msg.edit_text(res_text, parse_mode='HTML', disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Error during TG /catchup: {e}")
        await status_msg.edit_text("❌ Catchup failed. Please check logs for details.", parse_mode='HTML')


async def tg_userbotsync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger a Userbot subscription sync."""
    import bot as _bot_module
    if not database.is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Only the bot owner can use /userbotsync.")
        return
    
    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
        await update.message.reply_text("❌ Userbot is not connected.")
        return
    
    await update.message.reply_text("⏳ Starting Userbot synchronization...")
    async def _sync_and_notify():
        try:
            await sync_userbot_channels(force=True)
            await update.message.reply_text("✅ Userbot channel synchronization completed.")
        except Exception as e:
            logger.error(f"Userbot synchronization failed: {e}")
            await update.message.reply_text("❌ Userbot synchronization failed. Please check logs.")
    asyncio.create_task(_sync_and_notify())


async def tg_userbotjoin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Join a channel/group via Userbot using an invite link. Owner only."""
    import bot as _bot_module
    if not database.is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Only the bot owner can use /userbotjoin.")
        return

    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
        await update.message.reply_text("❌ Userbot is not connected.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/userbotjoin &lt;invite_link_or_username&gt;</code>\n\n"
            "Examples:\n"
            "• <code>/userbotjoin https://t.me/+AbCdEfGhIjK</code>\n"
            "• <code>/userbotjoin https://t.me/channelname</code>\n"
            "• <code>/userbotjoin @channelname</code>",
            parse_mode='HTML'
        )
        return

    link = context.args[0].strip()
    status_msg = await update.message.reply_text(f"⏳ Attempting to join via Userbot: <code>{html.escape(link)}</code>...", parse_mode='HTML')

    try:
        joined_entity = None
        joined_title = "Unknown"

        # Check if it's a private invite link (t.me/+HASH or t.me/joinchat/HASH)
        import re
        invite_hash = None
        m = re.search(r't\.me/\+([a-zA-Z0-9_-]+)', link)
        if m:
            invite_hash = m.group(1)
        else:
            m = re.search(r't\.me/joinchat/([a-zA-Z0-9_-]+)', link)
            if m:
                invite_hash = m.group(1)

        if invite_hash:
            # Private invite link — use ImportChatInviteRequest
            if not ImportChatInviteRequest:
                await status_msg.edit_text("❌ Telethon is not properly installed (ImportChatInviteRequest missing).")
                return

            # First check what we're joining
            try:
                check = await asyncio.wait_for(_bot_module.userbot_client(CheckChatInviteRequest(invite_hash)), timeout=15.0)
                if hasattr(check, 'chat'):
                    # Already a member
                    joined_entity = check.chat
                    joined_title = getattr(joined_entity, 'title', 'Unknown')
                    await status_msg.edit_text(
                        f"✅ Userbot is already a member of <b>{html.escape(joined_title)}</b>.",
                        parse_mode='HTML'
                    )
                else:
                    # Not a member yet — join
                    result = await asyncio.wait_for(_bot_module.userbot_client(ImportChatInviteRequest(invite_hash)), timeout=15.0)
                    if hasattr(result, 'chats') and result.chats:
                        joined_entity = result.chats[0]
                        joined_title = getattr(joined_entity, 'title', 'Unknown')
            except Exception as e:
                if "already" in str(e).lower() or "USER_ALREADY_PARTICIPANT" in str(e):
                    # Try to resolve via the link to get the entity
                    try:
                        joined_entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(link), timeout=15.0)
                        joined_title = getattr(joined_entity, 'title', 'Unknown')
                    except Exception:
                        pass
                    await status_msg.edit_text(
                        f"✅ Userbot is already a member." + (f" ({html.escape(joined_title)})" if joined_title != "Unknown" else ""),
                        parse_mode='HTML'
                    )
                    # Still try to update DB below
                else:
                    raise
        else:
            # Public link or @username — use get_entity + JoinChannelRequest
            target = link
            if not target.startswith('@') and 't.me/' in target:
                # Extract username from t.me/username
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
            # Telethon uses positive IDs; convert to Bot API format
            tg_channel_id = int(f"-100{joined_id}") if joined_id and joined_id > 0 else joined_id

            # Try to match this to an existing channel in DB and update its invite_link
            matched_channel = None
            if tg_channel_id:
                matched_channel = database.get_channel_by_tg_id(tg_channel_id)
            if not matched_channel:
                username = getattr(joined_entity, 'username', None)
                if username:
                    matched_channel = database.get_channel_by_tg_username(username)

            if matched_channel:
                # Save the invite link for future syncs
                database.update_channel_invite_link(matched_channel['id'], link)
                # Also update tg_channel_id if it was missing
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

            # Update stats
            try:
                chan_id = matched_channel['id'] if matched_channel else None
                if chan_id:
                    await _bot_module.update_tg_channel_stats(chan_id, joined_entity)
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
    """List all groups the Userbot is in that aren't bridged yet (Owner only)."""
    import bot as _bot_module
    if not database.is_owner(update.effective_user.id):
        return
    
    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
        await update.message.reply_text("❌ Userbot is not connected.")
        return
    
    status_msg = await update.message.reply_text("⏳ Fetching your groups from Telegram...")
    
    try:
        # Get already bridged IDs from BOTH channels and regular bridges
        bridged_channels = database.get_all_channels()
        bridged_ids = {c['tg_channel_id'] for c in bridged_channels if c['tg_channel_id']}
        
        regular_bridges = database.get_all_bridges()
        for b in regular_bridges:
            if len(b) > 1 and b[1]:  # b[1] is tg_chat_id
                bridged_ids.add(b[1])
        
        # Get dialogs via Userbot
        groups = []
        async for dialog in _bot_module.userbot_client.iter_dialogs():
            if dialog.is_group:
                if dialog.id not in bridged_ids:
                    groups.append(dialog)
        
        if not groups:
            await status_msg.edit_text("✅ No new groups found (all are already bridged or you aren't in any groups).")
            return
        
        # Format response
        lines = ["<b>Your Telegram Groups:</b>\n"]
        for g in groups:
            line = f"• <b>{html.escape(g.name)}</b> (ID: <code>{g.id}</code>)\n"
            line += f"  └ Command: <code>/channeladd {g.id}</code>\n"
            lines.append(line)
        
        # Split into chunks if too long (Telegram limit ~4096 chars)
        full_text = "".join(lines)
        if len(full_text) > 4000:
            # Simple split by line
            current_chunk = ""
            for line in lines:
                if len(current_chunk) + len(line) > 4000:
                    await update.message.reply_text(current_chunk, parse_mode='HTML')
                    current_chunk = ""
                current_chunk += line
            await update.message.reply_text(current_chunk, parse_mode='HTML')
            await status_msg.delete()
        else:
            await status_msg.edit_text(full_text, parse_mode='HTML')
            
    except Exception as e:
        logger.error(f"Failed to fetch groups: {e}")
        await status_msg.edit_text(f"❌ Error fetching groups: {html.escape(str(e))}", parse_mode='HTML')


async def tg_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List bridged channels (owner sees all, sub-admin sees own)."""
    import bot as _bot_module
    if not await _check_channel_admin(update):
        return

    user = update.effective_user

    # Serve from 10-minute cache if available and fresh
    cached_text = get_cached_channels_text(user.id)
    if cached_text is not None:
        await retry_async(update.message.reply_text, cached_text, parse_mode='HTML', max_retries=3, delay=2.0)
        return

    if database.is_owner(user.id):
        channels = database.get_all_channels()
    else:
        channels = database.get_channels_by_creator(user.id)

    if not channels:
        await retry_async(update.message.reply_text, "📺 No channels are bridged.", max_retries=3, delay=2.0)
        return

    loop = asyncio.get_running_loop()

    def _build_channels_text():
        lines = [f"📺 <b>Bridged Channels</b> ({len(channels)})\n"]
        for ch in channels:
            dc_cid = ch['dc_chat_id']
            tg_id = ch.get('tg_channel_id', 0)
            m_count = database.get_bridge_message_count(dc_cid, tg_id)
            tg_sub_count = ch.get('tg_participants_count', 0)
            title = ch.get('title') or "Unknown Channel"
            dc_sub_count = "?"
            
            if _bot_module.dc_bot_instance and _bot_module.dc_accid:
                try:
                    chat_info = _bot_module.dc_bot_instance.rpc.get_basic_chat_info(_bot_module.dc_accid, dc_cid)
                    if chat_info and chat_info.get("name"):
                        title = chat_info.get("name")
                    contacts = _bot_module.dc_bot_instance.rpc.get_chat_contacts(_bot_module.dc_accid, dc_cid)
                    if contacts:
                        try:
                            self_id = _bot_module.dc_bot_instance.rpc.get_contact(_bot_module.dc_accid, 1).id
                        except Exception:
                            self_id = 1
                        dc_sub_count = len(contacts) - 1 if self_id in contacts else len(contacts)
                    else:
                        dc_sub_count = 0
                except Exception:
                    pass

            disp_title = (title[:37] + "...") if len(title) > 40 else title
            if ch.get('tg_channel_username'):
                title_display = f'<a href="https://t.me/{ch["tg_channel_username"]}">{html.escape(disp_title)}</a>'
            else:
                title_display = f"<b>{html.escape(disp_title)}</b> (ID: {ch['tg_channel_id']})"
            stats_str = f"👤 {tg_sub_count:,} TG / {dc_sub_count} DC — 💬 {m_count}"
            lines.append(f"/channel{ch['id']} — {title_display} — {stats_str}")
        lines.append(f"\nUse <code>/channel N</code> for invite link")
        return "\n".join(lines)

    text_to_send = await loop.run_in_executor(None, _build_channels_text)
    set_cached_channels_text(user.id, text_to_send)
    await retry_async(update.message.reply_text, text_to_send, parse_mode='HTML', max_retries=3, delay=2.0)


async def tg_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show invite link for a specific channel by number."""
    if not await _check_channel_admin(update):
        return

    channel_id = None
    # Support both "/channel 1" and "/channel1"
    cmd = update.message.text.split()[0].lower()
    if len(cmd) > 8 and cmd.startswith("/channel"):
        try:
            channel_id = int(cmd[8:])
        except ValueError:
            pass
            
    if channel_id is None:
        if not context.args or len(context.args) < 1:
            await update.message.reply_text("Usage: <code>/channel N</code> (use /channels to see numbers)", parse_mode='HTML')
            return
        try:
            channel_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Please provide a valid channel number.")
            return

    ch = database.get_channel_by_id(channel_id)
    if not ch:
        await update.message.reply_text(f"❌ Channel #{channel_id} not found.")
        return

    # Sub-admin can only view own channels
    user = update.effective_user
    if not database.is_owner(user.id):
        creator = database.get_channel_creator(channel_id)
        if creator != user.id:
            await update.message.reply_text(f"❌ Channel #{channel_id} not found.")
            return

    invite_link = ch['invite_link'] or "No invite link available"
    if ch['tg_channel_username']:
        name_str = f"@{html.escape(ch['tg_channel_username'])}"
    else:
        name_str = f"ID <code>{ch['tg_channel_id']}</code>"
        
    await update.message.reply_text(
        f"📺 Channel #{ch['id']} — <b>{name_str}</b>\n\n"
        f"🔗 Subscribe in Delta Chat:\n{html.escape(invite_link)}",
        parse_mode='HTML',
        disable_web_page_preview=True
    )


async def tg_channelqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show QR invite for a specific channel by number."""
    import bot as _bot_module
    if not await _check_channel_admin(update):
        return

    if not qrcode:
        await update.message.reply_text("❌ QR code generation is not supported. Please install 'qrcode[pil]' python package.")
        return


    channel_id = None
    # Support both "/channelqr 1" and "/channel1qr" and "/channelqr1"
    cmd = update.message.text.split()[0].lower()
    if "qr" in cmd:
        # Extract digits
        import re
        digits = re.findall(r'\d+', cmd)
        if digits:
            try:
                channel_id = int(digits[0])
            except ValueError:
                pass
            
    if channel_id is None:
        if not context.args or len(context.args) < 1:
            await update.message.reply_text("Usage: <code>/channelqr N</code>", parse_mode='HTML')
            return
        try:
            channel_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Please provide a valid channel number.")
            return

    ch = database.get_channel_by_id(channel_id)
    if not ch:
        await update.message.reply_text(f"❌ Channel #{channel_id} not found.")
        return

    # Sub-admin can only view own channels
    user = update.effective_user
    if not database.is_owner(user.id):
        creator = database.get_channel_creator(channel_id)
        if creator != user.id:
            await update.message.reply_text(f"❌ Channel #{channel_id} not found.")
            return

    if not ch['invite_link']:
        await update.message.reply_text("❌ No invite link available for this channel.")
        return

    try:
        # Get the original QR data (securejoin format, not the https link)
        if _bot_module.dc_bot_instance and _bot_module.dc_accid:
            qrdata = _bot_module.dc_bot_instance.rpc.get_chat_securejoin_qr_code(_bot_module.dc_accid, ch['dc_chat_id'])
        else:
            qrdata = ch['invite_link']

        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(qrdata)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        bio = io.BytesIO()
        bio.name = 'channel_invite_qr.png'
        img.save(bio, 'PNG')
        bio.seek(0)
        
        if ch['tg_channel_username']:
            caption = f"Scan to subscribe to @{ch['tg_channel_username']} in Delta Chat"
        else:
            caption = f"Scan to subscribe to channel ID {ch['tg_channel_id']} in Delta Chat"
            
        await update.message.reply_photo(
            photo=bio,
            caption=caption
        )
    except Exception as e:
        logger.error(f"Failed to generate channel QR: {e}")
        await update.message.reply_text("❌ Error generating QR code.")


async def tg_channelremove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a channel bridge."""
    import bot as _bot_module
    if not await _check_channel_admin(update):
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Usage: <code>/channelremove N</code> (use /channels to see numbers)", parse_mode='HTML')
        return

    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid channel number.")
        return

    ch = database.get_channel_by_id(channel_id)
    if not ch:
        await update.message.reply_text(f"❌ Channel #{channel_id} not found.")
        return

    # Sub-admin can only remove own channels
    user = update.effective_user
    if not database.is_owner(user.id):
        creator = database.get_channel_creator(channel_id)
        if creator != user.id:
            await update.message.reply_text(f"❌ Channel #{channel_id} not found.")
            return

    channel_name = ch['tg_channel_username']
    if channel_name:
        display_name = f"@{html.escape(channel_name)}"
    else:
        display_name = f"ID <code>{ch['tg_channel_id']}</code>"

    if _bot_module._notify_and_remove_channel_bridge(ch):
        await update.message.reply_text(f"✅ Channel #{channel_id} (<b>{display_name}</b>) removed.", parse_mode='HTML')
    else:
        await update.message.reply_text("❌ Failed to remove channel.")


async def tg_filters_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all active message filters."""
    if not await _check_channel_admin(update):
        return

    filters = database.get_all_filters()
    if not filters:
        await update.message.reply_text(
            "📋 <b>Message Filters</b>\nNo message filters configured.\n\nTo add a filter:\n<code>/filteradd &lt;word or phrase&gt;</code>",
            parse_mode='HTML'
        )
        return

    lines = [f"📋 <b>Message Filters ({len(filters)})</b>:"]
    for f in filters:
        lines.append(f"{f['id']}. <code>{html.escape(f['pattern'])}</code>")
    lines.append("\nTo add: <code>/filteradd &lt;word or phrase&gt;</code>")
    lines.append("To remove: <code>/filterdel &lt;number or phrase&gt;</code>")
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


async def tg_filteradd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a message filter."""
    if not await _check_channel_admin(update):
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: <code>/filteradd &lt;word or phrase&gt;</code>\nExample: <code>/filteradd #реклама</code>",
            parse_mode='HTML'
        )
        return

    phrase = " ".join(context.args).strip()
    row_id = database.add_filter(phrase)
    clean = phrase.strip().strip('"\'')
    if row_id is not None:
        _reload_filter_cache()
        await update.message.reply_text(f"✅ Filter added (#{row_id}): <code>{html.escape(clean)}</code>", parse_mode='HTML')
    else:
        await update.message.reply_text(f"⚠️ Filter <code>{html.escape(clean)}</code> already exists or is invalid.", parse_mode='HTML')


async def tg_filterdel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a message filter."""
    if not await _check_channel_admin(update):
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: <code>/filterdel &lt;number or phrase&gt;</code>\nExample: <code>/filterdel 1</code> or <code>/filterdel #реклама</code>",
            parse_mode='HTML'
        )
        return

    target = " ".join(context.args).strip()
    success, deleted_pattern = database.remove_filter(target)
    if success:
        _reload_filter_cache()
        await update.message.reply_text(f"✅ Filter <code>{html.escape(deleted_pattern)}</code> removed.", parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ Filter <code>{html.escape(target)}</code> not found.", parse_mode='HTML')


async def tg_cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger manual cleanup of stale/orphaned/duplicate bridges (Owner only)."""
    import bot as _bot_module
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "private":
        await update.message.reply_text("❌ This command can only be used in a private chat with the bot.")
        return

    if not database.is_owner(user.id):
        await update.message.reply_text("❌ Only the bot owner can use /cleanup.")
        return

    status_msg = await update.message.reply_text("⏳ Running cleanup of stale and duplicate bridges...")
    try:
        stats = await _bot_module.cleanup_stale_bridges()
        total_removed = (
            stats['orphaned_bridges_removed']
            + stats['duplicate_bridges_removed']
            + stats['dead_bridges_removed']
            + stats['orphaned_channels_removed']
        )
        res_lines = [
            "🧹 <b>Cleanup Complete</b>\n",
            f"• Orphaned bridges removed from DB: {stats['orphaned_bridges_removed']}",
            f"• Duplicate bridges removed: {stats['duplicate_bridges_removed']}",
            f"• Dead ghost bridges removed: {stats['dead_bridges_removed']}",
            f"• Orphaned channels removed: {stats['orphaned_channels_removed']}",
            f"• Empty DC chats deleted: {stats['dc_chats_deleted']}",
            f"\n<b>Total items cleaned:</b> {total_removed}"
        ]
        await status_msg.edit_text("\n".join(res_lines), parse_mode='HTML')
    except Exception as e:
        logger.error(f"Manual cleanup failed: {e}")
        await status_msg.edit_text(f"❌ Cleanup failed: {html.escape(str(e))}", parse_mode='HTML')

