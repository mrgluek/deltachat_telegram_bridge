import logging
import threading
from typing import Optional

import database
from app_context import app_ctx

logger = logging.getLogger("tg_dc_bridge")


class EditSyncService:
    def __init__(self):
        self._bot_initiated_dc_deletes: set[int] = set()
        self._bot_initiated_dc_deletes_lock = threading.Lock()

    def register_bot_delete(self, dc_msg_id: int):
        with self._bot_initiated_dc_deletes_lock:
            self._bot_initiated_dc_deletes.add(dc_msg_id)

    def consume_bot_delete(self, dc_msg_id: int) -> bool:
        with self._bot_initiated_dc_deletes_lock:
            if dc_msg_id in self._bot_initiated_dc_deletes:
                self._bot_initiated_dc_deletes.discard(dc_msg_id)
                return True
            return False

    def sync_to_dc(self, bot, accid, dc_chat_id: int, old_tg_msg_id: int, tg_chat_id: int,
                   new_text: str, new_hash: str, sender_name: str = None,
                   override_prefix: str = None) -> Optional[int]:
        if not app_ctx.dc_bot or not app_ctx.dc_accid:
            return None

        old_dc_msg_id = database.get_dc_msg_id(old_tg_msg_id, tg_chat_id, dc_chat_id)
        if old_dc_msg_id:
            try:
                self.register_bot_delete(old_dc_msg_id)
                bot.rpc.delete_messages(accid, [old_dc_msg_id])
            except Exception as del_e:
                logger.debug(f"Could not delete old DC msg {old_dc_msg_id} before edit relay: {del_e}")
                self.consume_bot_delete(old_dc_msg_id)

        from deltachat2 import MsgData
        msg_data = MsgData(text=new_text)
        if override_prefix:
            msg_data.override_sender_name = override_prefix
        elif sender_name:
            msg_data.override_sender_name = sender_name

        from rate_limiter import rate_limiter
        import asyncio
        try:
            loop = app_ctx.main_loop
            if loop and loop.is_running():
                import asyncio
                fut = asyncio.run_coroutine_threadsafe(rate_limiter.wait_global_dc(), loop)
                fut.result(timeout=30)
        except Exception:
            pass

        dc_sent_id = bot.rpc.send_msg(accid, dc_chat_id, msg_data)
        if dc_sent_id:
            database.save_message_map(dc_sent_id, dc_chat_id, old_tg_msg_id, tg_chat_id, content_hash=new_hash)
        return dc_sent_id

    def get_content_hash(self, msg) -> str:
        import hashlib
        text = getattr(msg, 'text', "") or ""
        caption = getattr(msg, 'caption', "") or ""
        content = text or caption or ""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()


edit_sync_service = EditSyncService()
