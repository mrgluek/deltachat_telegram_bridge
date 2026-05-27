import asyncio
import html
import io
import logging
import os

from deltachat2 import EventType, events

import database
from app_context import app_ctx
from constants import DELETE_SYNC_MAX, DELETE_SYNC_WINDOW
from channel_history import channel_history_service
from edit_sync import edit_sync_service
from rate_limiter import rate_limiter
from relay import message_relay

logger = logging.getLogger("tg_dc_bridge")


@app_ctx.dc_cli.on_init
def on_init(bot, args):
    bot.logger.info("Initializing Delta Chat tgbridge...")

    for accid in bot.rpc.get_all_account_ids():
        displayname = database.get_config("bot_displayname") or "TG Bridge"
        bot.rpc.set_config(accid, "displayname", displayname)
        bot.rpc.set_config(accid, "selfstatus", "I bridge Telegram and Delta Chat groups. Send /help for commands.")
        bot.rpc.set_config(accid, "delete_device_after", "604800")
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


@app_ctx.dc_cli.on_start
def on_start(bot, _args):
    app_ctx.dc_bot = bot
    accounts = bot.rpc.get_all_account_ids()
    if accounts:
        app_ctx.dc_accid = accounts[0]

        admin_dc_email = database.get_config("admin_dc_email")
        admin_dc_fingerprint = database.get_config("admin_dc_fingerprint")
        if admin_dc_email:
            fp_suffix = f" ({admin_dc_fingerprint[-8:].upper()})" if admin_dc_fingerprint else ""
            print(f"Bot Administrator: {admin_dc_email}{fp_suffix}")

        try:
            transports = bot.rpc.list_transports(app_ctx.dc_accid)
            print("\n" + "=" * 50)
            print("Configured Bot Transports (Relays):")
            for t in transports:
                addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
                print(f" - {addr}")
        except Exception:
            pass

        try:
            app_ctx.bot_contact_id = bot.rpc.get_contact(app_ctx.dc_accid, 1).id
        except Exception:
            try:
                app_ctx.bot_contact_id = 1
            except Exception:
                pass

        try:
            qrdata = bot.rpc.get_chat_securejoin_qr_code(app_ctx.dc_accid, None)
            print("\n" + "=" * 50)
            print("To add this bot to a Delta Chat group, scan the QR code")
            print("or copy the link below:\n")

            try:
                import qrcode
                qr = qrcode.QRCode(version=1, box_size=1, border=2)
                qr.add_data(qrdata)
                qr.make(fit=True)
                f = io.StringIO()
                qr.print_ascii(out=f)
                print(f.getvalue())
            except ImportError:
                print("(Install 'qrcode' package to see the ASCII QR code here)")

            print(qrdata)
            print("\n" + "=" * 50 + "\n")
        except Exception as e:
            bot.logger.error(f"Failed to generate QR code: {e}")


@app_ctx.dc_cli.on(events.RawEvent(EventType.MSG_DELETED))
def handle_dc_msg_deleted(bot, accid, event):
    if not app_ctx.tg_app or not app_ctx.main_loop:
        return

    try:
        msg_id = getattr(event, 'msg_id', None)
        if not msg_id:
            return

        if edit_sync_service.consume_bot_delete(msg_id):
            database.delete_message_map_entry_by_dc(msg_id, None)
            return

        tg_mappings = database.get_tg_mappings_by_dc_msg_id(msg_id)
        if not tg_mappings:
            return

        for tg_msg_id, tg_chat_id, dc_chat_id in tg_mappings:
            database.delete_message_map_entry_by_dc(msg_id, None)

            if database.get_channel_by_dc_chat_id(dc_chat_id):
                continue

            dc_chan = database.get_dc_channel_by_dc_chat_id(dc_chat_id)
            if dc_chan and dc_chan.get('tg_channel_id') == tg_chat_id:
                asyncio.run_coroutine_threadsafe(
                    message_relay.delete_tg_message(tg_chat_id, tg_msg_id,
                                                     f" (DC→TG channel '{dc_chan.get('dc_chat_name', dc_chat_id)}')"),
                    app_ctx.main_loop
                )
                continue

            active_tg_chats = database.get_tg_chats(dc_chat_id)
            if tg_chat_id not in active_tg_chats:
                logger.info(f"Skipping DC→TG deletion sync: bridge for DC chat {dc_chat_id} to TG {tg_chat_id} no longer exists.")
                continue

            chat_title = f"Chat {dc_chat_id}"
            extra_info = ""
            try:
                chat_info = bot.rpc.get_basic_chat_info(accid, dc_chat_id)
                if chat_info and chat_info.get('name'):
                    chat_title = chat_info['name']
            except Exception:
                pass

            try:
                msg_obj = bot.rpc.get_message(accid, msg_id)
                if msg_obj:
                    author = getattr(msg_obj, 'from_id', 'Unknown')
                    text = getattr(msg_obj, 'text', '')
                    if text:
                        extra_info = f" | Author: {author} | Text: '{text[:50] + '…' if len(text) > 50 else text}'"
                    else:
                        extra_info = f" | Author: {author} (Media/Other)"
            except Exception:
                pass

            if rate_limiter.check_deletion_sync():
                logger.warning(
                    f"DC→TG deletion sync rate limit hit ({DELETE_SYNC_MAX}/{DELETE_SYNC_WINDOW}s). "
                    f"Skipping deletion of TG msg {tg_msg_id} in {tg_chat_id} (DC: '{chat_title}', msg {msg_id}{extra_info})."
                )
                admin_tg_id = database.get_config("admin_tg_id")
                if admin_tg_id and app_ctx.tg_app and app_ctx.main_loop:
                    asyncio.run_coroutine_threadsafe(
                        app_ctx.tg_app.bot.send_message(
                            chat_id=int(admin_tg_id),
                            text=(
                                f"⚠️ <b>Bulk deletion blocked</b>\n"
                                f"More than {DELETE_SYNC_MAX} messages deleted in {DELETE_SYNC_WINDOW}s.\n"
                                f"<b>Chat:</b> {html.escape(chat_title)}\n"
                                f"<b>Message Info:</b> {html.escape(extra_info.strip(' | ') or 'Not available')}\n"
                                f"DC msg {msg_id} → TG msg {tg_msg_id} in chat {tg_chat_id} was <b>NOT</b> deleted on Telegram."
                            ),
                            parse_mode='HTML'
                        ),
                        app_ctx.main_loop
                    )
                return

            asyncio.run_coroutine_threadsafe(
                message_relay.delete_tg_message(tg_chat_id, tg_msg_id, f" (DC: '{chat_title}'{extra_info})"),
                app_ctx.main_loop
            )

    except Exception as e:
        logger.error(f"Error in DC msg deletion handler: {e}")


@app_ctx.dc_cli.on(events.RawEvent(EventType.MSGS_CHANGED))
def handle_dc_msg_modified(bot, accid, event):
    if not app_ctx.tg_app or not app_ctx.main_loop:
        return

    try:
        msg_id = getattr(event, 'msg_id', None)
        chat_id = getattr(event, 'chat_id', None)
        if not msg_id or not chat_id:
            return

        if message_relay.is_pending_relay(chat_id, msg_id):
            return

        dc_chan = database.get_dc_channel_by_dc_chat_id(chat_id)
        if not dc_chan:
            return

        tg_channel_id = dc_chan['tg_channel_id']

        tg_msg_id = database.get_tg_msg_id(msg_id, chat_id, tg_channel_id)
        if not tg_msg_id:
            return

        dc_msg = bot.rpc.get_message(accid, msg_id)
        if not dc_msg:
            return

        new_hash = edit_sync_service.get_content_hash(dc_msg)
        old_hash = database.get_message_content_hash(tg_msg_id, tg_channel_id, chat_id)
        if old_hash and old_hash == new_hash:
            return

        if rate_limiter.check_edit_debounce(chat_id, msg_id):
            return

        text = getattr(dc_msg, 'text', '') or ''
        has_file = bool(getattr(dc_msg, 'file', None))

        asyncio.run_coroutine_threadsafe(
            message_relay.edit_tg_channel_post(tg_channel_id, tg_msg_id, text, has_file,
                                                dc_msg_id=msg_id, dc_chat_id=chat_id, new_hash=new_hash),
            app_ctx.main_loop,
        )

    except Exception as e:
        logger.error(f"Failed to process DC→TG edit for msg {event.msg_id}: {e}")


@app_ctx.dc_cli.on(events.RawEvent(EventType.REACTIONS_CHANGED))
def handle_dc_reaction(bot, accid, event):
    if not app_ctx.tg_app or not app_ctx.main_loop:
        return

    try:
        contact_id = getattr(event, 'contact_id', None)
        if app_ctx.bot_contact_id and contact_id and str(contact_id) == str(app_ctx.bot_contact_id):
            return

        msg_id = getattr(event, 'msg_id', None)
        if not msg_id:
            return

        dc_chat_id = bot.rpc.get_message(accid, msg_id).chat_id

        bot.logger.info(f"DC Reaction event for msg {msg_id} in DC chat {dc_chat_id} from contact {contact_id}")

        try:
            dc_reactions = bot.rpc.get_message_reactions(accid, msg_id)
        except Exception:
            dc_reactions = None

        tg_mappings = database.get_tg_mappings_by_dc_msg_id(msg_id)
        if not tg_mappings:
            return

        primary_emoji = None
        if dc_reactions and hasattr(dc_reactions, 'reactions') and dc_reactions.reactions:
            primary_emoji = dc_reactions.reactions[0].emoji
        else:
            bot.logger.info(f"No reactions found for DC msg {msg_id}")

        for tg_msg_id, tg_chat_id, dc_chat_id in tg_mappings:
            is_channel = database.get_channel_by_dc_chat_id(int(dc_chat_id))
            is_group = int(tg_chat_id) in database.get_tg_chats(int(dc_chat_id))

            if not is_channel and not is_group:
                logger.info(f"Skipping DC→TG reaction sync: bridge for DC chat {dc_chat_id} to TG {tg_chat_id} no longer exists.")
                continue

            try:
                from telegram import ReactionTypeEmoji
                reaction = [ReactionTypeEmoji(primary_emoji)] if primary_emoji else []
                asyncio.run_coroutine_threadsafe(
                    app_ctx.tg_app.bot.set_message_reaction(chat_id=tg_chat_id, message_id=tg_msg_id, reaction=reaction),
                    app_ctx.main_loop
                )
                if database.get_dc_channel_chat_id(tg_chat_id):
                    database.increment_channel_reaction_count(tg_chat_id)
                else:
                    database.increment_bridge_reaction_count(int(dc_chat_id), int(tg_chat_id))
            except Exception as e:
                bot.logger.error(f"Failed to relay DC reaction to TG chat {tg_chat_id}: {e}")
    except Exception as e:
        bot.logger.error(f"Error handling DC reaction: {e}")
