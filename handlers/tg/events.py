import html
import logging

from telegram import Update, ReactionTypeEmoji
from telegram.ext import ContextTypes

import database
from app_context import app_ctx

logger = logging.getLogger("tg_dc_bridge")


async def handle_tg_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not app_ctx.dc_bot or not app_ctx.dc_accid:
        return

    reaction_update = update.message_reaction
    if not reaction_update:
        return

    if reaction_update.user and reaction_update.user.id == context.bot.id:
        return

    tg_chat_id = reaction_update.chat.id
    tg_msg_id = reaction_update.message_id
    logger.info(f"TG Reaction update in {tg_chat_id} msg {tg_msg_id} from user {reaction_update.user.id if reaction_update.user else 'unknown'}")

    dc_chats = database.get_dc_chats(tg_chat_id)
    if not dc_chats:
        chan_dc_id = database.get_dc_channel_chat_id(tg_chat_id)
        if chan_dc_id:
            dc_chats = [chan_dc_id]
        else:
            return

    primary_emoji = None
    if reaction_update.new_reaction:
        for r in reaction_update.new_reaction:
            if isinstance(r, ReactionTypeEmoji):
                primary_emoji = r.emoji
                break

    for dc_chat_id in dc_chats:
        dc_msg_id = database.get_dc_msg_id(tg_msg_id, tg_chat_id, dc_chat_id)
        if dc_msg_id:
            try:
                emoji_list = [primary_emoji] if primary_emoji else []
                app_ctx.dc_bot.rpc.send_reaction(app_ctx.dc_accid, dc_msg_id, emoji_list)
                if primary_emoji:
                    if database.get_dc_channel_chat_id(tg_chat_id):
                        database.increment_channel_reaction_count(tg_chat_id)
                    else:
                        database.increment_bridge_reaction_count(dc_chat_id, tg_chat_id)
                logger.info(f"Relayed TG reaction '{primary_emoji or '(cleared)'}' to DC chat {dc_chat_id}")
            except Exception as e:
                logger.error(f"Failed to relay TG reaction to DC: {e}")


async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member_update = update.my_chat_member
    if not member_update:
        return

    chat = member_update.chat
    new_status = member_update.new_chat_member.status if member_update.new_chat_member else None

    if chat.type != "channel" or new_status != "administrator":
        return

    admin_tg_id = database.get_config("admin_tg_id")
    if not admin_tg_id:
        return

    channel_title = chat.title or "Unknown"
    channel_id = chat.id
    channel_username = chat.username

    already = database.get_channel_by_tg_id(channel_id)
    if already:
        return

    if channel_username:
        msg_text = (
            f"📺 I was added as admin to channel <b>{html.escape(channel_title)}</b> "
            f"(@{html.escape(channel_username)}, ID: <code>{channel_id}</code>).\n\n"
            f"To bridge it:\n"
            f"<code>/channeladd @{html.escape(channel_username)}</code>\n"
            f"or\n"
            f"<code>/channeladd {channel_id}</code>"
        )
    else:
        msg_text = (
            f"📺 I was added as admin to private channel <b>{html.escape(channel_title)}</b> "
            f"(ID: <code>{channel_id}</code>).\n\n"
            f"To bridge it:\n"
            f"<code>/channeladd {channel_id}</code>"
        )

    try:
        await context.bot.send_message(chat_id=int(admin_tg_id), text=msg_text, parse_mode='HTML')
        logger.info(f"Notified owner about new channel: {channel_title} ({channel_id})")
    except Exception as e:
        logger.error(f"Failed to notify owner about channel {channel_id}: {e}")


async def handle_tg_migration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    old_id = msg.chat.id
    new_id = msg.migrate_to_chat_id
    if not new_id:
        old_id = msg.migrate_from_chat_id
        new_id = msg.chat.id
        if not old_id:
            return

    logger.info(f"TG group migrated: {old_id} -> {new_id}")
    database.update_bridge_tg_chat_id(old_id, new_id)


async def tg_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc = context.error
    exc_str = str(exc)
    exc_type = type(exc).__name__

    from telegram.error import ChatMigrated
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

    network_errors = ("ReadTimeout", "ConnectTimeout", "ProxyError", "NetworkError", "RemoteProtocolError")
    is_network = any(err in exc_str or err in exc_type for err in network_errors) or "Server disconnected" in exc_str

    if is_network:
        logger.warning(f"Telegram network error: {exc_type}: {exc_str}")
        return

    logger.error(f"Telegram Application Error [{exc_type}]: {exc_str}", exc_info=exc)
