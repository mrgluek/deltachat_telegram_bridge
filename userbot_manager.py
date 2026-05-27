import asyncio
import html
import logging
import time
import random
from typing import Optional

import database
from app_context import app_ctx
from rate_limiter import rate_limiter
from constants import DELETE_SYNC_MAX, DELETE_SYNC_WINDOW

logger = logging.getLogger("tg_dc_bridge")


class UserbotManager:
    def __init__(self):
        self._tasks: set[asyncio.Task] = set()
        self._syncing = False

    async def start(self):
        try:
            from telethon import TelegramClient, events as tg_events
            from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
            from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
            from telethon.errors import ChannelPrivateError
        except ImportError:
            logger.warning("Telethon not installed, Userbot unavailable.")
            return

        api_id = database.get_config("api_id")
        api_hash = database.get_config("api_hash")
        if not (api_id and api_hash):
            return

        try:
            if self._tasks:
                logger.info(f"Cancelling {len(self._tasks)} pending Userbot tasks during restart...")
                for task in self._tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)
                self._tasks.clear()

            if app_ctx.userbot_client:
                try:
                    await asyncio.wait_for(app_ctx.userbot_client.disconnect(), timeout=10)
                except Exception as e:
                    logger.warning(f"Error disconnecting old Userbot client: {e}")
                finally:
                    app_ctx.userbot_client = None

            logger.info("Initializing new Telethon Userbot client...")
            app_ctx.userbot_client = TelegramClient('userbot_session', int(api_id), api_hash, sequential_updates=True)

            @app_ctx.userbot_client.on(tg_events.NewMessage())
            async def on_new_userbot_msg(event):
                await self._process_event(event, is_edit=False)

            @app_ctx.userbot_client.on(tg_events.MessageEdited())
            async def on_edited_userbot_msg(event):
                await self._process_event(event, is_edit=True)

            @app_ctx.userbot_client.on(tg_events.MessageDeleted())
            async def on_deleted_userbot_msg(event):
                await self._process_deletion(event)

            await app_ctx.userbot_client.start()
            logger.info("Telethon Userbot client started successfully.")

            me = await app_ctx.userbot_client.get_me()
            if me:
                last_id = database.get_config("userbot_last_user_id")
                if last_id != str(me.id):
                    logger.info(f"New Userbot account detected ({me.id}). Triggering auto-sync...")
                    database.set_config("userbot_last_user_id", str(me.id))
                    asyncio.create_task(self.sync_channels())
        except Exception as e:
            logger.error(f"Failed to start Userbot client: {e}")
            app_ctx.userbot_client = None

    async def leave_chat(self, tg_chat_id: int):
        userbot_client = app_ctx.userbot_client
        if not (userbot_client and userbot_client.is_connected()):
            return

        await asyncio.sleep(1)

        if database.count_bridges_for_tg(tg_chat_id) > 0:
            logger.debug(f"Userbot: Keeping membership in {tg_chat_id} (other bridges exist).")
            return

        try:
            from telethon.tl.types import Channel, Chat
            from telethon.tl.functions.channels import LeaveChannelRequest
            from telethon.tl.functions.messages import DeleteChatUserRequest
            entity = await userbot_client.get_entity(tg_chat_id)
            if isinstance(entity, Channel):
                await userbot_client(LeaveChannelRequest(entity))
                logger.info(f"Userbot: Left Telegram channel/supergroup {tg_chat_id}")
            elif isinstance(entity, Chat):
                me = await userbot_client.get_me()
                await userbot_client(DeleteChatUserRequest(chat_id=entity.id, user_id=me.id))
                logger.info(f"Userbot: Left Telegram group {tg_chat_id}")
        except Exception as e:
            logger.debug(f"Userbot: Could not leave chat {tg_chat_id} (maybe already left): {e}")

    async def create_channel(self, title: str, bot_username: str):
        userbot_client = app_ctx.userbot_client
        if not (userbot_client and userbot_client.is_connected()):
            raise RuntimeError("Userbot is not connected")

        from telethon.tl.functions.channels import CreateChannelRequest, EditAdminRequest
        from telethon.tl.functions.messages import ExportChatInviteRequest
        from telethon.tl.types import ChatAdminRights

        result = await userbot_client(CreateChannelRequest(title=title, about="Bridged from Delta Chat", megagroup=False))
        channel = result.chats[0]
        tg_channel_id = int(f"-100{channel.id}")

        bot_entity = await userbot_client.get_entity(f"@{bot_username}")
        rights = ChatAdminRights(
            post_messages=True, edit_messages=True, delete_messages=True,
            invite_users=True, ban_users=True, add_admins=True,
            manage_call=True, other=True
        )
        await userbot_client(EditAdminRequest(channel, bot_entity, rights, rank=""))

        invite = await userbot_client(ExportChatInviteRequest(channel))
        return tg_channel_id, invite.link

    async def join_chat(self, link: str):
        """Join a chat via Userbot. Returns (entity, title, tg_channel_id)."""
        import re
        userbot_client = app_ctx.userbot_client
        if not (userbot_client and userbot_client.is_connected()):
            return None, None, None

        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest

        joined_entity = None
        joined_title = "Unknown"
        tg_channel_id = None

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
                return None, None, None

            try:
                check = await userbot_client(CheckChatInviteRequest(invite_hash))
                if hasattr(check, 'chat'):
                    joined_entity = check.chat
                    joined_title = getattr(joined_entity, 'title', 'Unknown')
                else:
                    result = await userbot_client(ImportChatInviteRequest(invite_hash))
                    if hasattr(result, 'chats') and result.chats:
                        joined_entity = result.chats[0]
                        joined_title = getattr(joined_entity, 'title', 'Unknown')
            except Exception as e:
                if "already" in str(e).lower() or "USER_ALREADY_PARTICIPANT" in str(e):
                    try:
                        joined_entity = await userbot_client.get_entity(link)
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

            entity = await userbot_client.get_entity(target)
            if getattr(entity, 'left', True):
                if JoinChannelRequest:
                    await userbot_client(JoinChannelRequest(entity))
            joined_entity = entity
            joined_title = getattr(entity, 'title', 'Unknown')

        if joined_entity:
            joined_id = getattr(joined_entity, 'id', None)
            tg_channel_id = int(f"-100{joined_id}") if joined_id and joined_id > 0 else joined_id

        return joined_entity, joined_title, tg_channel_id

    async def update_channel_stats(self, channel_id: int, tg_peer):
        userbot_client = app_ctx.userbot_client
        if not (userbot_client and userbot_client.is_connected()):
            return 0

        try:
            from telethon.tl.functions.channels import GetFullChannelRequest
            from telethon.tl.functions.messages import GetFullChatRequest
            from telethon.tl.types import Channel, Chat

            full = None
            username = getattr(tg_peer, 'username', None)

            if isinstance(tg_peer, Channel):
                full = await userbot_client(GetFullChannelRequest(tg_peer))
            elif isinstance(tg_peer, Chat):
                full = await userbot_client(GetFullChatRequest(tg_peer.id))

            count = 0
            if full and hasattr(full, 'full_chat'):
                count = getattr(full.full_chat, 'participants_count', 0)

            database.update_channel_info(channel_id, participants_count=count, username=username)
            return count
        except Exception as e:
            logger.warning(f"Failed to fetch stats for TG peer: {e}")
        return 0

    async def sync_channels(self, force: bool = False):
        userbot_client = app_ctx.userbot_client
        if not (userbot_client and userbot_client.is_connected()):
            return

        if self._syncing and not force:
            logger.info("Userbot sync is already in progress.")
            return

        from telethon.tl.functions.channels import JoinChannelRequest

        self._syncing = True
        try:
            me = await userbot_client.get_me()
            if not me:
                return

            logger.info(f"Starting Userbot sync for account {me.id} (@{getattr(me, 'username', 'N/A')})...")
            channels = database.get_all_channels()

            joined_count = 0
            failed_reports = []

            for chan in channels:
                tg_id = chan.get('tg_channel_id')
                username = chan.get('tg_channel_username')
                invite_link = chan.get('invite_link')

                target = (f"@{username}" if username else None) or tg_id

                if not target:
                    continue

                try:
                    entity = None
                    try:
                        entity = await userbot_client.get_entity(target)
                    except Exception as e:
                        if invite_link and ("t.me/" in invite_link or "telegram.me/" in invite_link):
                            logger.debug(f"Sync: Could not resolve {target} by ID, trying invite link: {e}")
                            try:
                                entity = await userbot_client.get_entity(invite_link)
                            except Exception as e2:
                                logger.debug(f"Sync: Could not resolve via link either: {e2}")

                    if not entity:
                        raise Exception(f"Could not resolve {target} (and no working invite link)")

                    if getattr(entity, 'left', True):
                        logger.info(f"Userbot: Joining channel {target}...")
                        if JoinChannelRequest:
                            await userbot_client(JoinChannelRequest(entity))
                            joined_count += 1
                            chan_id = chan.get('id')
                            if chan_id:
                                await self.update_channel_stats(chan_id, entity)
                            delay = random.uniform(5, 20)
                            await asyncio.sleep(delay)
                    else:
                        chan_id = chan.get('id')
                        if chan_id:
                            await self.update_channel_stats(chan_id, entity)
                except Exception as e:
                    logger.warning(f"Userbot: Could not sync/join channel {target}: {e}")
                    chan_id = chan.get('id', '?')
                    tg_id = chan.get('tg_channel_id', '?')
                    username = chan.get('tg_channel_username')
                    clean_id = str(tg_id).replace("-100", "")
                    report = f"• <b>{html.escape(f'Channel #{chan_id}')}</b> (ID: <code>{tg_id}</code>)"
                    if username:
                        report += f" (@{html.escape(username)})"
                    else:
                        report += f" — <a href='https://t.me/c/{clean_id}/1'>Search Link</a>"
                    failed_reports.append(report)

            database.set_config("userbot_last_user_id", str(me.id))
            logger.info(f"Userbot sync completed. Joined {joined_count} channels.")

            if failed_reports and (force or joined_count > 0):
                admin_tg_id = database.get_config("admin_tg_id")
                if admin_tg_id and app_ctx.tg_app:
                    try:
                        report_text = (
                            f"⚠️ <b>Userbot Sync Summary</b>\n\n"
                            f"Completed. Joined {joined_count} channels.\n"
                            f"The following {len(failed_reports)} channels require <b>manual join</b> on your new technical account:\n\n"
                            + "\n".join(failed_reports) +
                            f"\n\n<i>Note: Click the ID links to find the group if you are already a member, or search for these IDs in your Telegram history.</i>"
                        )
                        if len(report_text) > 4000:
                            report_text = report_text[:3900] + "...\n(List too long)"

                        await app_ctx.tg_app.bot.send_message(chat_id=int(admin_tg_id), text=report_text, parse_mode='HTML')
                    except Exception as notify_e:
                        logger.error(f"Failed to send sync report to owner: {notify_e}")
        finally:
            self._syncing = False

    async def _process_event(self, event, is_edit: bool = False):
        current_task = asyncio.current_task()
        if current_task:
            self._tasks.add(current_task)

        try:
            await self._process_event_internal(event, is_edit)
        finally:
            if current_task:
                self._tasks.discard(current_task)

    async def _process_event_internal(self, event, is_edit: bool = False):
        userbot_client = app_ctx.userbot_client
        msg = event.message
        if not msg:
            return

        if not (userbot_client and userbot_client.is_connected()):
            return

        sender_id = getattr(msg, 'sender_id', None) or getattr(msg, 'from_id', None)
        if sender_id == 777000 and msg.text:
            logger.info("Userbot: Received message from Telegram service account (login code)")
            code_text = f"🔐 *Login code for technical account:*\n\n`{msg.text}`"
            admin_tg_id = database.get_config("admin_tg_id")
            if admin_tg_id and app_ctx.tg_app:
                try:
                    await app_ctx.tg_app.bot.send_message(chat_id=int(admin_tg_id), text=code_text, parse_mode='Markdown')
                except Exception as e:
                    logger.error(f"Failed to forward login code to TG admin: {e}")

            if app_ctx.dc_bot and app_ctx.dc_accid:
                admin_dc_email = database.get_config("admin_dc_email")
                if admin_dc_email:
                    try:
                        from deltachat2 import MsgData
                        dc_code_text = f"🔐 Login code for technical account:\n\n{msg.text}"
                        contact_id = app_ctx.dc_bot.rpc.create_contact(app_ctx.dc_accid, admin_dc_email, "Admin")
                        chat_id = app_ctx.dc_bot.rpc.create_chat_by_contact_id(app_ctx.dc_accid, contact_id)
                        app_ctx.dc_bot.rpc.send_msg(app_ctx.dc_accid, chat_id, MsgData(text=dc_code_text))
                    except Exception as e:
                        logger.error(f"Failed to forward login code to DC admin: {e}")
            return

        tg_channel_id = msg.chat_id
        if rate_limiter.check_dedup(tg_channel_id, msg.id):
            return

        if is_edit and rate_limiter.check_edit_debounce(tg_channel_id, msg.id):
            return

        dc_chat_id = database.get_dc_channel_chat_id(tg_channel_id)

        if not dc_chat_id:
            chat_username = getattr(msg.chat, 'username', None)
            if chat_username:
                chan_data = database.get_channel_by_tg_username(chat_username)
                if chan_data:
                    dc_chat_id = chan_data['dc_chat_id']
                    logger.info(f"USERBOT: Found matching channel by username @{chat_username}. Updating numeric ID to {tg_channel_id}...")
                    database.update_channel_tg_id(chat_username, tg_channel_id)

        if not dc_chat_id or not app_ctx.dc_bot or not app_ctx.dc_accid:
            return

        from edit_sync import edit_sync_service
        new_hash = edit_sync_service.get_content_hash(msg)
        old_hash = database.get_message_content_hash(msg.id, tg_channel_id, dc_chat_id)

        if is_edit and old_hash and old_hash == new_hash:
            return

        from relay import message_relay
        from message_queue import ordered_queue
        await ordered_queue.enqueue(f"dc:{dc_chat_id}",
            message_relay.relay_userbot_message(dc_chat_id, msg, is_edit=is_edit))

    async def _process_deletion(self, event):
        if not app_ctx.dc_bot or not app_ctx.dc_accid:
            return

        chat_id = getattr(event, 'chat_id', None)
        deleted_ids = getattr(event, 'deleted_ids', []) or []

        if not deleted_ids or not chat_id:
            return

        for tg_msg_id in deleted_ids:
            dc_pairs = database.get_dc_msgs_by_tg_msg_id(tg_msg_id, chat_id)
            if not dc_pairs:
                continue

            for dc_msg_id, dc_chat_id in dc_pairs:
                database.delete_message_map_entry_by_tg(tg_msg_id, chat_id)

                if rate_limiter.check_deletion_sync():
                    logger.warning(f"TG→DC deletion sync rate limit hit ({DELETE_SYNC_MAX}/{DELETE_SYNC_WINDOW}s). Skipping deletion of DC msg {dc_msg_id}.")
                    admin_tg_id = database.get_config("admin_tg_id")
                    if admin_tg_id and app_ctx.tg_app:
                        try:
                            await app_ctx.tg_app.bot.send_message(
                                chat_id=int(admin_tg_id),
                                text=f"⚠️ <b>Bulk TG→DC deletion blocked</b>\nMore than {DELETE_SYNC_MAX} messages deleted in {DELETE_SYNC_WINDOW}s.\nTG msg {tg_msg_id} → DC msg {dc_msg_id} was <b>NOT</b> removed from Delta Chat.",
                                parse_mode='HTML'
                            )
                        except Exception:
                            pass
                    return

                try:
                    app_ctx.dc_bot.rpc.delete_messages(app_ctx.dc_accid, [dc_msg_id])
                    logger.info(f"TG→DC: Deleted DC msg {dc_msg_id} (mirrored TG msg {tg_msg_id} in {chat_id})")
                except Exception as e:
                    logger.warning(f"TG→DC: Could not delete DC msg {dc_msg_id}: {e}")


userbot_manager = UserbotManager()
