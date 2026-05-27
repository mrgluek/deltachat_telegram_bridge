import asyncio
import logging
import os
import sys
import time
import getpass

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, MessageReactionHandler, ChatMemberHandler

import database
from app_context import app_ctx
from logging_setup import setup_logging

logger = logging.getLogger("tg_dc_bridge")


def start_dc_bot():
    try:
        app_ctx.dc_cli.start()
    except SystemExit:
        pass
    except Exception as e:
        logger.error(f"DC CLI error: {e}")


async def db_cleanup_loop():
    while True:
        try:
            database.cleanup_old_messages()
            await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            await asyncio.sleep(60)


def _import_handlers():
    from dc_handlers import (on_init, on_start, setprimary_command, help_command,
        transports_command, rmtransport_command, addtransport_command, dc_donate_command,
        dc_channeladd_command, dc_channelremove_command, bridge_command, unbridge_command,
        locupdate_command, dc_userbotsync_command, dc_userbotjoin_command, stats_command,
        channels_command_dc, handle_dc_message, handle_dc_info_message,
        handle_dc_msg_deleted, handle_dc_msg_modified, handle_dc_reaction)
    from tg_handlers import (tg_start_command, tg_help_command, tg_donate_command, tg_id_command,
        tg_stats_command, tg_bridge_command, tg_unbridge_command, tg_adminadd_command,
        tg_adminremove_command, tg_admins_command, tg_invite_command, tg_inviteqr_command,
        tg_channeladd_command, tg_userbotsync_command, tg_userbotjoin_command, tg_groups_command,
        tg_channels_command, tg_channel_command, tg_channelqr_command, tg_channelremove_command,
        handle_tg_channel_post, handle_tg_edited_channel_post, handle_tg_message,
        handle_tg_edited_message, handle_tg_poll, handle_tg_reaction, handle_my_chat_member,
        handle_tg_migration, tg_error_handler)
    from userbot_manager import userbot_manager
    return {k: v for k, v in locals().items() if not k.startswith('_')}


def _register_handlers(tg_app, h):
    tg_app.add_error_handler(h['tg_error_handler'])
    for cmd, func in (("start", "tg_start_command"), ("help", "tg_help_command"),
                      ("donate", "tg_donate_command"), ("id", "tg_id_command"),
                      ("stats", "tg_stats_command"), ("invite", "tg_invite_command"),
                      ("inviteqr", "tg_inviteqr_command"), ("bridge", "tg_bridge_command"),
                      ("unbridge", "tg_unbridge_command"), ("adminadd", "tg_adminadd_command"),
                      ("adminremove", "tg_adminremove_command"), ("admins", "tg_admins_command"),
                      ("channeladd", "tg_channeladd_command"), ("channels", "tg_channels_command"),
                      ("groups", "tg_groups_command"), ("channel", "tg_channel_command"),
                      ("channelqr", "tg_channelqr_command"), ("channelremove", "tg_channelremove_command"),
                      ("userbotsync", "tg_userbotsync_command"), ("userbotjoin", "tg_userbotjoin_command")):
        tg_app.add_handler(CommandHandler(cmd, h[func]))
    tg_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/channel\d+$'), h['tg_channel_command']))
    tg_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/channel\d+qr$'), h['tg_channelqr_command']))
    tg_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/channelqr\d+$'), h['tg_channelqr_command']))
    tg_app.add_handler(ChatMemberHandler(h['handle_my_chat_member'], ChatMemberHandler.MY_CHAT_MEMBER), group=1)
    tg_app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, h['handle_tg_migration']), group=1)

    media_filters = filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.VOICE | filters.AUDIO | filters.Sticker.ALL | filters.ANIMATION | filters.VIDEO_NOTE | filters.LOCATION | filters.VENUE
    tg_app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST & media_filters, h['handle_tg_channel_post']), group=1)
    tg_app.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST & (filters.TEXT | filters.CAPTION | filters.LOCATION), h['handle_tg_edited_channel_post']), group=1)
    tg_app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & (media_filters | filters.POLL), h['handle_tg_message']), group=1)
    tg_app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & (filters.TEXT | filters.CAPTION | filters.LOCATION), h['handle_tg_edited_message']), group=1)
    from telegram.ext import PollHandler
    tg_app.add_handler(PollHandler(h['handle_tg_poll']))
    tg_app.add_handler(MessageReactionHandler(h['handle_tg_reaction']))


async def _userbot_health_loop(app_ctx, userbot_manager):
    last_sync = time.time()
    last_userbot_check = time.time()
    try:
        while True:
            await asyncio.sleep(10)
            now = time.time()

            if (now - last_userbot_check) > 60:
                api_id = database.get_config("api_id")
                api_hash = database.get_config("api_hash")
                if api_id and api_hash:
                    is_healthy = False
                    try:
                        if app_ctx.userbot_client and app_ctx.userbot_client.is_connected() and await app_ctx.userbot_client.is_user_authorized():
                            await app_ctx.userbot_client.get_me()
                            is_healthy = True
                    except (AttributeError, Exception) as e:
                        logger.warning(f"Userbot health check failed: {e}. Attempting restart...")
                    if not is_healthy:
                        logger.info("Userbot client is offline or unhealthy. Restarting...")
                        asyncio.create_task(userbot_manager.start())
                last_userbot_check = now

            if (now - last_sync) > 3600:
                if app_ctx.userbot_client and app_ctx.userbot_client.is_connected():
                    asyncio.create_task(userbot_manager.sync_channels())
                last_sync = now
    except asyncio.CancelledError:
        pass


async def main():
    h = _import_handlers()
    userbot_manager = h['userbot_manager']

    args = sys.argv[:]
    is_serve = "serve" in args

    if not is_serve:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, start_dc_bot)
        return

    token = database.get_config("telegram_token") or os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("Error: Telegram token not found. Setup using 'python bot.py init tg <TOKEN>'.")
        sys.exit(1)

    app_ctx.main_loop = asyncio.get_running_loop()
    tg_app = Application.builder().token(token).build()
    app_ctx.tg_app = tg_app
    _register_handlers(tg_app, h)

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    asyncio.create_task(db_cleanup_loop())
    await userbot_manager.start()

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, start_dc_bot)

    logger.info("Bridge is now fully running. Waiting for events...")
    await _userbot_health_loop(app_ctx, userbot_manager)

    logger.info("Shutting down Telegram bot...")
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()
    logger.info("Shutdown complete.")
