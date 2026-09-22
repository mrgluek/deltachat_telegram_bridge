"""Delta Chat side helpers shared across command/event handlers: chat
description formatting, bulk channel metadata refresh, contact
fingerprint/admin checks, and userbot channel-membership cleanup.

References to the pervasive bot.py singletons (tg_app, userbot_client)
go through a function-local `import bot as _bot_module` for the same
reasons documented in relay.py's module docstring.
"""
import asyncio
import logging
import os
import time
from typing import Optional

import database
from deltachat2 import MsgData

from caching import files_are_identical

logger = logging.getLogger("tg_dc_bridge")


def _get_tg_chat_desc(tg_chat_id: int) -> str:
    """Return a human-readable description for a Telegram chat/channel ID."""
    try:
        ch = database.get_channel_by_tg_id(tg_chat_id)
        if ch:
            uname = ch.get('tg_channel_username')
            ch_num = ch.get('id')
            if uname:
                return f"@{uname} (Channel #{ch_num})"
            return f"Channel #{ch_num} (ID: {tg_chat_id})"
    except Exception:
        pass
    return str(tg_chat_id)


async def async_update_channels_dc(bot, accid, reply_chat_id, target_channel_id=None):
    import bot as _bot_module
    try:
        # Send a starting status message
        target_str = f" channel #{target_channel_id}" if target_channel_id is not None else "s"
        status_msg_id = _dc_send_msg_with_stats(bot, accid, reply_chat_id, MsgData(text=f"🔄 Updating{target_str} from Telegram..."))
        if not status_msg_id:
            logger.error("Could not send update status message to DC.")
            return

        channels = database.get_all_channels()
        if not channels:
            bot.rpc.send_edit_request(accid, status_msg_id, "❌ No bridged channels found.")
            return

        if target_channel_id is not None:
            channels = [ch for ch in channels if ch['id'] == target_channel_id]
            if not channels:
                bot.rpc.send_edit_request(accid, status_msg_id, f"❌ Channel #{target_channel_id} not found in database.")
                return

        updated_count = 0
        error_count = 0

        for ch in channels:
            dc_chat_id = ch['dc_chat_id']
            tg_channel_id = ch['tg_channel_id']
            tg_username = ch.get('tg_channel_username')
            
            if tg_username:
                target_tg = f"@{tg_username}" if not tg_username.startswith("@") else tg_username
            else:
                target_tg = tg_channel_id

            if not target_tg:
                continue

            try:
                new_title = None
                avatar_path = None

                # Try fetching via Telegram Bot API first
                try:
                    chat = await _bot_module.tg_app.bot.get_chat(target_tg)
                    new_title = chat.title
                    if chat.photo:
                        avatar_path = f"tmp_avatar_{tg_channel_id}.jpg"
                        avatar_file = await chat.photo.get_big_file()
                        await avatar_file.download_to_drive(custom_path=avatar_path)
                    
                    # Fetch and update subscriber count
                    try:
                        member_count = await _bot_module.tg_app.bot.get_chat_member_count(target_tg)
                        database.update_channel_info(ch['id'], participants_count=member_count)
                        logger.info(f"Updated subscriber count for channel {target_tg} to {member_count}")
                    except Exception as mc_err:
                        logger.debug(f"Failed to get member count for {target_tg}: {mc_err}")
                except Exception as tg_err:
                    logger.debug(f"Bot API failed for {target_tg}, trying userbot: {tg_err}")
                    if _bot_module.userbot_client and _bot_module.userbot_client.is_connected():
                        entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(tg_channel_id), timeout=15.0)
                        new_title = entity.title
                        avatar_path = await asyncio.wait_for(_bot_module.userbot_client.download_profile_photo(tg_channel_id), timeout=30.0)
                        
                        # Fetch and update subscriber count via Userbot
                        try:
                            from telethon.tl.functions.channels import GetFullChannelRequest
                            from telethon.tl.functions.messages import GetFullChatRequest
                            from telethon.tl.types import Channel, Chat
                            
                            full = None
                            if isinstance(entity, Channel):
                                full = await asyncio.wait_for(_bot_module.userbot_client(GetFullChannelRequest(entity)), timeout=15.0)
                            elif isinstance(entity, Chat):
                                full = await asyncio.wait_for(_bot_module.userbot_client(GetFullChatRequest(entity.id)), timeout=15.0)
                            
                            if full and hasattr(full, 'full_chat'):
                                member_count = getattr(full.full_chat, 'participants_count', 0)
                                database.update_channel_info(ch['id'], participants_count=member_count)
                                logger.info(f"Userbot updated subscriber count for channel {tg_channel_id} to {member_count}")
                        except Exception as ub_mc_err:
                            logger.debug(f"Userbot failed to get member count for {tg_channel_id}: {ub_mc_err}")
                    else:
                        raise tg_err

                # Get current Delta Chat chat info to check name and avatar path
                current_chat = None
                try:
                    current_chat = bot.rpc.get_full_chat_by_id(accid, dc_chat_id)
                except Exception as e:
                    logger.debug(f"Failed to get chat info for DC chat {dc_chat_id}: {e}")

                # Apply updates to Delta Chat only if they changed
                if new_title:
                    old_title = current_chat.get("name") if current_chat else None
                    if new_title != old_title:
                        bot.rpc.set_chat_name(accid, dc_chat_id, new_title)
                        logger.info(f"Updated DC chat {dc_chat_id} name: {old_title} -> {new_title}")

                if avatar_path and os.path.exists(avatar_path):
                    old_avatar_path = current_chat.get("profile_image") if current_chat else None
                    if not files_are_identical(avatar_path, old_avatar_path):
                        bot.rpc.set_chat_profile_image(accid, dc_chat_id, avatar_path)
                        logger.info(f"Updated DC chat {dc_chat_id} profile image")
                    else:
                        logger.debug(f"DC chat {dc_chat_id} profile image is already up-to-date")
                    try: os.unlink(avatar_path)
                    except: pass
                
                updated_count += 1
            except Exception as ch_err:
                logger.error(f"Failed to update channel {target_tg}: {ch_err}")
                error_count += 1

        summary = f"✅ Channel update complete!\nUpdated: {updated_count}\nErrors: {error_count}"
        bot.rpc.send_edit_request(accid, status_msg_id, summary)

    except Exception as e:
        logger.error(f"Failed to run channels update: {e}")


def _get_contact_fingerprint(bot, accid, contact_id, contact=None):
    self_fps = set()
    try:
        bot_addrs = []
        bot_addr = bot.rpc.get_config(accid, "addr")
        if bot_addr: bot_addrs.append(bot_addr.lower().strip())
            
        try:
            transports = bot.rpc.list_transports(accid)
            for t in transports:
                t_addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
                if t_addr: bot_addrs.append(t_addr.lower().strip())
        except: pass
        
        if bot_addrs:
            for args in [(accid, contact_id), (contact_id,)]:
                try:
                    enc_info_self = bot.rpc.get_contact_encryption_info(*args)
                    if enc_info_self:
                        import re
                        blocks = re.split(r'\n\s*\n', enc_info_self.strip())
                        for block in blocks:
                            if any(a in block.lower() for a in bot_addrs):
                                matches = re.findall(r'[0-9a-fA-F]{32,64}', "".join(block.split()).replace(':', ''))
                                self_fps.update(m.upper() for m in matches)
                        break
                except Exception:
                    continue
        if self_fps:
            logger.debug(f"Detected bot's own fingerprints from enc_info: {[f[-8:] for f in self_fps]}")
    except Exception as e:
        logger.error(f"Error detecting self-fingerprint: {e}")

    # 1. Try directly from the contact object if available
    if contact:
        get_val = getattr(contact, 'get', lambda k: getattr(contact, k, None))
        for attr in ['fingerprint', 'key_fingerprint', 'public_key']:
            val = get_val(attr)
            if val:
                import re
                matches = re.findall(r'[0-9a-fA-F]{32,64}', str(val).replace(' ', '').replace(':', ''))
                valid_matches = [m.upper() for m in matches if m.upper() not in self_fps]
                if valid_matches:
                    fps = ",".join(valid_matches)
                    logger.debug(f"Found fingerprint(s) in contact.{attr}: {fps}")
                    return fps

    # 2. Try get_contact_config(accid, contact_id, "fp")
    try:
        fp = bot.rpc.get_contact_config(accid, contact_id, "fp")
        if fp and fp.upper().replace(' ', '') not in self_fps:
            logger.debug(f"Found fingerprint in contact config 'fp': {fp}")
            return fp.upper().replace(' ', '')
    except Exception:
        pass

    # 3. Try get_contact_encryption_info
    for args in [(accid, contact_id), (contact_id,)]:
        try:
            enc_info = bot.rpc.get_contact_encryption_info(*args)
            if enc_info:
                import re
                cleaned_info = "".join(enc_info.split()).replace(':', '')
                matches = re.findall(r'[0-9a-fA-F]{32,64}', cleaned_info)
                valid_matches = [m.upper() for m in matches if m.upper() not in self_fps]
                if valid_matches:
                    fps = ",".join(valid_matches)
                    logger.debug(f"Found fingerprint(s) in encryption info: {fps}")
                    return fps
        except Exception as e:
            logger.debug(f"get_contact_encryption_info{args} failed: {e}")
            continue
            
    return None


def _parse_chat_info_is_private(chat_info) -> bool:
    if isinstance(chat_info, dict):
        chat_type = chat_info.get("chatType") or chat_info.get("chat_type")
        if isinstance(chat_type, str) and chat_type.lower() == "single":
            return True
        type_val = chat_info.get("type")
        if type_val in (1, "1"):
            return True
    else:
        chat_type = getattr(chat_info, "chat_type", None) or getattr(chat_info, "chatType", None)
        if isinstance(chat_type, str) and chat_type.lower() == "single":
            return True
        type_val = getattr(chat_info, "type", None)
        if type_val in (1, "1"):
            return True
    return False


def _is_private_chat(bot, accid, chat_id) -> bool:
    # 1. Try get_basic_chat_info
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
        if chat_info:
            return _parse_chat_info_is_private(chat_info)
    except Exception as e:
        logger.debug(f"get_basic_chat_info failed: {e}")

    # 2. Fallback to get_full_chat_by_id
    try:
        chat_info = bot.rpc.get_full_chat_by_id(accid, chat_id)
        if chat_info:
            return _parse_chat_info_is_private(chat_info)
    except Exception as e:
        logger.debug(f"get_full_chat_by_id failed: {e}")

    # 3. Ultimate fallback: get_chat_contacts length check
    try:
        contacts = bot.rpc.get_chat_contacts(accid, chat_id)
        if isinstance(contacts, list) and len(contacts) == 1:
            return True
    except Exception as e:
        logger.error(f"get_chat_contacts failed: {e}")

    return False


def _is_dc_admin(bot, accid, from_id):
    """Checks if a Delta Chat user is the bot administrator."""
    try:
        # 1. Always load contact object first to avoid race conditions
        contact = None
        try:
            contact = bot.rpc.get_contact(accid, from_id)
        except Exception:
            pass
            
        # 2. Fingerprint check (Secure)
        stored_fingerprint = database.get_config("admin_dc_fingerprint")
        if stored_fingerprint:
            # Try to get fingerprint through improved extraction
            current_fingerprint = _get_contact_fingerprint(bot, accid, from_id, contact=contact)
                            
            logger.debug(f"Admin check (fp): stored={stored_fingerprint}, current={current_fingerprint}")
            if current_fingerprint:
                # current_fingerprint might be a comma-separated list if multiple keys were found
                if stored_fingerprint.upper() in current_fingerprint.upper().split(','):
                    return True
                
            # If fingerprint is set but didn't match, we REJECT even if email matches (security)
            if current_fingerprint:
                 logger.warning(f"Admin fingerprint mismatch for {from_id}")
                 return False

        # 3. Email fallback (only used if fingerprint is not configured OR not found for current user)
        stored_email = database.get_config("admin_dc_email")
        if stored_email and contact:
            email = contact.address.replace(' ', '').lower()
            target = stored_email.replace(' ', '').lower()
            logger.debug(f"Admin check (email): stored={stored_email}, current={email}")
            if email == target:
                return True
                
    except Exception as e:
        logger.error(f"Error during admin verification: {e}")
        
    return False


def _dc_send_msg_with_stats(bot, accid, chat_id, msg_data):
    """Wrapper for bot.rpc.send_msg that tracks stats."""
    try:
        msg_id = bot.rpc.send_msg(accid, chat_id, msg_data)
        
        # Track success
        try:
            addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr") or "unknown"
            if addr != "unknown":
                database.increment_transport_sent(addr)
        except Exception:
            pass
        
        logger.info(f"Successfully sent msg_id {msg_id} to chat {chat_id} on account {accid}")
        return msg_id
    except Exception as e:
        logger.error(f"Failed to send DC message to chat {chat_id} on account {accid}: {e}")
        raise e


async def _userbot_leave_chat(tg_chat_id: int):
    """Make the Userbot leave a chat or channel if no other bridges exist."""
    import bot as _bot_module
    if not (_bot_module.userbot_client and _bot_module.userbot_client.is_connected()):
        return

    # Wait a bit to ensure DB is updated
    await asyncio.sleep(1)

    if database.count_bridges_for_tg(tg_chat_id) > 0:
        logger.debug(f"Userbot: Keeping membership in {tg_chat_id} (other bridges exist).")
        return

    try:
        from telethon.tl.types import Channel, Chat
        from telethon.tl.functions.channels import LeaveChannelRequest
        entity = await asyncio.wait_for(_bot_module.userbot_client.get_entity(tg_chat_id), timeout=15.0)
        if isinstance(entity, Channel):
             await asyncio.wait_for(_bot_module.userbot_client(LeaveChannelRequest(entity)), timeout=15.0)
             logger.info(f"Userbot: Left Telegram channel/supergroup {tg_chat_id}")
        elif isinstance(entity, Chat):
             # For legacy small groups
             me = await asyncio.wait_for(_bot_module.userbot_client.get_me(), timeout=15.0)
             from telethon.tl.functions.messages import DeleteChatUserRequest
             await asyncio.wait_for(_bot_module.userbot_client(DeleteChatUserRequest(chat_id=entity.id, user_id=me.id)), timeout=15.0)
             logger.info(f"Userbot: Left Telegram group {tg_chat_id}")
    except Exception as e:
        logger.debug(f"Userbot: Could not leave chat {tg_chat_id} (maybe already left): {e}")

