"""Tracks Telegram live-location messages so their DC-side echoes can be
updated in place as new position updates arrive.

Pure leaf module. LIVE_LOCATIONS previously had zero accessor functions
and was written directly from four separate Telegram-side handlers and
read directly from a Delta Chat command handler with no encapsulation
at all. get_live_location/set_live_location/clear_live_location exist
so those handlers (moved to their own modules in later extraction
steps) can depend on this module instead of on each other.
"""

LIVE_LOCATIONS: dict[int, tuple[float, float]] = {}


def get_live_location(message_id: int):
    """Return the (lat, lon) tuple for message_id, or None if not tracked."""
    return LIVE_LOCATIONS.get(message_id)


def set_live_location(message_id: int, lat: float, lon: float) -> None:
    LIVE_LOCATIONS[message_id] = (lat, lon)


def clear_live_location(message_id: int) -> None:
    LIVE_LOCATIONS.pop(message_id, None)
