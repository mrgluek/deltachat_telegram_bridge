import asyncio
import logging
import time
from typing import Any, Optional

from app_context import app_ctx
from rate_limiter import rate_limiter
from constants import HISTORY_RELAY_COOLDOWN

logger = logging.getLogger("tg_dc_bridge")


class ChannelHistoryService:
    def __init__(self):
        self._history_cache: dict[int, dict] = {}

    async def _resolve_entity(self, userbot_client, ub_target, invite_link):
        try:
            return await userbot_client.get_entity(ub_target)
        except Exception as e:
            if invite_link and ("t.me/" in invite_link or "telegram.me/" in invite_link):
                logger.debug(f"Could not resolve {ub_target}, trying invite link: {e}")
                return await userbot_client.get_entity(invite_link)
            raise e

    async def _do_relay_history(self, dc_chat_id: int, tg_channel_id: int, ub_target: Any,
                                limit: int, invite_link: str, cache_key, send_header=False,
                                bot_ref=None, accid=None):
        userbot_client = app_ctx.userbot_client
        if not (userbot_client and userbot_client.is_connected()):
            return

        try:
            now = time.time()
            cached = self._history_cache.get(cache_key)

            if cached and (now - cached['timestamp'] < HISTORY_RELAY_COOLDOWN):
                history = cached['messages']
            else:
                ub_entity = await self._resolve_entity(userbot_client, ub_target, invite_link)
                if not ub_entity:
                    return
                history = await userbot_client.get_messages(ub_entity, limit=limit)
                if history:
                    self._history_cache[cache_key] = {'timestamp': now, 'messages': history}

            if history:
                if send_header and bot_ref and accid:
                    from deltachat2 import MsgData
                    bot_ref.rpc.send_msg(accid, dc_chat_id, MsgData(text="📜 *Recent posts from this channel:*"))
                from relay import message_relay
                for msg in reversed(history):
                    rate_limiter.check_dedup(tg_channel_id, msg.id)
                    await asyncio.sleep(0.5)
                    await message_relay.relay_userbot_message(dc_chat_id, msg)
        except Exception as e:
            logger.error(f"Failed to relay history for TG {ub_target} to DC {dc_chat_id}: {e}")

    async def relay_channel_history(self, dc_chat_id: int, tg_channel_id: int, ub_target: Any,
                                    limit: int = 3, invite_link: str = None):
        await self._do_relay_history(dc_chat_id, tg_channel_id, ub_target, limit, invite_link,
                                     cache_key=dc_chat_id)

    async def relay_history_to_chat(self, target_dc_chat_id: int, tg_channel_id: int, ub_target: Any,
                                    limit: int = 3, invite_link: str = None, bot_ref=None, accid=None):
        await self._do_relay_history(target_dc_chat_id, tg_channel_id, ub_target, limit, invite_link,
                                     cache_key=f"preview_{tg_channel_id}", send_header=True,
                                     bot_ref=bot_ref, accid=accid)


channel_history_service = ChannelHistoryService()
