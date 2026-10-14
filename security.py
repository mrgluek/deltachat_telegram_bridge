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


# Outgoing Delta Chat sends are paced by a token bucket, not a sliding window.
# chatmail relays limit each sender with GCRA: max_user_send_per_minute = 60
# and max_user_send_burst_size = 10. The old "60 per 60 s window" let up to 60
# messages out at once, so the relay answered "450 4.7.1 Too much mail" after
# the 10th and the core had to retry. 50/min (one every 1.2 s) with a burst of
# 5 leaves headroom for the core's own SMTP traffic (sync messages, receipts).
GLOBAL_DC_RATE_LIMIT = 50    # messages


GLOBAL_DC_RATE_WINDOW = 60   # seconds


DC_SEND_BURST = 5            # sends allowed back-to-back after an idle period


# Two queues: bridged groups are conversations and go first; broadcast channel
# posts (including catchup backlogs of hundreds of posts) wait behind them.
PRIORITY_GROUP = 0


PRIORITY_CHANNEL = 1


_dc_send_waiters: dict[int, collections.deque] = {
    PRIORITY_GROUP: collections.deque(),
    PRIORITY_CHANNEL: collections.deque(),
}


_dc_send_tokens: float = float(DC_SEND_BURST)


_dc_send_tokens_ts: float = time.monotonic()


_dc_send_dispatcher: Optional[asyncio.Task] = None


# Recent send timestamps, kept for diagnostics.
_global_dc_send_times = collections.deque(maxlen=GLOBAL_DC_RATE_LIMIT)


_last_owner_notification_time = 0


RATE_LIMIT_NOTIFY_MIN_BACKLOG = 30   # posts waiting before the owner is told


RATE_LIMIT_NOTIFY_INTERVAL = 900     # seconds between owner notifications


def get_dc_send_waiting() -> dict[int, int]:
    """Number of sends currently waiting for a token, per priority."""
    return {prio: sum(1 for f in q if not f.done()) for prio, q in _dc_send_waiters.items()}


def _refill_dc_send_tokens() -> None:
    global _dc_send_tokens, _dc_send_tokens_ts
    now = time.monotonic()
    rate = GLOBAL_DC_RATE_LIMIT / GLOBAL_DC_RATE_WINDOW
    _dc_send_tokens = min(float(DC_SEND_BURST), _dc_send_tokens + (now - _dc_send_tokens_ts) * rate)
    _dc_send_tokens_ts = now


def _next_dc_send_waiter() -> Optional[asyncio.Future]:
    """Oldest live waiter, group queue first. Drops waiters that were cancelled."""
    for prio in (PRIORITY_GROUP, PRIORITY_CHANNEL):
        q = _dc_send_waiters[prio]
        while q and q[0].done():
            q.popleft()
        if q:
            return q.popleft()
    return None


def _notify_owner_about_backlog() -> None:
    """Tell the owner about a real send backlog, at most every RATE_LIMIT_NOTIFY_INTERVAL."""
    import bot as _bot_module
    global _last_owner_notification_time
    now = time.time()
    if now - _last_owner_notification_time <= RATE_LIMIT_NOTIFY_INTERVAL:
        return
    waiting = get_dc_send_waiting()
    # Most of a channel backlog sits in the per-channel userbot queues, not here.
    try:
        import userbot as _userbot_module
        pending = _userbot_module.get_pending_event_count()
    except Exception:
        pending = 0
    backlog = max(pending, waiting[PRIORITY_CHANNEL]) + waiting[PRIORITY_GROUP]
    if backlog < RATE_LIMIT_NOTIFY_MIN_BACKLOG:
        return
    admin_tg_id = database.get_config("admin_tg_id")
    if not (admin_tg_id and _bot_module.tg_app):
        return
    _last_owner_notification_time = now
    eta_min = max(1, round(backlog * GLOBAL_DC_RATE_WINDOW / GLOBAL_DC_RATE_LIMIT / 60))
    notification = (
        f"⏳ *Delta Chat send limit reached* ({GLOBAL_DC_RATE_LIMIT} msgs / {GLOBAL_DC_RATE_WINDOW}s)\n"
        f"{backlog} posts are waiting, sending them takes ~{eta_min} min.\n"
        f"Bridged groups are sent first, channel posts after them. Nothing is lost."
    )
    logger.info(f"DC send backlog: {backlog} waiting (groups: {waiting[PRIORITY_GROUP]}, channel events: {backlog - waiting[PRIORITY_GROUP]}).")
    asyncio.create_task(_bot_module.tg_app.bot.send_message(chat_id=admin_tg_id, text=notification, parse_mode='Markdown'))


async def _dc_send_dispatch_loop():
    """Hand out send tokens one by one while anyone is waiting, then exit."""
    global _dc_send_tokens, _dc_send_dispatcher
    try:
        while any(_dc_send_waiters.values()):
            _refill_dc_send_tokens()
            if _dc_send_tokens >= 1:
                fut = _next_dc_send_waiter()
                if fut is None:
                    continue
                _dc_send_tokens -= 1
                _global_dc_send_times.append(time.time())
                fut.set_result(None)
                continue
            _notify_owner_about_backlog()
            rate = GLOBAL_DC_RATE_LIMIT / GLOBAL_DC_RATE_WINDOW
            await asyncio.sleep((1 - _dc_send_tokens) / rate)
    finally:
        _dc_send_dispatcher = None


async def _wait_for_global_dc_rate_limit(priority: int = PRIORITY_GROUP):
    """Wait for a slot to send one Delta Chat message.

    Sends are paced to GLOBAL_DC_RATE_LIMIT per window with bursts of at most
    DC_SEND_BURST. Waiters of the same priority go in arrival order (keeps the
    order of messages within a chat); PRIORITY_GROUP always goes before
    PRIORITY_CHANNEL.
    """
    global _dc_send_dispatcher
    fut = asyncio.get_running_loop().create_future()
    _dc_send_waiters[priority].append(fut)
    if _dc_send_dispatcher is None or _dc_send_dispatcher.done():
        _dc_send_dispatcher = asyncio.create_task(_dc_send_dispatch_loop())
    try:
        await fut
    except asyncio.CancelledError:
        # A cancelled waiter is skipped by the dispatcher (fut.done() is True).
        fut.cancel()
        raise


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
