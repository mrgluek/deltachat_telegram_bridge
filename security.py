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


# Set when a new waiter arrives, so a group message doesn't sit out the
# dispatcher's pause while channel posts are held back.
_dc_send_wakeup: Optional[asyncio.Event] = None


# The core sends its SMTP queue strictly in order, and after a relay rejection
# it backs off with the rest of the queue waiting. A command reply sent while
# hundreds of channel posts sit in that queue arrives many minutes late. So
# channel posts are only handed to the core while few of the bridge's own
# messages are still pending there; group messages and command replies are
# never held back.
CORE_OUTBOX_MAX_PENDING = 8


CORE_OUTBOX_TRACK_TTL = 600   # stop waiting for a message the core sits on longer than this


_core_outbox: collections.deque = collections.deque()   # (dc_msg_id, sent_at)


# DC message states that mean "still in the core's outgoing queue".
_DC_STATE_OUT_PREPARING = 18


_DC_STATE_OUT_PENDING = 20


def track_dc_outgoing(dc_msg_id) -> None:
    """Remember a bridge message handed to the core, to measure the core's outbox."""
    if isinstance(dc_msg_id, int) and dc_msg_id > 0:
        _core_outbox.append((dc_msg_id, time.monotonic()))


def _core_outbox_pending_count() -> int:
    """How many tracked messages the core has not sent yet. Blocking RPC; run in a thread."""
    import bot as _bot_module
    now = time.monotonic()
    dc = _bot_module.dc_bot_instance
    keep = []
    for msg_id, sent_at in list(_core_outbox):
        if now - sent_at > CORE_OUTBOX_TRACK_TTL:
            continue
        try:
            state = dc.rpc.get_message(_bot_module.dc_accid, msg_id).state
        except Exception:
            continue  # deleted or unreadable: not blocking anything
        if state in (_DC_STATE_OUT_PREPARING, _DC_STATE_OUT_PENDING):
            keep.append((msg_id, sent_at))
    _core_outbox.clear()
    _core_outbox.extend(keep)
    return len(keep)


async def _core_outbox_has_room() -> bool:
    global _core_outbox_full
    if len(_core_outbox) < CORE_OUTBOX_MAX_PENDING:
        _core_outbox_full = False
        return True
    try:
        pending = await asyncio.to_thread(_core_outbox_pending_count)
    except Exception as e:
        logger.warning(f"Could not check the core outbox: {e}")
        return True
    _core_outbox_full = pending >= CORE_OUTBOX_MAX_PENDING
    return not _core_outbox_full


# Send counters since the last flush_send_stats(); stored hourly for /status.
_send_stats = {"group": 0, "channel": 0, "rate_wait": 0.0, "outbox_wait": 0.0}


_core_outbox_full = False   # result of the last outbox check, shown in /status


def get_dc_backlog() -> int:
    """Messages the bridge still has to hand to the core (both priorities)."""
    waiting = get_dc_send_waiting()
    try:
        import userbot as _userbot_module
        pending = _userbot_module.get_pending_event_count()
    except Exception:
        pending = 0
    return max(pending, waiting[PRIORITY_CHANNEL]) + waiting[PRIORITY_GROUP]


def flush_send_stats() -> None:
    """Write the counters collected since the last call into the hourly table."""
    snap = dict(_send_stats)
    for k in _send_stats:
        _send_stats[k] = 0 if isinstance(_send_stats[k], int) else 0.0
    try:
        database.record_send_stats(snap["group"], snap["channel"], snap["rate_wait"], snap["outbox_wait"], get_dc_backlog())
    except Exception as e:
        logger.warning(f"Could not store send stats: {e}")


def get_core_outbox_pending() -> int:
    """Bridge messages the core has not sent yet. Blocking RPC; call from a thread."""
    return _core_outbox_pending_count()


def is_core_outbox_full() -> bool:
    return _core_outbox_full


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


def _notify_owner_about_backlog() -> None:
    """Tell the owner about a real send backlog, at most every RATE_LIMIT_NOTIFY_INTERVAL."""
    import bot as _bot_module
    global _last_owner_notification_time
    now = time.time()
    if now - _last_owner_notification_time <= RATE_LIMIT_NOTIFY_INTERVAL:
        return
    waiting = get_dc_send_waiting()
    # Most of a channel backlog sits in the per-channel userbot queues, not here.
    backlog = get_dc_backlog()
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


def _first_live_waiter(prio: int) -> Optional[asyncio.Future]:
    q = _dc_send_waiters[prio]
    while q and q[0].done():
        q.popleft()
    return q[0] if q else None


async def _dc_send_dispatch_loop():
    """Hand out send tokens one by one while anyone is waiting, then exit."""
    global _dc_send_tokens, _dc_send_dispatcher
    rate = GLOBAL_DC_RATE_LIMIT / GLOBAL_DC_RATE_WINDOW
    try:
        while True:
            group = _first_live_waiter(PRIORITY_GROUP)
            channel = _first_live_waiter(PRIORITY_CHANNEL)
            if group is None and channel is None:
                break
            _refill_dc_send_tokens()
            if _dc_send_tokens < 1:
                _notify_owner_about_backlog()
                pause = (1 - _dc_send_tokens) / rate
                _send_stats["rate_wait"] += pause
                await asyncio.sleep(pause)
                continue
            if group is not None:
                fut = _dc_send_waiters[PRIORITY_GROUP].popleft()
            else:
                # Channel posts also wait until the core has worked off its outbox.
                if not await _core_outbox_has_room():
                    _notify_owner_about_backlog()
                    _dc_send_wakeup.clear()
                    held_since = time.monotonic()
                    try:
                        await asyncio.wait_for(_dc_send_wakeup.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    _send_stats["outbox_wait"] += time.monotonic() - held_since
                    continue
                if channel.done():
                    continue
                fut = _dc_send_waiters[PRIORITY_CHANNEL].popleft()
            _dc_send_tokens -= 1
            _send_stats["group" if group is not None else "channel"] += 1
            _global_dc_send_times.append(time.time())
            fut.set_result(None)
    finally:
        _dc_send_dispatcher = None


async def _wait_for_global_dc_rate_limit(priority: int = PRIORITY_GROUP):
    """Wait for a slot to send one Delta Chat message.

    Sends are paced to GLOBAL_DC_RATE_LIMIT per window with bursts of at most
    DC_SEND_BURST. Waiters of the same priority go in arrival order (keeps the
    order of messages within a chat); PRIORITY_GROUP always goes before
    PRIORITY_CHANNEL.
    """
    global _dc_send_dispatcher, _dc_send_wakeup
    fut = asyncio.get_running_loop().create_future()
    _dc_send_waiters[priority].append(fut)
    if _dc_send_dispatcher is None or _dc_send_dispatcher.done():
        # Created here so it belongs to the running event loop.
        _dc_send_wakeup = asyncio.Event()
        _dc_send_dispatcher = asyncio.create_task(_dc_send_dispatch_loop())
    else:
        _dc_send_wakeup.set()
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
