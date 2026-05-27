import html
import logging
import os

from telegram import Update
from telegram.ext import ContextTypes
from deltachat2 import MsgData

import database
from app_context import app_ctx
from constants import DC_MAX_MSG_LEN
from rate_limiter import rate_limiter
from relay import message_relay
from edit_sync import edit_sync_service
from media_handler import media_handler
from message_queue import ordered_queue, file_tracker
from shared import _inline_links

logger = logging.getLogger("tg_dc_bridge")


async def handle_tg_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post:
        return

    tg_channel_id = post.chat.id
    if rate_limiter.check_dedup(tg_channel_id, post.message_id):
        return

    tg_username = post.chat.username

    dc_chat_id = database.get_dc_channel_chat_id(tg_channel_id)
    if not dc_chat_id and tg_username:
        ch = database.get_channel_by_tg_username(tg_username)
        if ch:
            if not ch.get('tg_channel_id'):
                database.update_channel_tg_id(tg_username, tg_channel_id)
            dc_chat_id = ch['dc_chat_id']

    if not dc_chat_id or not app_ctx.dc_bot or not app_ctx.dc_accid:
        return

    if rate_limiter.check_per_chat(tg_channel_id):
        return

    text = post.text or post.caption or ""
    entities = post.entities or post.caption_entities or []
    text = _inline_links(text, entities)

    author = getattr(post, 'author_signature', None)

    tg_file, file_name, media_text_extra = media_handler.detect_channel_post_media(post)
    if media_text_extra:
        text = (text + media_text_extra).strip()

    loc_text = media_handler.detect_location(post)
    if loc_text:
        is_live = False
        if post.location:
            is_live = getattr(post.location, 'live_period', None) is not None
        if is_live:
            message_relay.update_live_location(post.message_id, post.location.latitude, post.location.longitude)
        text = (text + "\n\n" + loc_text).strip()

    if not text and not tg_file:
        return

    formatted_msg = text if text else ""

    if tg_username:
        formatted_msg = (formatted_msg + f"\n\n🔗 t.me/{tg_username}/{post.message_id}").strip()

    formatted_msg = _truncate(formatted_msg, DC_MAX_MSG_LEN)

    local_file_path = None
    if tg_file:
        local_file_path, timeout_error = await media_handler.download_media(tg_file, file_name)
        if timeout_error:
            formatted_msg += timeout_error

    async def _relay():
        try:
            msg_data = MsgData(text=formatted_msg)
            if author:
                msg_data.override_sender_name = author
            if local_file_path and os.path.exists(local_file_path):
                msg_data.file = local_file_path
            dc_msg_id = app_ctx.dc_bot.rpc.send_msg(app_ctx.dc_accid, dc_chat_id, msg_data)
            if dc_msg_id:
                c_hash = edit_sync_service.get_content_hash(post)
                database.save_message_map(dc_msg_id, dc_chat_id, post.message_id, tg_channel_id, content_hash=c_hash)
            rate_limiter.register_edit_timestamp(tg_channel_id, post.message_id)

            logger.info(f"Relayed channel post from @{tg_username or tg_channel_id} to DC broadcast {dc_chat_id}")
        except Exception as e:
            logger.error(f"Failed to relay channel post to DC: {e}")
        finally:
            if local_file_path and os.path.exists(local_file_path):
                try:
                    os.unlink(local_file_path)
                except Exception:
                    pass

    await ordered_queue.enqueue(f"dc:{dc_chat_id}", _relay())


async def handle_tg_edited_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.edited_channel_post
    if not post:
        return

    tg_channel_id = post.chat.id
    if rate_limiter.check_dedup(tg_channel_id, post.message_id):
        return

    tg_username = post.chat.username

    if post.location:
        is_live = getattr(post.location, 'live_period', None) is not None
        if is_live or post.message_id in message_relay.live_locations:
            lat, lon = post.location.latitude, post.location.longitude
            message_relay.update_live_location(post.message_id, lat, lon)

            if not is_live:
                message_relay.pop_live_location(post.message_id)
                final_text = f"🛑 Live Location Ended\nFinal coordinates: https://maps.google.com/?q={lat},{lon}"
                dc_chat_id = database.get_dc_channel_chat_id(tg_channel_id)
                if not dc_chat_id and tg_username:
                    ch = database.get_channel_by_tg_username(tg_username)
                    if ch:
                        dc_chat_id = ch['dc_chat_id']
                if dc_chat_id and app_ctx.dc_bot and app_ctx.dc_accid:
                    try:
                        dc_reply_id = database.get_dc_msg_id(post.message_id, tg_channel_id, dc_chat_id)
                        msg_data = MsgData(text=final_text)
                        if dc_reply_id:
                            msg_data.quoted_message_id = dc_reply_id
                        app_ctx.dc_bot.rpc.send_msg(app_ctx.dc_accid, dc_chat_id, msg_data)
                    except Exception as e:
                        logger.error(f"Failed to send final location: {e}")
                return

            if not (post.text or post.caption):
                return

    dc_chat_id = database.get_dc_channel_chat_id(tg_channel_id)
    if not dc_chat_id and tg_username:
        ch = database.get_channel_by_tg_username(tg_username)
        if ch:
            dc_chat_id = ch['dc_chat_id']

    if not dc_chat_id or not app_ctx.dc_bot or not app_ctx.dc_accid:
        return

    new_hash = edit_sync_service.get_content_hash(post)
    old_hash = database.get_message_content_hash(post.message_id, tg_channel_id, dc_chat_id)
    if old_hash and old_hash == new_hash:
        return

    if rate_limiter.check_edit_debounce(tg_channel_id, post.message_id):
        return

    text = post.text or post.caption or ""
    entities = post.entities or post.caption_entities or []
    text = _inline_links(text, entities)

    author = getattr(post, 'author_signature', None)

    if not text:
        return

    formatted_msg = text

    if tg_username:
        formatted_msg = (formatted_msg + f"\n\n🔗 t.me/{tg_username}/{post.message_id}").strip()

    formatted_msg = _truncate(formatted_msg, DC_MAX_MSG_LEN)

    async def _relay_edit():
        try:
            old_dc_msg_id = database.get_dc_msg_id(post.message_id, tg_channel_id, dc_chat_id)
            if old_dc_msg_id:
                try:
                    edit_sync_service.register_bot_delete(old_dc_msg_id)
                    app_ctx.dc_bot.rpc.delete_messages(app_ctx.dc_accid, [old_dc_msg_id])
                except Exception as del_e:
                    logger.debug(f"Could not delete old DC msg {old_dc_msg_id} before edit relay: {del_e}")
                    edit_sync_service.consume_bot_delete(old_dc_msg_id)

            msg_data = MsgData(text=formatted_msg)
            if author:
                msg_data.override_sender_name = f"✏️ [Edited] {author}"
            else:
                msg_data.override_sender_name = "✏️ [Edited]"
            dc_sent_id = app_ctx.dc_bot.rpc.send_msg(app_ctx.dc_accid, dc_chat_id, msg_data)
            if dc_sent_id:
                database.save_message_map(dc_sent_id, dc_chat_id, post.message_id, tg_channel_id, content_hash=new_hash)
            logger.info(f"Relayed edited channel post from @{tg_username or tg_channel_id} to DC broadcast {dc_chat_id}")
        except Exception as e:
            logger.error(f"Failed to relay edited channel post to DC: {e}")

    await ordered_queue.enqueue(f"dc:{dc_chat_id}", _relay_edit())


async def handle_tg_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_message
    if not msg:
        return

    tg_chat_id = msg.chat.id
    if rate_limiter.check_dedup(tg_chat_id, msg.message_id):
        return

    if msg.location:
        is_live = getattr(msg.location, 'live_period', None) is not None
        if is_live or msg.message_id in message_relay.live_locations:
            lat, lon = msg.location.latitude, msg.location.longitude
            message_relay.update_live_location(msg.message_id, lat, lon)

            if not is_live:
                message_relay.pop_live_location(msg.message_id)
                final_text = f"🛑 Live Location Ended\nFinal coordinates: https://maps.google.com/?q={lat},{lon}"
                dc_chats = database.get_dc_chats(tg_chat_id)
                if dc_chats and app_ctx.dc_bot and app_ctx.dc_accid:
                    for dc_chat_id in dc_chats:
                        dc_reply_id = database.get_dc_msg_id(msg.message_id, tg_chat_id, dc_chat_id)
                        msg_data = MsgData(text=final_text)
                        if dc_reply_id:
                            msg_data.quoted_message_id = dc_reply_id
                        try:
                            await rate_limiter.wait_global_dc()
                            app_ctx.dc_bot.rpc.send_msg(app_ctx.dc_accid, dc_chat_id, msg_data)
                        except Exception as e:
                            logger.error(f"Failed to send final location edit: {e}")
                return

            if not (msg.text or msg.caption):
                return

    if msg.chat.type == "private":
        return

    if msg.from_user and msg.from_user.is_bot:
        return

    dc_chats = database.get_dc_chats(tg_chat_id)
    if not dc_chats or not app_ctx.dc_bot or not app_ctx.dc_accid:
        return

    new_hash = edit_sync_service.get_content_hash(msg)
    old_hash = database.get_message_content_hash(msg.message_id, tg_chat_id, dc_chats[0])
    if old_hash and old_hash == new_hash:
        return

    if rate_limiter.check_edit_debounce(tg_chat_id, msg.message_id):
        return

    sender = msg.from_user
    sender_name = sender.first_name
    if sender.last_name:
        sender_name += f" {sender.last_name}"

    text = msg.text or msg.caption or ""

    if text.startswith('/') and not msg.photo and not msg.video and not msg.document:
        return

    entities = msg.entities or msg.caption_entities or []
    text = _inline_links(text, entities)

    if not text:
        return

    formatted_msg = f"✏️ [Edited]:\n{text}"
    formatted_msg = _truncate(formatted_msg, DC_MAX_MSG_LEN)

    for dc_chat_id in dc_chats:
        async def _relay_edit(dc_chat_id=dc_chat_id):
            try:
                old_dc_msg_id = database.get_dc_msg_id(msg.message_id, tg_chat_id, dc_chat_id)
                if old_dc_msg_id:
                    try:
                        edit_sync_service.register_bot_delete(old_dc_msg_id)
                        app_ctx.dc_bot.rpc.delete_messages(app_ctx.dc_accid, [old_dc_msg_id])
                    except Exception as del_e:
                        logger.debug(f"Could not delete old DC msg {old_dc_msg_id} before edit relay: {del_e}")
                        edit_sync_service.consume_bot_delete(old_dc_msg_id)

                msg_data = MsgData(text=formatted_msg)
                msg_data.override_sender_name = sender_name
                await rate_limiter.wait_global_dc()
                dc_sent_id = app_ctx.dc_bot.rpc.send_msg(app_ctx.dc_accid, dc_chat_id, msg_data)
                if dc_sent_id:
                    database.save_message_map(dc_sent_id, dc_chat_id, msg.message_id, tg_chat_id, content_hash=new_hash)
                logger.info(f"Relayed edited TG msg to DC chat {dc_chat_id}")
            except Exception as e:
                logger.error(f"Failed to relay edited msg to DC chat {dc_chat_id}: {e}")

        await ordered_queue.enqueue(f"dc:{dc_chat_id}", _relay_edit())


async def handle_tg_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message:
        return

    tg_chat_id = update.effective_chat.id
    if rate_limiter.check_dedup(tg_chat_id, update.message.message_id):
        return

    if update.effective_chat.type == "private":
        return

    if update.message.from_user and update.message.from_user.is_bot:
        return

    dc_chats = database.get_dc_chats(tg_chat_id)
    if not dc_chats or not app_ctx.dc_bot or not app_ctx.dc_accid:
        return

    if rate_limiter.check_per_chat(tg_chat_id):
        return

    sender = update.message.from_user
    sender_name = sender.first_name
    if sender.last_name:
        sender_name += f" {sender.last_name}"

    text = update.message.text or update.message.caption or ""

    if text.startswith('/') and not update.message.photo and not update.message.video and not update.message.document:
        return

    entities = update.message.entities or update.message.caption_entities or []
    text = _inline_links(text, entities)

    if update.message.poll:
        poll = update.message.poll
        poll_text = f"📊 {poll.question}\n"
        for option in poll.options:
            poll_text += f"▫️ {option.text}\n"
        text = (text + "\n\n" + poll_text).strip()

        for dc_chat_id in dc_chats:
            database.save_poll_context(poll.id, tg_chat_id, dc_chat_id)

    tg_file, file_name, media_text_extra = media_handler.detect_message_media(update.message)
    if media_text_extra:
        text = (text + media_text_extra).strip()

    loc_text = media_handler.detect_location(update.message)
    if loc_text:
        is_live = False
        if update.message.location:
            is_live = getattr(update.message.location, 'live_period', None) is not None
        if is_live:
            message_relay.update_live_location(update.message.message_id,
                                               update.message.location.latitude,
                                               update.message.location.longitude)
        text = (text + "\n\n" + loc_text).strip()

    if not text and not tg_file:
        return

    reply_prefix = ""
    tg_reply_to_msg_id = None
    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        tg_reply_to_msg_id = replied.message_id
        replied_text = replied.text or replied.caption or ""
        if replied_text:
            short_quote = _truncate(replied_text, 80)
            reply_prefix = f"↩ {short_quote}\n"

    local_file_path = None
    if tg_file:
        local_file_path, timeout_error_text = await media_handler.download_media(
            tg_file, file_name, tg_chat_id, update.message.message_id
        )

    if local_file_path:
        for _ in dc_chats:
            file_tracker.add_ref(local_file_path)

    for dc_chat_id in dc_chats:
        async def _relay(dc_chat_id=dc_chat_id):
            try:
                dc_reply_id = None
                if tg_reply_to_msg_id:
                    dc_reply_id = database.get_dc_msg_id(tg_reply_to_msg_id, tg_chat_id, dc_chat_id)

                chat_reply_prefix = reply_prefix if not dc_reply_id else ""

                formatted_msg = f"{chat_reply_prefix}{text}" if text else ""
                formatted_msg = _truncate(formatted_msg, DC_MAX_MSG_LEN)
                if timeout_error_text:
                    formatted_msg += timeout_error_text

                msg_data = MsgData(text=formatted_msg)
                msg_data.override_sender_name = sender_name
                if dc_reply_id:
                    msg_data.quoted_message_id = dc_reply_id
                if local_file_path and os.path.exists(local_file_path):
                    msg_data.file = local_file_path
                try:
                    await rate_limiter.wait_global_dc()
                    sent_msg_id = message_relay.dc_send_msg(app_ctx.dc_bot, app_ctx.dc_accid, dc_chat_id, msg_data)
                except Exception as e:
                    if "does not exist" in str(e).lower() and "message" in str(e).lower():
                        logger.warning(f"Quoted message {dc_reply_id} not found in DC chat {dc_chat_id}. Retrying without quote.")
                        msg_data.quoted_message_id = None
                        if not chat_reply_prefix:
                            replied = update.message.reply_to_message
                            replied_text = replied.text or replied.caption or ""
                            if replied_text:
                                short_quote = _truncate(replied_text, 80)
                                chat_reply_prefix = f"↩ {short_quote}\n"
                                msg_data.text = _truncate(f"{chat_reply_prefix}{text}" if text else "", DC_MAX_MSG_LEN)

                        await rate_limiter.wait_global_dc()
                        sent_msg_id = message_relay.dc_send_msg(app_ctx.dc_bot, app_ctx.dc_accid, dc_chat_id, msg_data)
                    else:
                        raise

                if sent_msg_id:
                    c_hash = edit_sync_service.get_content_hash(update.message)
                    database.save_message_map(sent_msg_id, dc_chat_id, update.message.message_id, tg_chat_id, content_hash=c_hash)
                rate_limiter.register_edit_timestamp(tg_chat_id, update.message.message_id)
                logger.info(f"Relayed TG msg to DC chat {dc_chat_id}")
            except Exception as e:
                logger.error(f"Failed to relay msg to DC chat {dc_chat_id}: {e}")
            finally:
                if local_file_path:
                    file_tracker.release(local_file_path)

        await ordered_queue.enqueue(f"dc:{dc_chat_id}", _relay())


async def handle_tg_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll = update.poll
    if not poll or not poll.is_closed:
        return

    ctx = database.get_poll_context(poll.id)
    if not ctx:
        return

    tg_chat_id, dc_chat_id = ctx

    if not app_ctx.dc_bot or not app_ctx.dc_accid:
        return
    if rate_limiter.check_per_chat(tg_chat_id):
        return

    total_voter_count = poll.total_voter_count

    text = f"🏁 **Poll closed:** {html.escape(poll.question)}\n\n"

    sorted_options = sorted(poll.options, key=lambda x: x.voter_count, reverse=True)

    for option in sorted_options:
        percentage = (option.voter_count / total_voter_count * 100) if total_voter_count > 0 else 0
        text += f"▫️ {html.escape(option.text)} — {option.voter_count} votes ({percentage:.1f}%)\n"

    text += f"\n*Total votes: {total_voter_count}*"

    try:
        msg_data = MsgData(text=text)
        message_relay.dc_send_msg(app_ctx.dc_bot, app_ctx.dc_accid, dc_chat_id, msg_data)
        logger.info(f"Relayed TG poll results to DC chat {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to relay poll results to DC chat {dc_chat_id}: {e}")


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"
