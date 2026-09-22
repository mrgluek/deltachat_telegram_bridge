"""In-memory caches: channel report cache, TG-channel<->DC-chat id cache,
last-relayed-message-id cache, channel history cache/cooldown, media-group
dedup, and content-hash comparison helpers.

Pure leaf module: no dependency on DC/TG handlers or the pervasive bot.py
singletons. get_cached_channels_text/set_cached_channels_text and
set_history_cache/mark_history_cooldown are new accessor functions added
so tg_channels_command and _relay_channel_history (moved in later
extraction steps) stop mutating these dicts directly from other modules.
"""
import hashlib
import logging
import os
import threading
import time
from typing import Optional

import database

logger = logging.getLogger("tg_dc_bridge")

def invalidate_channels_cache():
    global _channels_cache
    _channels_cache.clear()


_channels_cache: dict[int, tuple[str, float]] = {}


_CHANNELS_CACHE_TTL = 600.0  # 10 minutes


def _get_cached_dc_channel_chat_id(tg_channel_id: int) -> int | None:
    v1, v2 = database._normalize_tg_id_variants(tg_channel_id)
    with _tg_channel_dc_id_lock:
        if v1 in _tg_channel_dc_id_cache:
            return _tg_channel_dc_id_cache[v1]
        if v2 in _tg_channel_dc_id_cache:
            return _tg_channel_dc_id_cache[v2]
    
    dc_id = database.get_dc_channel_chat_id(tg_channel_id)
    if dc_id:
        with _tg_channel_dc_id_lock:
            _tg_channel_dc_id_cache[v1] = dc_id
            _tg_channel_dc_id_cache[v2] = dc_id
    return dc_id


def _invalidate_dc_channel_cache(tg_channel_id: Optional[int] = None):
    with _tg_channel_dc_id_lock:
        if tg_channel_id is None:
            _tg_channel_dc_id_cache.clear()
        else:
            v1, v2 = database._normalize_tg_id_variants(tg_channel_id)
            _tg_channel_dc_id_cache.pop(v1, None)
            _tg_channel_dc_id_cache.pop(v2, None)


_tg_channel_dc_id_cache: dict[int, int] = {}


_tg_channel_dc_id_lock = threading.Lock()


def _get_cached_last_msg_id(tg_channel_id: int) -> int:
    """Get last relayed post ID from cache, falling back to database."""
    v1, v2 = database._normalize_tg_id_variants(tg_channel_id)
    with _last_msg_id_cache_lock:
        if v1 in _last_msg_id_cache:
            return _last_msg_id_cache[v1]
        if v2 in _last_msg_id_cache:
            return _last_msg_id_cache[v2]
    
    # Fallback to database
    val = database.get_channel_last_msg_id(tg_channel_id)
    
    with _last_msg_id_cache_lock:
        _last_msg_id_cache[v1] = val
        _last_msg_id_cache[v2] = val
    return val


def _update_cached_last_msg_id(tg_channel_id: int, msg_id: int):
    """Update last relayed post ID in memory cache and database."""
    v1, v2 = database._normalize_tg_id_variants(tg_channel_id)
    with _last_msg_id_cache_lock:
        current = max(_last_msg_id_cache.get(v1, 0), _last_msg_id_cache.get(v2, 0))
        if msg_id > current:
            _last_msg_id_cache[v1] = msg_id
            _last_msg_id_cache[v2] = msg_id
    
    database.update_channel_last_msg_id(tg_channel_id, msg_id)


def _warm_last_msg_id_cache():
    """Load all channels and populate the last_msg_id cache."""
    try:
        channels = database.get_all_channels()
        with _last_msg_id_cache_lock:
            for ch in channels:
                tg_id = ch.get('tg_channel_id')
                last_msg_id = ch.get('last_msg_id', 0) or 0
                if tg_id:
                    v1, v2 = database._normalize_tg_id_variants(tg_id)
                    _last_msg_id_cache[v1] = last_msg_id
                    _last_msg_id_cache[v2] = last_msg_id
    except Exception as e:
        logger.error(f"Error warming last_msg_id cache: {e}")


_last_msg_id_cache: dict[int, int] = {}


_last_msg_id_cache_lock = threading.Lock()


def _clear_dc_caches(dc_chat_id: int):
    """Clear in-memory caches for a specific Delta Chat chat."""
    _history_cooldowns.pop(dc_chat_id, None)
    _history_cache.pop(dc_chat_id, None)
    logger.debug(f"Cleared in-memory caches for DC chat {dc_chat_id}")


_history_cooldowns: dict[int, float] = {}


_history_cache: dict[int, dict] = {}


HISTORY_RELAY_COOLDOWN = 300  # 5 minutes


def _is_history_on_cooldown(dc_chat_id: int) -> bool:
    """Returns True if history relay for this chat is on cooldown."""
    now = time.time()
    last = _history_cooldowns.get(dc_chat_id, 0)
    if now - last < HISTORY_RELAY_COOLDOWN:
        return True
    _history_cooldowns[dc_chat_id] = now
    return False


def _is_media_group_processed(group_id: str | int | None) -> bool:
    """Check if a media group / album has already been processed (in memory or persistent DB)."""
    if not group_id:
        return False
    group_str = str(group_id)
    now = time.time()
    stale = [k for k, t in _processed_media_groups.items() if now - t > 3600]
    for k in stale:
        _processed_media_groups.pop(k, None)
    if group_str in _processed_media_groups:
        return True
    if database.is_media_group_processed(group_str):
        _processed_media_groups[group_str] = now
        return True
    _processed_media_groups[group_str] = now
    return False


_processed_media_groups: dict[str, float] = {}


def _get_content_hash(msg) -> str:
    """Return a SHA-256 hash of the message content (text or caption)."""
    # Safe access for both PTB and Telethon objects
    text = getattr(msg, 'message', "") or getattr(msg, 'text', "") or getattr(msg, 'raw_text', "") or ""
    caption = getattr(msg, 'caption', "") or ""
    content = str(text or caption or "")
    paid = getattr(msg, 'paid_media', None)
    if paid:
        content += f"_paid_{getattr(paid, 'star_count', 0)}"
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def files_are_identical(file1: str, file2: str) -> bool:
    if not file1 or not file2:
        return False
    if not os.path.exists(file1) or not os.path.exists(file2):
        return False
    # Check sizes first for speed
    if os.path.getsize(file1) != os.path.getsize(file2):
        return False
    # Check contents
    try:
        with open(file1, "rb") as f1, open(file2, "rb") as f2:
            return f1.read() == f2.read()
    except Exception:
        return False


def get_cached_channels_text(user_id: int) -> Optional[str]:
    """Return the cached /channels report for user_id if still within the TTL, else None."""
    entry = _channels_cache.get(user_id)
    if not entry:
        return None
    text, cached_time = entry
    if (time.time() - cached_time) >= _CHANNELS_CACHE_TTL:
        return None
    return text


def set_cached_channels_text(user_id: int, text: str) -> None:
    _channels_cache[user_id] = (text, time.time())


def set_history_cache(dc_chat_id: int, messages) -> None:
    _history_cache[dc_chat_id] = {'timestamp': time.time(), 'messages': messages}


def mark_history_cooldown(dc_chat_id: int) -> None:
    _history_cooldowns[dc_chat_id] = time.time()


_warm_last_msg_id_cache()
