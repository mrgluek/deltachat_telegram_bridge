import time
import asyncio
import threading
from collections import defaultdict, deque
from typing import Optional

from constants import RATE_LIMIT_WINDOW, RATE_LIMIT_MAX, GLOBAL_DC_RATE_LIMIT, GLOBAL_DC_RATE_WINDOW, HISTORY_RELAY_COOLDOWN, DELETE_SYNC_MAX, DELETE_SYNC_WINDOW, EDIT_DEBOUNCE_SECONDS
from app_context import app_ctx


class RateLimiter:
    def __init__(self):
        self._rate_limits: dict[int, list[float]] = defaultdict(list)
        self._global_dc_send_times: deque[float] = deque()
        self._global_dc_rate_limit_lock = asyncio.Lock()
        self._global_wait_counter = 0
        self._last_owner_notification_time = 0
        self._deletion_sync_times: list[float] = []
        self._deletion_sync_lock = threading.Lock()
        self._edit_timestamps: dict[tuple[int, int], float] = {}
        self._history_cooldowns: dict[int, float] = {}
        self._processed_tg_msgs: dict[tuple[int, int], float] = {}

    def check_per_chat(self, chat_id: int) -> bool:
        now = time.time()
        timestamps = self._rate_limits[chat_id]
        self._rate_limits[chat_id] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
        if len(self._rate_limits[chat_id]) >= RATE_LIMIT_MAX:
            return True
        self._rate_limits[chat_id].append(now)
        return False

    async def wait_global_dc(self):
        self._global_wait_counter += 1
        try:
            async with self._global_dc_rate_limit_lock:
                now = time.time()
                while self._global_dc_send_times and now - self._global_dc_send_times[0] > GLOBAL_DC_RATE_WINDOW:
                    self._global_dc_send_times.popleft()

                if len(self._global_dc_send_times) >= GLOBAL_DC_RATE_LIMIT:
                    wait_time = GLOBAL_DC_RATE_WINDOW - (now - self._global_dc_send_times[0])
                    if wait_time > 0:
                        import database
                        admin_tg_id = database.get_config("admin_tg_id")
                        if admin_tg_id and app_ctx.tg_app and (now - self._last_owner_notification_time > 60):
                            self._last_owner_notification_time = now
                            queue_size = self._global_wait_counter - 1
                            notification = (
                                f"⏳ **Global DC Rate Limit Enforced**\n"
                                f"The bot is waiting {wait_time:.1f}s before sending next message.\n"
                                f"Current queue: {queue_size} messages waiting."
                            )
                            asyncio.create_task(app_ctx.tg_app.bot.send_message(chat_id=int(admin_tg_id), text=notification, parse_mode='Markdown'))

                        import logging
                        logging.getLogger("tg_dc_bridge").info(f"Global DC rate limit reached. Waiting {wait_time:.2f}s (Queue: {self._global_wait_counter-1})...")
                        await asyncio.sleep(wait_time + 0.1)
                        now = time.time()
                        while self._global_dc_send_times and now - self._global_dc_send_times[0] > GLOBAL_DC_RATE_WINDOW:
                            self._global_dc_send_times.popleft()

                self._global_dc_send_times.append(time.time())
        finally:
            self._global_wait_counter -= 1

    def check_deletion_sync(self) -> bool:
        with self._deletion_sync_lock:
            now = time.time()
            self._deletion_sync_times[:] = [t for t in self._deletion_sync_times if now - t < DELETE_SYNC_WINDOW]
            if len(self._deletion_sync_times) >= DELETE_SYNC_MAX:
                return True
            self._deletion_sync_times.append(now)
            return False

    def check_edit_debounce(self, chat_id: int, msg_id: int) -> bool:
        now = time.time()
        key = (chat_id, msg_id)
        last = self._edit_timestamps.get(key, 0)
        if now - last < EDIT_DEBOUNCE_SECONDS:
            return True
        self._edit_timestamps[key] = now
        if len(self._edit_timestamps) > 500:
            self._edit_timestamps.clear()
        return False

    def register_edit_timestamp(self, chat_id: int, msg_id: int):
        self._edit_timestamps[(chat_id, msg_id)] = time.time()

    def check_history_cooldown(self, dc_chat_id: int) -> bool:
        now = time.time()
        last = self._history_cooldowns.get(dc_chat_id, 0)
        if now - last < HISTORY_RELAY_COOLDOWN:
            return True
        self._history_cooldowns[dc_chat_id] = now
        return False

    def register_history_cooldown(self, dc_chat_id: int):
        self._history_cooldowns[dc_chat_id] = time.time()

    def check_dedup(self, chat_id: int, msg_id: int, ttl: int = 120) -> bool:
        now = time.time()
        key = (chat_id, msg_id)
        last = self._processed_tg_msgs.get(key, 0)
        if now - last < ttl:
            return True
        self._processed_tg_msgs[key] = now
        if len(self._processed_tg_msgs) > 2000:
            cutoff = now - ttl
            keys_to_delete = [k for k, v in self._processed_tg_msgs.items() if v < cutoff]
            for k in keys_to_delete:
                del self._processed_tg_msgs[k]
        return False

    def clear_caches(self, dc_chat_id: int):
        self._history_cooldowns.pop(dc_chat_id, None)
        keys_to_remove = [k for k in self._edit_timestamps if k[0] == dc_chat_id]
        for k in keys_to_remove:
            self._edit_timestamps.pop(k, None)


rate_limiter = RateLimiter()
