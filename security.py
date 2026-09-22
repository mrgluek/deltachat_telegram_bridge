"""Rate limiting, dedup/cooldown logic, and the message-filter cache.

Pure leaf module except `_wait_for_global_dc_rate_limit`, which reaches
`bot.tg_app` via a function-local `import bot` (never module-level, and
never `from bot import tg_app`) to avoid both a stale-reference bug
(tg_app is reassigned after this module loads) and the self-import
deadlock described in bot.py's run_cli() comment.
"""
import asyncio
import collections
import logging
import re
import threading
import time
from collections import defaultdict
from typing import Optional

import database
from telegram.error import NetworkError, TimedOut

logger = logging.getLogger("tg_dc_bridge")


DC_FALLBACK_PATTERN = re.compile(r'\s*\[(?:Image|Video|Voice|Audio|Document|File|Sticker|Gif)[ \-–]+[^\]]+\]', re.IGNORECASE)


RATE_LIMIT_WINDOW = 60   # seconds


RATE_LIMIT_MAX = 30       # max messages per window per chat


_filter_cache: list[str] = []


_filter_regex: Optional[re.Pattern] = None


_filter_cache_lock = threading.Lock()


def _reload_filter_cache():
    """Reload active filter patterns into memory and compile into a unified regex."""
    global _filter_cache, _filter_regex
    with _filter_cache_lock:
        try:
            patterns = database.get_all_filter_patterns()
            _filter_cache = [p.lower() for p in patterns]
            if _filter_cache:
                sorted_pats = sorted(_filter_cache, key=len, reverse=True)
                escaped = [re.escape(p) for p in sorted_pats]
                _filter_regex = re.compile("|".join(escaped), re.IGNORECASE)
            else:
                _filter_regex = None
        except Exception as e:
            logger.error(f"Failed to reload filter cache: {e}")


def is_text_filtered(text: str | None) -> tuple[bool, str | None]:
    """Check if text contains any configured filter pattern (case-insensitive) via compiled regex."""
    if not isinstance(text, str) or not text:
        return False, None
    with _filter_cache_lock:
        regex = _filter_regex
    if not regex:
        return False, None
    m = regex.search(text)
    if m:
        return True, m.group(0).lower()
    return False, None


GLOBAL_DC_RATE_LIMIT = 60    # messages


GLOBAL_DC_RATE_WINDOW = 60   # seconds


_global_dc_send_times = collections.deque()


_global_dc_rate_limit_lock = asyncio.Lock()


_global_wait_counter = 0


_last_owner_notification_time = 0


async def _wait_for_global_dc_rate_limit():
    """Ensures we don't exceed the global Delta Chat message rate limit."""
    import bot as _bot_module
    global _global_dc_send_times, _global_wait_counter, _last_owner_notification_time
    
    _global_wait_counter += 1
    try:
        async with _global_dc_rate_limit_lock:
            now = time.time()
            # Remove timestamps older than the window
            while _global_dc_send_times and now - _global_dc_send_times[0] > GLOBAL_DC_RATE_WINDOW:
                _global_dc_send_times.popleft()
            
            if len(_global_dc_send_times) >= GLOBAL_DC_RATE_LIMIT:
                # We hit the limit, wait until the oldest one expires
                wait_time = GLOBAL_DC_RATE_WINDOW - (now - _global_dc_send_times[0])
                if wait_time > 0:
                    # Notify owner on Telegram (debounced to once per minute)
                    admin_tg_id = database.get_config("admin_tg_id")
                    if admin_tg_id and _bot_module.tg_app and (now - _last_owner_notification_time > 60):
                        _last_owner_notification_time = now
                        queue_size = _global_wait_counter - 1 # Current task is waiting on the lock
                        notification = (
                            f"⏳ **Global DC Rate Limit Enforced**\n"
                            f"The bot is waiting {wait_time:.1f}s before sending next message.\n"
                            f"Current queue: {queue_size} messages waiting."
                        )
                        # Send notification in background to not block the relay
                        asyncio.create_task(_bot_module.tg_app.bot.send_message(chat_id=admin_tg_id, text=notification, parse_mode='Markdown'))
                    
                    logger.info(f"Global DC rate limit reached. Waiting {wait_time:.2f}s (Queue: {_global_wait_counter-1})...")
                    await asyncio.sleep(wait_time + 0.1)
                    # Re-clean after sleep
                    now = time.time()
                    while _global_dc_send_times and now - _global_dc_send_times[0] > GLOBAL_DC_RATE_WINDOW:
                        _global_dc_send_times.popleft()
            
            _global_dc_send_times.append(time.time())
    finally:
        _global_wait_counter -= 1


_processed_tg_msgs: dict[tuple[int, int, str], float] = {}


DELETE_SYNC_MAX = 10          # max messages to auto-delete


DELETE_SYNC_WINDOW = 60       # seconds


_deletion_sync_times: list[float] = []


_deletion_sync_lock = threading.Lock()


_bot_initiated_dc_deletes: set[int] = set()


_bot_initiated_dc_deletes_lock = threading.Lock()


def _register_bot_initiated_delete(dc_msg_id: int):
    """Mark a DC message as being deleted by the bot (e.g. edit replacement). Thread-safe."""
    with _bot_initiated_dc_deletes_lock:
        _bot_initiated_dc_deletes.add(dc_msg_id)


def _consume_bot_initiated_delete(dc_msg_id: int) -> bool:
    """Returns True (and removes the mark) if this deletion was bot-initiated."""
    with _bot_initiated_dc_deletes_lock:
        if dc_msg_id in _bot_initiated_dc_deletes:
            _bot_initiated_dc_deletes.discard(dc_msg_id)
            return True
        return False


def _is_deletion_rate_limited() -> bool:
    """Returns True if too many deletions have been synced recently (safety guard)."""
    with _deletion_sync_lock:
        now = time.time()
        _deletion_sync_times[:] = [t for t in _deletion_sync_times if now - t < DELETE_SYNC_WINDOW]
        if len(_deletion_sync_times) >= DELETE_SYNC_MAX:
            return True
        _deletion_sync_times.append(now)
        return False


def _mark_processed(chat_id: int, msg_id: int, event_type: str = 'new') -> bool:
    """Returns True if this message event was already processed in the last 120 seconds."""
    now = time.time()
    key = (chat_id, msg_id, event_type)
    last = _processed_tg_msgs.get(key, 0)
    if now - last < 120:
        return True
    _processed_tg_msgs[key] = now
    if len(_processed_tg_msgs) > 2000:
        cutoff = now - 120
        keys_to_delete = [k for k, v in _processed_tg_msgs.items() if v < cutoff]
        for k in keys_to_delete:
            del _processed_tg_msgs[k]
    return False


async def retry_async(coro_func, *args, max_retries=5, delay=2.0, backoff=2.0, exceptions=(TimeoutError, asyncio.TimeoutError, ConnectionError, OSError, NetworkError, TimedOut), **kwargs):
    """Retries an async function with exponential backoff."""
    last_exception = None
    for attempt in range(max_retries):
        try:
            return await coro_func(*args, **kwargs)
        except exceptions as e:
            last_exception = e
            if attempt == max_retries - 1:
                break
            wait = delay * (backoff ** attempt)
            logger.warning(f"Operation failed with {type(e).__name__}, retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(wait)
    raise last_exception


_rate_limits: dict[int, list[float]] = defaultdict(list)


def _is_rate_limited(chat_id: int) -> bool:
    """Returns True if this chat has exceeded the rate limit."""
    now = time.time()
    timestamps = _rate_limits[chat_id]
    # Remove old entries outside the window
    _rate_limits[chat_id] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limits[chat_id]) >= RATE_LIMIT_MAX:
        return True
    _rate_limits[chat_id].append(now)
    return False


EDIT_DEBOUNCE_SECONDS = 60


_edit_timestamps: dict[tuple[int, int], float] = {}


def _is_edit_debounced(chat_id: int, msg_id: int) -> bool:
    """Returns True if an edit for this message was relayed too recently."""
    now = time.time()
    key = (chat_id, msg_id)
    last = _edit_timestamps.get(key, 0)
    if now - last < EDIT_DEBOUNCE_SECONDS:
        return True
    _edit_timestamps[key] = now
    # Purge old entries periodically
    if len(_edit_timestamps) > 500:
        _edit_timestamps.clear()  # Simple cleanup
    return False


_reload_filter_cache()
