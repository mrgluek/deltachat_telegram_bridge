import logging
from typing import Optional

from deltachat2 import MsgData

import database
from app_context import app_ctx

logger = logging.getLogger("tg_dc_bridge")


class PermissionChecker:
    def is_dc_admin(self, bot, accid, from_id) -> bool:
        try:
            contact = None
            try:
                contact = bot.rpc.get_contact(accid, from_id)
            except Exception:
                pass

            stored_fingerprint = database.get_config("admin_dc_fingerprint")
            if stored_fingerprint:
                current_fingerprint = self._get_contact_fingerprint(bot, accid, from_id, contact=contact)
                logger.debug(f"Admin check (fp): stored={stored_fingerprint}, current={current_fingerprint}")
                if current_fingerprint:
                    if stored_fingerprint.upper() in current_fingerprint.upper().split(','):
                        return True

                if current_fingerprint:
                    logger.warning(f"Admin fingerprint mismatch for {from_id}")
                    return False

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

    def require_dc_admin(self, bot, accid, event) -> bool:
        msg = event.msg
        if not self.is_dc_admin(bot, accid, msg.from_id):
            from relay import message_relay
            message_relay.dc_send_msg(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use this command."))
            return False
        return True

    def _get_contact_fingerprint(self, bot, accid, contact_id, contact=None):
        self_fps = set()
        try:
            bot_addrs = []
            bot_addr = bot.rpc.get_config(accid, "addr")
            if bot_addr:
                bot_addrs.append(bot_addr.lower().strip())

            try:
                transports = bot.rpc.list_transports(accid)
                for t in transports:
                    t_addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
                    if t_addr:
                        bot_addrs.append(t_addr.lower().strip())
            except Exception:
                pass

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
                logger.info(f"Detected bot's own fingerprints from enc_info: {[f[-8:] for f in self_fps]}")
        except Exception as e:
            logger.error(f"Error detecting self-fingerprint: {e}")

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

        try:
            fp = bot.rpc.get_contact_config(accid, contact_id, "fp")
            if fp and fp.upper().replace(' ', '') not in self_fps:
                logger.debug(f"Found fingerprint in contact config 'fp': {fp}")
                return fp.upper().replace(' ', '')
        except Exception:
            pass

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

    async def check_tg_invite_permissions(self, update) -> bool:
        chat = update.effective_chat
        user = update.effective_user

        admin_tg_id = database.get_config("admin_tg_id")
        if admin_tg_id:
            if not database.is_owner_or_admin(user.id):
                await update.effective_message.reply_text("❌ Only the bot admin can generate invite links.")
                return False

        if chat.type != "private":
            if not admin_tg_id:
                try:
                    member = await chat.get_member(user.id)
                    if member.status not in ("administrator", "creator"):
                        await update.effective_message.reply_text("❌ Only group admins can generate invite links.")
                        return False
                except Exception:
                    pass

        return True

    async def check_channel_admin(self, update) -> bool:
        chat = update.effective_chat
        user = update.effective_user
        if chat.type != "private":
            await update.effective_message.reply_text("❌ Channel commands can only be used in a private chat with the bot.")
            return False
        admin_tg_id = database.get_config("admin_tg_id")
        if admin_tg_id and not database.is_owner_or_admin(user.id):
            await update.effective_message.reply_text("❌ Only the bot admin can manage channels.")
            return False
        return True


permission_checker = PermissionChecker()
