"""Track active Telegram live locations by chat and message."""
import time

LIVE_LOCATIONS: dict[tuple[int, int], tuple[float, float]] = {}
_expirations: dict[tuple[int, int], float] = {}
_MAX_AGE = 48 * 3600


def _expire_locations():
    now = time.time()
    for key, expiry in list(_expirations.items()):
        if expiry <= now:
            clear_live_location(*key)


def get_live_location(tg_chat_id: int, message_id: int):
    """Return active coordinates for this chat's message, or None."""
    _expire_locations()
    return LIVE_LOCATIONS.get((tg_chat_id, message_id))


def set_live_location(tg_chat_id: int, message_id: int, lat: float, lon: float,
                      live_period: int = _MAX_AGE, started_at: float = None) -> None:
    _expire_locations()
    key = (tg_chat_id, message_id)
    expiry = (started_at if started_at is not None else time.time()) + min(live_period, _MAX_AGE)
    _expirations[key] = min(_expirations.get(key, expiry), expiry)
    LIVE_LOCATIONS[key] = (lat, lon)
    _expire_locations()


def clear_live_location(tg_chat_id: int, message_id: int) -> None:
    key = (tg_chat_id, message_id)
    LIVE_LOCATIONS.pop(key, None)
    _expirations.pop(key, None)
