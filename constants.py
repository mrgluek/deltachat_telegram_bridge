import re

DC_FALLBACK_PATTERN = re.compile(r'\s*\[(?:Image|Video|Voice|Audio|Document|File|Sticker|Gif)[ \-–]+[^\]]+\]', re.IGNORECASE)

TRANSIENT_POLLING_ERRORS = (
    "Server disconnected without sending a response",
    "ReadTimeout",
    "All connection attempts failed",
    "ConnectError",
    "httpcore.ConnectError",
    "httpx.ConnectError",
    "Unexpected exception reconnecting",
    "Automatic reconnection failed",
    "AttributeError: 'NoneType' object has no attribute 'connect'",
)

TG_MAX_MSG_LEN = 4000
DC_MAX_MSG_LEN = 10000
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 30
GLOBAL_DC_RATE_LIMIT = 60
GLOBAL_DC_RATE_WINDOW = 60
HISTORY_RELAY_COOLDOWN = 300
DELETE_SYNC_MAX = 10
DELETE_SYNC_WINDOW = 60
EDIT_DEBOUNCE_SECONDS = 60
