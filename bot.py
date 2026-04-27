import asyncio
import html
import logging
import os
import tempfile
import time
import threading
import random
from collections import defaultdict
from typing import Optional
import hashlib

from deltachat2 import EventType, MsgData, SystemMessageType, events
from deltabot_cli import BotCli

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, MessageReactionHandler, ChatMemberHandler
from telegram import ReactionTypeEmoji
from telegram.error import NetworkError, TimedOut

try:
    from telethon import TelegramClient, events as tg_events
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
    from telethon.errors import ChannelPrivateError
except ImportError:
    TelegramClient = None
    tg_events = None
    JoinChannelRequest = None
    ImportChatInviteRequest = None
    CheckChatInviteRequest = None
    ChannelPrivateError = None


import database
import io
import sys
import getpass
import re
try:
    import qrcode
except ImportError:
    qrcode = None

DC_FALLBACK_PATTERN = re.compile(r'\s*\[(?:Image|Video|Voice|Audio|Document|File|Sticker|Gif)[ \-–]+[^\]]+\]', re.IGNORECASE)

# Initialize logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("tg_dc_bridge")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Global tracker for Userbot background tasks
_userbot_tasks: set[asyncio.Task] = set()

class PollingErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc_str = str(record.exc_info[1])
            if "Server disconnected without sending a response" in exc_str or "ReadTimeout" in exc_str:
                return False
        msg = record.getMessage()
        if "Server disconnected without sending a response" in msg or "ReadTimeout" in msg:
            return False
        return True

logging.getLogger("telegram.ext.Updater").addFilter(PollingErrorFilter())

class AdminLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self._is_emitting = threading.local()

    def emit(self, record):
        if record.levelno < logging.ERROR:
            return
            
        if getattr(self._is_emitting, 'flag', False):
            return
            
        self._is_emitting.flag = True
        try:
            log_entry = self.format(record)
            
            # Use local refs for globals to be safe
            local_tg_app = tg_app
            local_main_loop = main_loop
            local_dc_bot = dc_bot_instance
            local_dc_accid = dc_accid
            
            # Send to TG
            admin_tg_id = database.get_config("admin_tg_id")
            if admin_tg_id and local_tg_app and local_main_loop:
                try:
                    tg_id = int(admin_tg_id)
                    msg_text = f"⚠️ <b>Bot Error Log</b>\n\n<pre>{html.escape(_truncate(log_entry, 3800))}</pre>"
                    asyncio.run_coroutine_threadsafe(
                        local_tg_app.bot.send_message(chat_id=tg_id, text=msg_text, parse_mode='HTML'),
                        local_main_loop
                    )
                except Exception:
                    pass

            # Send to DC
            admin_dc_email = database.get_config("admin_dc_email")
            if admin_dc_email and local_dc_bot and local_dc_accid:
                try:
                    dc_msg_text = f"⚠️ Bot Error Log\n\n{_truncate(log_entry, DC_MAX_MSG_LEN - 100)}"
                    contact_id = local_dc_bot.rpc.create_contact(local_dc_accid, admin_dc_email, "Admin")
                    chat_id = local_dc_bot.rpc.create_chat_by_contact_id(local_dc_accid, contact_id)
                    local_dc_bot.rpc.send_msg(local_dc_accid, chat_id, MsgData(text=dc_msg_text))
                except Exception:
                    pass
        finally:
            self._is_emitting.flag = False

# Attach it to root and major component loggers
admin_handler = AdminLogHandler()
logging.getLogger().addHandler(admin_handler)
logging.getLogger("deltachat2").addHandler(admin_handler)
logging.getLogger("deltachat2").propagate = True
logging.getLogger("telegram").addHandler(admin_handler)

# Limits
TG_MAX_MSG_LEN = 4000   # Telegram limit is 4096; leave margin
DC_MAX_MSG_LEN = 10000   # Practical DC limit
RATE_LIMIT_WINDOW = 60   # seconds
RATE_LIMIT_MAX = 30       # max messages per window per chat

LIVE_LOCATIONS = {}
db_lock = threading.Lock()

# Initialize DeltaBot CLI
import collections

dc_cli = BotCli("tgbridge")

# Global references
tg_app: Optional[Application] = None
dc_bot_instance = None
dc_accid = None
main_loop = None
bot_contact_id = None  # To detect and skip own messages
userbot_client = None

# Global rate limiting for Delta Chat (e.g. chatmail limits)
GLOBAL_DC_RATE_LIMIT = 60    # messages
GLOBAL_DC_RATE_WINDOW = 60   # seconds
_global_dc_send_times = collections.deque()
_global_dc_rate_limit_lock = asyncio.Lock()
_global_wait_counter = 0
_last_owner_notification_time = 0

async def _wait_for_global_dc_rate_limit():
    """Ensures we don't exceed the global Delta Chat message rate limit."""
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
                    if admin_tg_id and tg_app and (now - _last_owner_notification_time > 60):
                        _last_owner_notification_time = now
                        queue_size = _global_wait_counter - 1 # Current task is waiting on the lock
                        notification = (
                            f"⏳ **Global DC Rate Limit Enforced**\n"
                            f"The bot is waiting {wait_time:.1f}s before sending next message.\n"
                            f"Current queue: {queue_size} messages waiting."
                        )
                        # Send notification in background to not block the relay
                        asyncio.create_task(tg_app.bot.send_message(chat_id=admin_tg_id, text=notification, parse_mode='Markdown'))
                    
                    logger.info(f"Global DC rate limit reached. Waiting {wait_time:.2f}s (Queue: {_global_wait_counter-1})...")
                    await asyncio.sleep(wait_time + 0.1)
                    # Re-clean after sleep
                    now = time.time()
                    while _global_dc_send_times and now - _global_dc_send_times[0] > GLOBAL_DC_RATE_WINDOW:
                        _global_dc_send_times.popleft()
            
            _global_dc_send_times.append(time.time())
    finally:
        _global_wait_counter -= 1


# Double bridging protection
_processed_tg_msgs: dict[tuple[int, int], float] = {}

# Cooldown for channel history relay (per DC chat_id)
HISTORY_RELAY_COOLDOWN = 300  # 5 minutes
_history_cooldowns: dict[int, float] = {}

# Cache for channel history messages (per DC chat_id)
# Stores: {dc_chat_id: {"timestamp": float, "messages": list[TelethonMessage]}}
_history_cache: dict[int, dict] = {}

def _is_history_on_cooldown(dc_chat_id: int) -> bool:
    """Returns True if history relay for this chat is on cooldown."""
    now = time.time()
    last = _history_cooldowns.get(dc_chat_id, 0)
    if now - last < HISTORY_RELAY_COOLDOWN:
        return True
    _history_cooldowns[dc_chat_id] = now
    return False


def _mark_processed(chat_id: int, msg_id: int) -> bool:
    """Returns True if this message was already processed in the last 120 seconds."""
    now = time.time()
    key = (chat_id, msg_id)
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

async def _download_via_userbot(chat_id: int, msg_id: int, suffix: str = "") -> Optional[str]:
    """Fetch message via Userbot and download its media if it exists and is <= 50MB."""
    global userbot_client
    if not (userbot_client and userbot_client.is_connected()):
        return None
    try:
        # Telethon get_messages can fetch by ID
        msg = await userbot_client.get_messages(chat_id, ids=msg_id)
        if not (msg and msg.media):
            return None
        
        # Check size (limit to 50MB for Delta Chat)
        size = 0
        if hasattr(msg, 'document') and msg.document:
            size = msg.document.size
        elif hasattr(msg, 'video') and msg.video:
            size = msg.video.size

        if size > 50 * 1024 * 1024:
            logger.warning(f"Userbot: Media in {chat_id}:{msg_id} is too large ({size // 1024 // 1024} MB > 50 MB)")
            return None

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(tmp_fd)
        logger.info(f"Userbot downloading media from {chat_id}:{msg_id} (size: {size // 1024 // 1024} MB)...")
        path = await userbot_client.download_media(msg.media, file=tmp_path)
        return path
    except Exception as e:
        logger.error(f"Userbot download failed for {chat_id}:{msg_id}: {e}")
    return None

# Simple per-chat rate limiter
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

# Per-message edit debounce: tracks last edit relay time per (chat_id, msg_id)
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

def _get_content_hash(msg) -> str:
    """Return a SHA-256 hash of the message content (text or caption)."""
    # Safe access for both PTB and Telethon objects
    text = getattr(msg, 'text', "") or ""
    caption = getattr(msg, 'caption', "") or ""
    content = text or caption or ""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, appending '…' if truncated."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"

def _inline_links(text: str, entities) -> str:
    """Inline text_link URLs into plain text so hidden links are not lost.

    Telegram 'text_link' entities have a display text and a hidden URL.
    This function appends the URL after the display text, e.g.:
      'Click here' -> 'Click here ( https://example.com )'
    Plain 'url' entities are already visible in the text and left untouched.
    """
    if not entities or not text:
        return text

    # Collect text_link entities, sorted by offset descending so we can
    # insert from the end without shifting earlier offsets.
    links = []
    for ent in entities:
        if ent.type == "text_link" and ent.url:
            links.append((ent.offset, ent.offset + ent.length, ent.url))

    if not links:
        return text

    links.sort(key=lambda x: x[0], reverse=True)
    for start, end, url in links:
        # Only insert if the URL isn't already in the display text
        display_text = text[start:end]
        if url not in display_text:
            text = text[:end] + f" ( {url} )" + text[end:]

    return text


async def async_relay_to_tg(tg_chat_id, dc_chat_id, msg_id, file_path, formatted_msg, tg_reply_id, is_image, is_video, is_voice, viewtype=''):
    try:
        tg_msg = None
        
        # If the message should have a file but hasn't downloaded yet, wait up to 60s
        is_media = is_image or is_video or is_voice or viewtype in ('Document', 'File', 'Image', 'Video', 'Voice', 'Audio', 'Gif', 'Sticker')
        if is_media and not file_path:
            for _ in range(30):
                await asyncio.sleep(2)
                try:
                    updated_msg = dc_bot_instance.rpc.get_message(dc_accid, msg_id)
                    file_path = getattr(updated_msg, 'file', None) or None
                    if file_path and os.path.exists(file_path):
                        break
                except Exception:
                    pass
            if not file_path:
                logger.warning(f"Timeout waiting for media to download for DC msg {msg_id}")
                formatted_msg += "\n\n*[Failed to relay media: timeout waiting for DC download]*"

        if file_path and os.path.exists(file_path):
            filename = os.path.basename(file_path)
            try:
                if is_image:
                    f = open(file_path, 'rb')
                    func = tg_app.bot.send_photo
                    kwargs = {'chat_id': tg_chat_id, 'photo': f, 'caption': formatted_msg, 'parse_mode': 'HTML', 'reply_to_message_id': tg_reply_id}
                elif is_video:
                    f = open(file_path, 'rb')
                    func = tg_app.bot.send_video
                    kwargs = {'chat_id': tg_chat_id, 'video': f, 'caption': formatted_msg, 'parse_mode': 'HTML', 'reply_to_message_id': tg_reply_id}
                elif is_voice:
                    f = open(file_path, 'rb')
                    func = tg_app.bot.send_voice
                    kwargs = {'chat_id': tg_chat_id, 'voice': f, 'caption': formatted_msg, 'parse_mode': 'HTML', 'reply_to_message_id': tg_reply_id}
                else:
                    f = open(file_path, 'rb')
                    func = tg_app.bot.send_document
                    kwargs = {'chat_id': tg_chat_id, 'document': f, 'caption': formatted_msg, 'parse_mode': 'HTML', 'reply_to_message_id': tg_reply_id}
                
                # using retry_async with a longer overall timeout
                tg_msg = await retry_async(func, **kwargs)
            except (TimeoutError, asyncio.TimeoutError):
                fallback_text = formatted_msg + f"\n\n*[Failed to relay media: {html.escape(filename)} - timeout exceeded after retries]*"
                tg_msg = await tg_app.bot.send_message(chat_id=tg_chat_id, text=fallback_text, parse_mode='HTML', reply_to_message_id=tg_reply_id)
            except Exception as e:
                logger.error(f"Error uploading media to TG: {e}")
                tg_msg = await tg_app.bot.send_message(chat_id=tg_chat_id, text=formatted_msg, parse_mode='HTML', reply_to_message_id=tg_reply_id)
        else:
            tg_msg = await tg_app.bot.send_message(chat_id=tg_chat_id, text=formatted_msg, parse_mode='HTML', reply_to_message_id=tg_reply_id)
            
        if tg_msg:
            database.save_message_map(msg_id, dc_chat_id, tg_msg.message_id, tg_chat_id)
    except Exception as e:
        logger.error(f"Failed to relay msg to TG chat {tg_chat_id}: {e}")

# ---------------------------------------------------------
# DELTA CHAT HANDLERS
# ---------------------------------------------------------

@dc_cli.on_init
def on_init(bot, args):
    """Called when the Delta Chat bot starts."""
    bot.logger.info("Initializing Delta Chat tgbridge...")
    
    # Ensure our error handler is attached to the bot's own logger
    bot.logger.addHandler(admin_handler)
    
    for accid in bot.rpc.get_all_account_ids():
        bot.rpc.set_config(accid, "displayname", "TG Bridge")
        bot.rpc.set_config(accid, "selfstatus", "I bridge Telegram and Delta Chat groups. Send /help for commands.")
        # Auto-delete messages after 7 days to save disk space
        # (shorter values cause 'message does not exist' errors for reactions/replies)
        bot.rpc.set_config(accid, "delete_device_after", "604800")
        # Set bot avatar if icon file exists (prefer .jpg)
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            icon_path = os.path.join(base_dir, "icon_deltachat.jpg")
            if not os.path.exists(icon_path):
                icon_path = os.path.join(base_dir, "icon_deltachat.png")
            if os.path.exists(icon_path):
                bot.rpc.set_config(accid, "selfavatar", icon_path)
        except Exception as e:
            bot.logger.warning(f"Could not set avatar: {e}")

def get_dc_help_text(sender_email: str) -> str:
    admin_dc = database.get_config("admin_dc_email")
    mode = "Private (bot owner only)" if admin_dc else "Public (group admins only)"
    return (
        f"👋 Hi {sender_email}!\n\n"
        f"I'm the TG Bridge bot. Current mode: {mode}\n\n"
        f"I relay messages between Delta Chat and Telegram groups.\n\n"
        f"Commands:\n"
        f"/channels — List public Telegram channels to subscribe to\n"
        f"/channelN — Get invite link for channel #N\n"
        f"/channelNqr — Get QR code invite for channel #N\n"
        f"/stats — Show bridge statistics for current chat\n"
        f"/help — Show this help message\n"
        f"/donate — Support bot development ❤️\n\n"
        f"Management (Admins only):\n"
        f"/bridge <tg_group_id> — Link DC group to a Telegram group\n"
        f"/unbridge — Remove the bridge from the group\n\n"
        f"To get started, add me to a Delta Chat group and a Telegram group, then use /bridge to connect them.\n\n"
        f"Run your own bot: https://github.com/mrgluek/deltachat_telegram_bridge"
    )

def get_tg_help_text(name: str, user_id: int) -> str:
    admin_tg = database.get_config("admin_tg_id")
    mode = "Private (bot owner only)" if admin_tg else "Public (group admins only)"
    lines = [
        f"👋 Hi {name} (<code>{user_id}</code>)!\n",
        f"I'm the DC Bridge bot. Current mode: <b>{mode}</b>\n",
        f"I relay messages between Telegram and Delta Chat groups.\n",
        f"Commands:",
        f"/help — Show this help message",
        f"/id — Show group's chat ID",
        f"/bridge — Bridge this TG group to a new DC group",
        f"/unbridge — Remove the bridge from this TG group",
        f"/stats — Show bridge statistics",
        f"/invite — Get Delta Chat bot/group invite link",
        f"/inviteqr — Get Delta Chat bot/group invite QR code",
        f"/donate — Support bot development ❤️",
    ]
    if database.is_owner(user_id):
        lines.append(f"\n<b>⚙️ Channel & Userbot (Owner):</b>")
        lines.append(f"/channeladd @name or ID — Bridge a channel/group")
        lines.append(f"/channels — List bridged channels")
        lines.append(f"/groups — List Userbot groups to bridge")
        lines.append(f"/channel N — Get channel invite link")
        lines.append(f"/channelqr N — Get channel QR invite")
        lines.append(f"/channelremove N — Remove a channel bridge")
        lines.append(f"/userbotsync — Force Userbot re-sync")
        
        lines.append(f"\n<b>👥 Sub-admins (Owner):</b>")
        lines.append(f"/adminadd <i>user_id</i> — Add a sub-admin")
        lines.append(f"/adminremove <i>user_id</i> — Remove a sub-admin")
        lines.append(f"/admins — List sub-admins")
    lines.append(f"\nTo get started, add me to a Telegram group and use /bridge to connect it to Delta Chat.")
    lines.append(f"\nℹ️ Make sure Group Privacy is turned off in @BotFather → Bot Settings.")
    lines.append(f"\nRun your own bot: https://github.com/mrgluek/deltachat_telegram_bridge")
    return "\n".join(lines)


@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    """Reply with help text."""
    msg = event.msg
    
    # Get sender info
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    
    help_msg = get_dc_help_text(sender_email)
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=help_msg))

@dc_cli.on(events.NewMessage(command="/donate"))
def dc_donate_command(bot, accid, event):
    """Reply with donate link."""
    msg = event.msg
    support_msg = (
        "❤️ Support Bot Development\n\n"
        "If you find this bridge useful, you can support its development and server costs here:\n\n"
        "🔗 https://web.tribute.tg/d/IWb\n\n"
        "Thank you! 🙏"
    )
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=support_msg))

def to_dc_markdown(text: str) -> str:
    """Convert limited HTML tags (b, i, code) to Markdown for Delta Chat."""
    if not text:
        return ""
    # Note: Using * for italic as requested by user
    return (text.replace("<b>", "**").replace("</b>", "**")
            .replace("<i>", "*").replace("</i>", "*")
            .replace("<code>", "`").replace("</code>", "`")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))

@dc_cli.on(events.NewMessage(command="/channeladd"))
def dc_channeladd_command(bot, accid, event):
    """Add a channel bridge from Delta Chat. Admin only."""
    msg = event.msg
    payload = event.payload.strip()
    
    # Simple email-based admin check
    admin_dc_email = database.get_config("admin_dc_email")
    sender_email = bot.rpc.get_contact(accid, msg.from_id).address
    if not admin_dc_email or sender_email.lower() != admin_dc_email.lower():
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can manage channels."))
        return

    if not payload:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="Usage: /channeladd @username or t.me link"))
        return

    # Use run_coroutine_threadsafe since dc_cli hooks might run in a separate thread
    async def run_add():
        status_id = bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="⏳ Processing channel bridge..."))
        result = await _add_channel_bridge(payload)
        # Convert HTML response to Markdown for DC
        result_md = to_dc_markdown(result)
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=result_md))
    
    if main_loop:
        asyncio.run_coroutine_threadsafe(run_add(), main_loop)
    else:
        logger.error("Main loop not found, cannot run channeladd")

@dc_cli.on(events.NewMessage(command="/channelremove"))
def dc_channelremove_command(bot, accid, event):
    """Remove a channel bridge from Delta Chat. Admin only."""
    msg = event.msg
    payload = event.payload.strip()
    
    admin_dc_email = database.get_config("admin_dc_email")
    sender_email = bot.rpc.get_contact(accid, msg.from_id).address
    if not admin_dc_email or sender_email.lower() != admin_dc_email.lower():
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can manage channels."))
        return

    if not payload:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="Usage: /channelremove N (channel number)"))
        return

    try:
        channel_id = int(payload)
        ch = database.get_channel_by_id(channel_id)
        if not ch:
             bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"❌ Channel #{channel_id} not found."))
             return
             
        if database.remove_channel(channel_id):
             bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"✅ Channel bridge #{channel_id} removed."))
        else:
             bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"❌ Failed to remove channel #{channel_id}."))
    except ValueError:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="❌ Invalid channel number."))

@dc_cli.on(events.NewMessage(command="/bridge"))
def bridge_command(bot, accid, event):
    """Bridge a Delta Chat group to a Telegram group. Admin only."""
    msg = event.msg
    chat_id = msg.chat_id

    # Check if it's a group chat
    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    if chat_info.get("type") == 1:
        bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ You must send that command in a Delta Chat group, not here."))
        return

    # Admin check: if a global admin is set, only they can manage bridges
    admin_dc_email = database.get_config("admin_dc_email")
    sender_email = bot.rpc.get_contact(accid, msg.from_id).address
    
    if admin_dc_email:
        if sender_email.lower() != admin_dc_email.lower():
            bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ Only the configured bot administrator can use /bridge."))
            return
    else:
        # Fallback to group creator check
        try:
            contacts = bot.rpc.get_chat_contacts(accid, chat_id)
            # In Delta Chat, the first contact in the list is the group creator/admin
            # Also check if the sender is the bot owner (contact ID 1 = self)
            if msg.from_id not in contacts[:1] and msg.from_id != 1:
                bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ Only group admins can use /bridge. (Or set a global admin via `init admin_dc`)"))
                return
        except Exception:
            pass  # If we can't check, allow it (backward compat)

    try:
        payload = event.payload.strip()
        if not payload:
            raise ValueError()
        tg_chat_id = int(payload)
    except ValueError:
        bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ You must provide the Telegram chat ID. Example: /bridge -123456789"))
        return

    if database.add_bridge(chat_id, tg_chat_id):
        bot.rpc.send_msg(accid, chat_id, MsgData(text=f"✔️ Bridged with Telegram group {tg_chat_id}."))
    else:
        bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ This chat is already bridged."))

@dc_cli.on(events.NewMessage(command="/unbridge"))
def unbridge_command(bot, accid, event):
    """Remove the bridge for this Delta Chat group. Admin only."""
    msg = event.msg
    chat_id = msg.chat_id

    # Check if it's a group chat
    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    if chat_info.get("type") == 1:
        bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ You must send that command in a Delta Chat group, not here."))
        return

    # Admin check
    admin_dc_email = database.get_config("admin_dc_email")
    sender_email = bot.rpc.get_contact(accid, msg.from_id).address
    
    if admin_dc_email:
        if sender_email.lower() != admin_dc_email.lower():
            bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ Only the configured bot administrator can use /unbridge."))
            return
    else:
        try:
            contacts = bot.rpc.get_chat_contacts(accid, chat_id)
            if msg.from_id not in contacts[:1] and msg.from_id != 1:
                bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ Only group admins can use /unbridge. (Or set a global admin via `init admin_dc`)"))
                return
        except Exception:
            pass

    if database.remove_bridge(chat_id):
        bot.rpc.send_msg(accid, chat_id, MsgData(text="✔️ Bridge removed."))
    else:
        bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ This chat is not bridged."))


@dc_cli.on(events.NewMessage(command="/locupdate"))
def locupdate_command(bot, accid, event):
    """Fetch the latest coordinates for a live location message."""
    msg = event.msg
    chat_id = msg.chat_id

    # Check if this is a reply to another message
    if not hasattr(msg, 'quote') or not msg.quote:
        bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ Please reply to a Live Location message with /locupdate."))
        return

    quote_msg_id = msg.quote.get('message_id') if isinstance(msg.quote, dict) else None
    if not quote_msg_id:
        bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ Please reply to a Live Location message with /locupdate."))
        return

    # Check group chat mappings
    tg_chats = database.get_tg_chats(chat_id)
    found = False
    
    if tg_chats:
        for tg_chat_id in tg_chats:
            tg_msg_id = database.get_tg_msg_id(quote_msg_id, chat_id, tg_chat_id)
            if tg_msg_id and tg_msg_id in LIVE_LOCATIONS:
                lat, lon = LIVE_LOCATIONS[tg_msg_id]
                bot.rpc.send_msg(accid, chat_id, MsgData(text=f"📍 Updated Location: https://maps.google.com/?q={lat},{lon}", quoted_message_id=quote_msg_id))
                found = True
                break
                
    if not found:
        # Check channel mapping if needed (very rare case for locupdate but just in case)
        # We don't track channel reverse mapping in memory easily here, so we just return not found.
        bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ No active live location found for this message. It may have expired or not be a live location.", quoted_message_id=msg.id))


@dc_cli.on(events.NewMessage(command="/userbotsync"))
def dc_userbotsync_command(bot, accid, event):
    """Force Userbot sync from Delta Chat. Admin only."""
    msg = event.msg
    admin_dc_email = database.get_config("admin_dc_email")
    sender_email = bot.rpc.get_contact(accid, msg.from_id).address
    if not admin_dc_email or sender_email.lower() != admin_dc_email.lower():
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can trigger synchronization."))
        return

    async def run_sync():
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="⏳ Starting Userbot synchronization..."))
        await sync_userbot_channels(force=True)
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="✅ Userbot synchronization completed (subscriber counts updated)."))
    
    if main_loop:
        asyncio.run_coroutine_threadsafe(run_sync(), main_loop)
    else:
        logger.error("Main loop not found, cannot run userbotsync")

@dc_cli.on(events.NewMessage(command="/stats"))
def stats_command(bot, accid, event):
    """Show bridge statistics."""
    msg = event.msg
    chat_id = msg.chat_id

    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    is_private = chat_info.get("type") == 1

    if is_private:
        # In private chat: show all bridges (admin only)
        admin_dc_email = database.get_config("admin_dc_email")
        sender_email = bot.rpc.get_contact(accid, msg.from_id).address
        if admin_dc_email and sender_email.lower() != admin_dc_email.lower():
            bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ Only the bot admin can view all stats."))
            return

        bridges = database.get_all_bridges()
        if not bridges:
            bot.rpc.send_msg(accid, chat_id, MsgData(text="📊 No bridges configured."))
            return

        lines = [f"📊 Bridge Statistics ({len(bridges)} bridge{'s' if len(bridges) != 1 else ''})\n"]
        for row in bridges:
            # row: (dc_chat_id, tg_chat_id, reactions_count)
            dc_cid, tg_cid, r_count = row if len(row) == 3 else (row[0], row[1], 0)
            m_count = database.get_bridge_message_count(dc_cid, tg_cid)
            try:
                title = bot.rpc.get_basic_chat_info(accid, dc_cid).get("name", "Unknown Group")
            except Exception:
                title = "Unknown Group"
            lines.append(f"• DC {dc_cid} ↔ TG {tg_cid} ({title}) — {m_count} 💬 {r_count} 🙂")
                
        bot.rpc.send_msg(accid, chat_id, MsgData(text="\n".join(lines)))
    else:
        # In group chat: show stats for this bridge only
        tg_chats = database.get_tg_chats(chat_id)
        if not tg_chats:
            bot.rpc.send_msg(accid, chat_id, MsgData(text="📊 This group is not bridged."))
            return

        lines = ["📊 Bridge Statistics\n"]
        for tg_cid in tg_chats:
            m_count = database.get_bridge_message_count(chat_id, tg_cid)
            try:
                title = bot.rpc.get_basic_chat_info(accid, chat_id).get("name", "this group")
            except Exception:
                title = "this group"
            
            # Reactions are not relevant for broadcast channels, only show for groups if we have them
            r_count = database.get_bridge_reaction_count(chat_id, tg_cid)
            lines.append(f"• TG Group {tg_cid} ({title}) — {m_count} 💬 {r_count} 🙂")
                
        bot.rpc.send_msg(accid, chat_id, MsgData(text="\n".join(lines)))


@dc_cli.on(events.NewMessage(is_info=True))
def handle_dc_info_message(bot, accid, event):
    """Handle Delta Chat system/info messages (like member additions)."""
    global main_loop
    
    msg = event.msg
    dc_chat_id = msg.chat_id
    
    # Get the system_message_type in a robust way - it can be an int, enum, or string
    smt = msg.system_message_type
    smt_str = str(smt).lower() if smt is not None else ""
    
    # Log every info message to help debug - only at debug level to avoid noise
    logger.debug(f"DC info msg in chat {dc_chat_id}: system_message_type={smt!r} ({smt_str!r})")
    
    # Match member-added/joined events regardless of how the library represents them:
    # - As enum: SystemMessageType.MEMBER_ADDED_TO_GROUP / .MEMBER_JOINED_GROUP
    # - As string: "MemberAddedToGroup", "member_added_to_group", etc.
    # - As int: whatever the underlying value happens to be
    is_member_event = False
    try:
        if smt == SystemMessageType.MEMBER_ADDED_TO_GROUP:
            is_member_event = True
        elif hasattr(SystemMessageType, 'MEMBER_JOINED_GROUP') and smt == SystemMessageType.MEMBER_JOINED_GROUP:
            is_member_event = True
    except Exception:
        pass
    
    # Also match by string representation as a fallback
    if not is_member_event:
        member_keywords = ("memberadded", "member_added", "memberjoined", "member_joined")
        is_member_event = any(kw in smt_str.replace(" ", "").replace("_", "") for kw in
                              ("memberadded", "memberjoined"))
    
    if is_member_event:
        logger.info(f"Member event detected in DC chat {dc_chat_id} (type={smt!r})")
        # Check if this is a bridged channel
        ch = database.get_channel_by_dc_chat_id(dc_chat_id)
        if ch and main_loop:
            tg_channel_id = ch['tg_channel_id']
            # ub_target is username if available, else numeric ID
            ub_target = ch['tg_channel_username'] if ch['tg_channel_username'] else tg_channel_id
            
            if ub_target:
                logger.info(f"New member joined bridged DC channel {dc_chat_id}. Relaying history...")
                invite_link = ch.get('invite_link')
                asyncio.run_coroutine_threadsafe(
                    _relay_channel_history(dc_chat_id, tg_channel_id, ub_target, limit=3, invite_link=invite_link),
                    main_loop
                )
        elif not ch:
            logger.debug(f"DC chat {dc_chat_id} is not a bridged channel, skipping history relay.")


@dc_cli.on(events.NewMessage(command="/channels"))
def channels_command_dc(bot, accid, event):
    """List public bridged channels to Delta Chat users."""
    msg = event.msg
    chat_id = msg.chat_id

    # Check if requester is admin
    admin_dc_email = database.get_config("admin_dc_email")
    sender_email = bot.rpc.get_contact(accid, msg.from_id).address
    is_admin = admin_dc_email and sender_email.lower() == admin_dc_email.lower()

    channels = database.get_all_channels()
    if is_admin:
        # Admin sees everything
        display_channels = channels
    else:
        # Others only see public channels (those with a username)
        display_channels = [c for c in channels if c.get('tg_channel_username')]

    if not display_channels:
        bot.rpc.send_msg(accid, chat_id, MsgData(text="📺 No public channels are currently available."))
        return

    lines = [f"📺 **{'All' if is_admin else 'Public'} Channels:**\n"]
    for ch in display_channels:
        dc_cid = ch['dc_chat_id']
        tg_username = ch.get('tg_channel_username')
        tg_id = ch.get('tg_channel_id', 0)
        
        # Get counts
        r_count = ch.get('reactions_count', 0)
        m_count = database.get_bridge_message_count(dc_cid, tg_id)
        tg_sub_count = ch.get('tg_participants_count', 0)
        
        try:
            chat_info = bot.rpc.get_basic_chat_info(accid, dc_cid)
            title = chat_info.get("name", "Unknown Channel")
            contacts = bot.rpc.get_chat_contacts(accid, dc_cid)
            dc_sub_count = len(contacts) - 1 if contacts else 0
        except Exception:
            title = "Unknown Channel"
            dc_sub_count = "?"

        # Format: /channel1 — Gluek's blog (t.me/gluekinfo) — 👤 150k TG / 7 DC — 💬 1
        tg_ref = f"(t.me/{tg_username})" if tg_username else f"(ID: {tg_id})"
        stats_str = f"👤 {tg_sub_count:,} TG / {dc_sub_count} DC — 💬 {m_count}"
        lines.append(f"/channel{ch['id']} — {title} {tg_ref} — {stats_str}")
    
    lines.append("\nClick a /channelN command for link or /channelNqr for QR code.")
    bot.rpc.send_msg(accid, chat_id, MsgData(text="\n".join(lines)))


@dc_cli.on(events.NewMessage(is_info=False))
def handle_dc_message(bot, accid, event):
    """Relay Delta Chat messages to Telegram."""
    global tg_app

    msg = event.msg
    dc_chat_id = msg.chat_id
    from_id = msg.from_id

    # Detect new users in private chats and send help
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, dc_chat_id)
        if chat_info.get("type") == 1: # Private chat
            # Check if we already greeted this contact
            if not bot.rpc.get_contact_config(accid, from_id, "greeted"):
                help_text = get_dc_help_text(bot.rpc.get_contact(accid, from_id).address)
                bot.rpc.send_msg(accid, dc_chat_id, MsgData(text=f"👋 Welcome!\n\n{help_text}"))
                bot.rpc.set_contact_config(accid, from_id, "greeted", "1")
    except Exception as e:
        logger.warning(f"Greeting check failed: {e}")

    if bot.has_command(event.command):
        return  # Ignore standard commands
    
    # Handle /channelN and /channelNqr commands for subscriptions
    text = msg.text or ""
    cmd = text.split()[0] if text else ""
    
    if cmd.startswith("/channel") and any(c.isdigit() for c in cmd):
        try:
            is_qr = cmd.endswith("qr")
            id_str = cmd[8:-2] if is_qr else cmd[8:]
            if not id_str.isdigit():
                 # Handle cases like /channel123qr where id_str is "123"
                 import re
                 match = re.search(r'(\d+)', cmd[8:])
                 if match:
                     channel_id = int(match.group(1))
                 else:
                     return # Not a valid command
            else:
                channel_id = int(id_str)
                
            ch = database.get_channel_by_id(channel_id)
            if ch:
                # Security Check:
                # 1. If public, everyone can join. 
                # 2. If private, only admin/creator can join.
                is_public = bool(ch.get('tg_channel_username'))
                
                if not is_public:
                    # Check if sender is admin or sub-admin
                    admin_dc_email = database.get_config("admin_dc_email")
                    contact = bot.rpc.get_contact(accid, msg.from_id)
                    sender_email = contact.address
                    
                    is_admin = False
                    if admin_dc_email and sender_email.lower() == admin_dc_email.lower():
                        is_admin = True
                    
                    # Also check if they are the creator of this bridge (optional but good)
                    # Note: sub-admins are identified by TG ID in the DB, so for DC we mostly rely on admin_dc_email
                    
                    if not is_admin:
                        # Deny access to private channels for regular users
                        bot.rpc.send_msg(accid, dc_chat_id, MsgData(text=f"❌ Channel #{channel_id} not found."))
                        return

                invite_link = ch.get('invite_link', '')
                if not invite_link:
                    bot.rpc.send_msg(accid, dc_chat_id, MsgData(text="❌ No invite link available for this channel."))
                    return
                
                if is_qr:
                    import qrcode
                    import tempfile
                    import os
                    
                    qr = qrcode.QRCode(version=1, box_size=10, border=5)
                    qr.add_data(invite_link)
                    qr.make(fit=True)
                    img = qr.make_image(fill_color="black", back_color="white")
                    
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                        tmp_path = tmp.name
                        img.save(tmp_path)
                    
                    title = ch.get('title')
                    if not title:
                         try:
                             chat_info = bot.rpc.get_basic_chat_info(accid, ch['dc_chat_id'])
                             title = chat_info.get("name", "channel")
                         except Exception:
                             title = "channel"
                    
                    # Format caption: Only include t.me link if it exists
                    tg_username = ch.get('tg_channel_username')
                    link_part = f" (t.me/{tg_username})" if tg_username else ""
                    
                    bot.rpc.send_msg(accid, dc_chat_id, MsgData(
                        text=f"📷 QR Code for **{title}**{link_part}", 
                        file=tmp_path
                    ))
                    
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
                else:
                    title = ch.get('title')
                    if not title:
                         try:
                             chat_info = bot.rpc.get_basic_chat_info(accid, ch['dc_chat_id'])
                             title = chat_info.get("name", "channel")
                         except Exception:
                             title = "channel"
                    
                    tg_username = ch.get('tg_channel_username')
                    link_part = f" (t.me/{tg_username})" if tg_username else ""
                    bot.rpc.send_msg(accid, dc_chat_id, MsgData(text=f"🔗 Join channel **{title}**{link_part}:\n\n{invite_link}"))
                    
                    # Relay last 3 channel posts to user's private chat as a preview
                    if main_loop and userbot_client:
                        tg_channel_id = ch.get('tg_channel_id')
                        dc_channel_chat_id = ch.get('dc_chat_id')
                        ub_target = ch.get('tg_channel_username') or tg_channel_id
                        ch_invite = ch.get('invite_link')
                        if tg_channel_id and ub_target:
                            logger.info(f"Sending channel history preview to private chat {dc_chat_id} for channel {tg_channel_id}")
                            asyncio.run_coroutine_threadsafe(
                                _relay_channel_history_to_chat(dc_chat_id, tg_channel_id, ub_target, limit=3, invite_link=ch_invite, bot_ref=bot, accid=accid),
                                main_loop
                            )
                return
            else:
                bot.rpc.send_msg(accid, dc_chat_id, MsgData(text=f"❌ Channel #{channel_id} not found."))
                return
        except Exception as e:
            logger.error(f"Error handling /channelN(qr): {e}")
            return

    # Skip bot's own messages to prevent echo loops
    if bot_contact_id and msg.from_id == bot_contact_id:
        return

    # Only relay group messages
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, dc_chat_id)
        if chat_info.get("type") == 1:
            return
    except Exception:
        return

    # Check if this is chat is bridged
    tg_chats = database.get_tg_chats(dc_chat_id)
    if not tg_chats or not tg_app:
        return

    # Rate limit check
    if _is_rate_limited(dc_chat_id):
        return

    try:
        sender_contact = bot.rpc.get_contact(accid, msg.from_id)
        sender_name = msg.override_sender_name or sender_contact.display_name or sender_contact.address
    except Exception:
        sender_name = "Unknown"

    text = msg.text or ""
    text = DC_FALLBACK_PATTERN.sub('', text).strip()
    file_path = getattr(msg, 'file', None) or None
    viewtype = getattr(msg, 'viewtype', None) or ''
    is_media = viewtype in ('Image', 'Gif', 'Sticker', 'Video', 'Voice', 'Audio', 'Document', 'File')

    # Skip messages with neither text nor file
    if not text and not file_path and not is_media:
        return

    # HTML-escape both name and text to prevent injection
    safe_name = html.escape(sender_name)
    safe_text = html.escape(text) if text else ""

    # Check if this is a reply to another message
    reply_quote = None
    if hasattr(msg, 'quote') and msg.quote:
        quote_text = msg.quote.get('text', '') if isinstance(msg.quote, dict) else ''
        quote_text = DC_FALLBACK_PATTERN.sub('', quote_text).strip()
        if quote_text:
            short_quote = _truncate(quote_text, 50)
            reply_quote = f"<i>↩ {html.escape(short_quote)}</i>\n"

    # Determine if this is a media message
    is_image = viewtype in ('Image', 'Gif', 'Sticker') or (
        file_path and file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp'))
    )
    is_video = viewtype == 'Video' or (
        file_path and file_path.lower().endswith(('.mp4', '.mov', '.avi', '.mkv'))
    )
    is_voice = viewtype in ('Voice', 'Audio')

    # Send to all mapped Telegram chats
    for tg_chat_id in tg_chats:
        try:
            tg_reply_id = None
            if hasattr(msg, 'quote') and msg.quote:
                quote_msg_id = msg.quote.get('message_id') if isinstance(msg.quote, dict) else None
                if quote_msg_id:
                    tg_reply_id = database.get_tg_msg_id(quote_msg_id, dc_chat_id, tg_chat_id)

            # Build caption/text for this specific chat
            chat_reply_quote = reply_quote if not tg_reply_id else None
            
            if safe_text:
                if chat_reply_quote:
                    formatted_msg = f"{chat_reply_quote}<b>{safe_name}</b>: {safe_text}"
                else:
                    formatted_msg = f"<b>{safe_name}</b>: {safe_text}"
            else:
                formatted_msg = f"<b>{safe_name}</b>"
            formatted_msg = _truncate(formatted_msg, TG_MAX_MSG_LEN)

            if main_loop:
                asyncio.run_coroutine_threadsafe(
                    async_relay_to_tg(tg_chat_id, dc_chat_id, msg.id, file_path, formatted_msg, tg_reply_id, is_image, is_video, is_voice, viewtype),
                    main_loop
                )
            bot.logger.info(f"Relayed DC msg {msg.id} to TG chat {tg_chat_id}")
        except Exception as e:
            bot.logger.error(f"Failed to relay msg to TG chat {tg_chat_id}: {e}")

@dc_cli.on(events.RawEvent(events.EventType.REACTIONS_CHANGED))
def handle_dc_reaction(bot, accid, event):
    """Relay Delta Chat reactions to Telegram."""
    global tg_app, main_loop
    if not tg_app or not main_loop:
        return
        
    try:
        # Ignore events triggered by the bot itself to prevent echo loops
        contact_id = getattr(event, 'contact_id', None)
        # Only return if both exist and match
        if bot_contact_id and contact_id and str(contact_id) == str(bot_contact_id):
            return

        msg_id = getattr(event, 'msg_id', None)
        if not msg_id:
            return

        dc_chat_id = bot.rpc.get_message(accid, msg_id).chat_id
        
        bot.logger.info(f"DC Reaction event for msg {msg_id} in DC chat {dc_chat_id} from contact {contact_id}")
            
        try:
            dc_reactions = bot.rpc.get_message_reactions(accid, msg_id)
        except Exception:
            dc_reactions = None
            
        tg_mappings = database.get_tg_mappings_by_dc_msg_id(msg_id)
        if not tg_mappings:
            return
            
        # get_message_reactions returns:
        # {'reactions': [{'count': N, 'emoji': '👍', 'is_from_self': True}], 'reactions_by_contact': {'1': ['👍']}}
        primary_emoji = None
        if dc_reactions and hasattr(dc_reactions, 'reactions') and dc_reactions.reactions:
            primary_emoji = dc_reactions.reactions[0].emoji
        else:
            bot.logger.info(f"No reactions found for DC msg {msg_id}")
                
        for tg_msg_id, tg_chat_id in tg_mappings:
            try:
                reaction = [ReactionTypeEmoji(primary_emoji)] if primary_emoji else []
                asyncio.run_coroutine_threadsafe(
                    tg_app.bot.set_message_reaction(chat_id=tg_chat_id, message_id=tg_msg_id, reaction=reaction),
                    main_loop
                )
                if database.get_dc_channel_chat_id(tg_chat_id):
                    database.increment_channel_reaction_count(tg_channel_id)
                else:
                    database.increment_bridge_reaction_count(int(dc_chat_id), int(tg_chat_id))
            except Exception as e:
                bot.logger.error(f"Failed to relay DC reaction to TG chat {tg_chat_id}: {e}")
    except Exception as e:
        bot.logger.error(f"Error handling DC reaction: {e}")

# Save references for Telegram to use and print QR
@dc_cli.on_start
def on_start(bot, _args):
    global dc_bot_instance, dc_accid, bot_contact_id
    dc_bot_instance = bot
    accounts = bot.rpc.get_all_account_ids()
    if accounts:
        dc_accid = accounts[0]

        # Detect bot's own contact ID to prevent echo loops
        try:
            bot_contact_id = bot.rpc.get_contact(dc_accid, 1).id  # Contact ID 1 = self
        except Exception:
            try:
                # Fallback: use the special contact constant
                bot_contact_id = 1
            except Exception:
                pass

        try:
            qrdata = bot.rpc.get_chat_securejoin_qr_code(dc_accid, None)
            print("\n" + "="*50)
            print("To add this bot to a Delta Chat group, scan the QR code")
            print("or copy the link below:\n")

            try:
                qr = qrcode.QRCode(version=1, box_size=1, border=2)
                qr.add_data(qrdata)
                qr.make(fit=True)
                f = io.StringIO()
                qr.print_ascii(out=f)
                print(f.getvalue())
            except ImportError:
                print("(Install 'qrcode' package to see the ASCII QR code here)")

            print(qrdata)
            print("\n" + "="*50 + "\n")
        except Exception as e:
            bot.logger.error(f"Failed to generate QR code: {e}")

# ---------------------------------------------------------
# TELEGRAM HANDLERS
# ---------------------------------------------------------

async def tg_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply to /start in private chat with help text and user ID."""
    user = update.effective_user
    name = html.escape(user.first_name)
    greeting = get_tg_help_text(name, user.id)
    await update.message.reply_text(greeting, parse_mode='HTML')

async def tg_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply to /help with help text."""
    user = update.effective_user
    name = html.escape(user.first_name)
    help_msg = get_tg_help_text(name, user.id)
    await update.message.reply_text(help_msg, parse_mode='HTML')

async def tg_donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply with donate link."""
    support_msg = (
        "❤️ <b>Support Bot Development</b>\n\n"
        "If you find this bridge useful, you can support its development and server costs here:\n\n"
        "🔗 <a href='https://t.me/tribute/app?startapp=dIWb'>Support via Tribute</a>\n\n"
        "Thank you! 🙏"
    )
    await update.message.reply_html(support_msg, disable_web_page_preview=True)

async def tg_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the Telegram chat ID. Only responds to group admins."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("❌ You must send that command in a Telegram group, not here.")
        return

    # Admin check for Telegram
    try:
        member = await chat.get_member(user.id)
        if member.status not in ("administrator", "creator"):
            await update.message.reply_text("❌ Only group admins can use /id.")
            return
    except Exception:
        pass  # If we can't verify, allow it

    await update.message.reply_text(f"Group ID: <code>{chat.id}</code>", parse_mode='HTML')


async def tg_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bridge statistics on Telegram side."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        # In private chat: owner sees all, sub-admin sees own, others denied
        admin_tg_id = database.get_config("admin_tg_id")
        if admin_tg_id:
            if database.is_owner(user.id):
                bridges = database.get_all_bridges()
            elif database.is_admin(user.id):
                bridges = database.get_bridges_by_creator(user.id)
            else:
                await update.message.reply_text("❌ Only the bot admin can view stats.")
                return
        else:
            bridges = database.get_all_bridges()

        if not bridges:
            await update.message.reply_text("📊 No bridges configured.")
            return

        lines = [f"📊 <b>Bridge Statistics</b> ({len(bridges)} bridge{'s' if len(bridges) != 1 else ''})\n"]
        for row in bridges:
            # row: (dc_cid, tg_cid, reactions_count)
            dc_cid, tg_cid, r_count = row if len(row) == 3 else (row[0], row[1], 0)
            m_count = database.get_bridge_message_count(dc_cid, tg_cid)
            try:
                chat_info = dc_bot_instance.rpc.get_basic_chat_info(dc_accid, dc_cid)
                title = chat_info.get("name", "Unknown Group")
                # Get member count (minus the bot itself)
                contacts = dc_bot_instance.rpc.get_chat_contacts(dc_accid, dc_cid)
                sub_count = len(contacts) - 1 if contacts else 0
            except Exception:
                title = "Unknown Group"
                sub_count = "?"
            lines.append(f"• DC <code>{dc_cid}</code> ↔ TG <code>{tg_cid}</code> ({html.escape(title)}) — {sub_count} 👤 {m_count} 💬 {r_count} 🙂")
        
        
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
    else:
        # In group chat: owner, sub-admin (if they created this bridge), or TG group admin
        admin_tg_id = database.get_config("admin_tg_id")
        if admin_tg_id:
            is_privileged = database.is_owner(user.id)
            if not is_privileged and database.is_admin(user.id):
                creator = database.get_bridge_creator_by_tg(chat.id)
                is_privileged = (creator is not None and creator == user.id)
            if not is_privileged:
                try:
                    member = await chat.get_member(user.id)
                    if member.status not in ("administrator", "creator"):
                        await update.message.reply_text("❌ Only group admins can view stats.")
                        return
                except Exception:
                    pass

        dc_chats = database.get_dc_chats(chat.id)
        if not dc_chats:
            await update.message.reply_text("📊 This group is not bridged.")
            return

        lines = ["📊 <b>Bridge Statistics</b>\n"]
        for dc_cid in dc_chats:
            m_count = database.get_bridge_message_count(dc_cid, chat.id)
            r_count = database.get_bridge_reaction_count(dc_cid, chat.id)
            try:
                title = dc_bot_instance.rpc.get_basic_chat_info(dc_accid, dc_cid).get("name", "this group")
            except Exception:
                title = "this group"
            lines.append(f"• DC <code>{dc_cid}</code> ({html.escape(title)}) — {m_count} 💬 {r_count} 🙂")
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')


# ---------------------------------------------------------
# TG BRIDGE / UNBRIDGE COMMANDS
# ---------------------------------------------------------

async def tg_bridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bridge a TG group to a new DC group. Auto-creates the DC group."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("❌ You must send /bridge in a Telegram group, not here.")
        return

    # Permission check: owner, sub-admin, or (in public mode) TG group admin
    admin_tg_id = database.get_config("admin_tg_id")
    if admin_tg_id:
        if not database.is_owner_or_admin(user.id):
            await update.message.reply_text("❌ Only the bot admin can use /bridge.")
            return
    else:
        try:
            member = await chat.get_member(user.id)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text("❌ Only group admins can use /bridge.")
                return
        except Exception:
            pass

    # Check if already bridged
    existing = database.get_dc_chats(chat.id)
    if existing:
        await update.message.reply_text("❌ This group is already bridged.")
        return

    if not dc_bot_instance or not dc_accid:
        await update.message.reply_text("❌ Delta Chat bot is not ready yet.")
        return

    tg_chat_id = chat.id
    tg_title = chat.title or f"TG Group {tg_chat_id}"

    await update.message.reply_text(f"⏳ Setting up bridge for <b>{html.escape(tg_title)}</b>...", parse_mode='HTML')

    try:
        # Create DC group with same name
        dc_chat_id = dc_bot_instance.rpc.create_group_chat(dc_accid, tg_title, False)

        # Copy TG group avatar to DC group
        try:
            tg_chat_info = await context.bot.get_chat(tg_chat_id)
            if tg_chat_info.photo:
                avatar_file = await tg_chat_info.photo.get_big_file()
                tmp_fd, avatar_path = tempfile.mkstemp(suffix=".jpg")
                os.close(tmp_fd)
                await avatar_file.download_to_drive(custom_path=avatar_path)
                dc_bot_instance.rpc.set_chat_profile_image(dc_accid, dc_chat_id, avatar_path)
                try:
                    os.unlink(avatar_path)
                except Exception:
                    pass
                logger.info(f"Copied avatar from TG group {tg_chat_id} to DC group {dc_chat_id}")
        except Exception as e:
            logger.warning(f"Could not copy group avatar: {e}")

        # Save bridge to DB
        database.add_bridge(dc_chat_id, tg_chat_id, created_by_tg_id=user.id)

        # Generate invite link
        invite_link = dc_bot_instance.rpc.get_chat_securejoin_qr_code(dc_accid, dc_chat_id)
        if invite_link.startswith("OPEN-CHAT:"):
            invite_link = "https://i.delta.chat/#" + invite_link[10:]
        elif invite_link.startswith("OPEN:"):
            invite_link = "https://i.delta.chat/#" + invite_link[5:]

        await update.message.reply_text(
            f"✅ Bridged! DC group <b>{html.escape(tg_title)}</b> created.\n\n"
            f"🔗 Join in Delta Chat:\n{html.escape(invite_link)}",
            parse_mode='HTML',
            disable_web_page_preview=True
        )
        logger.info(f"TG bridge: user {user.id} bridged TG {tg_chat_id} -> DC {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to create TG bridge: {e}")
        await update.message.reply_text(f"❌ Error: {html.escape(str(e))}", parse_mode='HTML')


async def tg_unbridge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove bridge for this TG group."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("❌ You must send /unbridge in a Telegram group, not here.")
        return

    # Permission check
    admin_tg_id = database.get_config("admin_tg_id")
    if admin_tg_id:
        if database.is_owner(user.id):
            pass  # owner can unbridge anything
        elif database.is_admin(user.id):
            creator = database.get_bridge_creator_by_tg(chat.id)
            if creator != user.id:
                await update.message.reply_text("❌ You can only unbridge groups you created.")
                return
        else:
            await update.message.reply_text("❌ Only the bot admin can use /unbridge.")
            return
    else:
        try:
            member = await chat.get_member(user.id)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text("❌ Only group admins can use /unbridge.")
                return
        except Exception:
            pass

    if database.remove_bridge_by_tg(chat.id):
        await update.message.reply_text("✔️ Bridge removed.")
    else:
        await update.message.reply_text("❌ This group is not bridged.")


# ---------------------------------------------------------
# ADMIN MANAGEMENT COMMANDS (owner only)
# ---------------------------------------------------------

async def tg_adminadd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a sub-admin. Owner only, private chat only."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "private":
        await update.message.reply_text("❌ This command can only be used in a private chat with the bot.")
        return

    if not database.is_owner(user.id):
        await update.message.reply_text("❌ Only the bot owner can manage admins.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: <code>/adminadd user_id</code>\n\n"
            "The user should send /start to the bot first to get their user ID.",
            parse_mode='HTML'
        )
        return

    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid numeric user ID.")
        return

    # Don't allow adding self
    if str(new_admin_id) == str(database.get_config("admin_tg_id")):
        await update.message.reply_text("❌ You are already the owner.")
        return

    if database.add_admin(new_admin_id):
        await update.message.reply_text(f"✅ User <code>{new_admin_id}</code> added as sub-admin.", parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ User <code>{new_admin_id}</code> is already a sub-admin.", parse_mode='HTML')


async def tg_adminremove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a sub-admin. Owner only, private chat only."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "private":
        await update.message.reply_text("❌ This command can only be used in a private chat with the bot.")
        return

    if not database.is_owner(user.id):
        await update.message.reply_text("❌ Only the bot owner can manage admins.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Usage: <code>/adminremove user_id</code>", parse_mode='HTML')
        return

    try:
        admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid numeric user ID.")
        return

    if database.remove_admin(admin_id):
        await update.message.reply_text(f"✅ User <code>{admin_id}</code> removed from sub-admins.", parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ User <code>{admin_id}</code> is not a sub-admin.", parse_mode='HTML')


async def tg_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all sub-admins. Owner only, private chat only."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "private":
        await update.message.reply_text("❌ This command can only be used in a private chat with the bot.")
        return

    if not database.is_owner(user.id):
        await update.message.reply_text("❌ Only the bot owner can view admins.")
        return

    admins = database.get_all_admins()
    if not admins:
        await update.message.reply_text("👥 No sub-admins configured.\n\nUse <code>/adminadd user_id</code> to add one.", parse_mode='HTML')
        return

    lines = [f"👥 <b>Sub-admins</b> ({len(admins)})\n"]
    for admin_id in admins:
        lines.append(f"• <code>{admin_id}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


async def _check_invite_permissions(update: Update) -> bool:
    """Check permissions for /invite and /inviteqr commands."""
    chat = update.effective_chat
    user = update.effective_user

    # Permission check
    admin_tg_id = database.get_config("admin_tg_id")
    if admin_tg_id:
        if not database.is_owner_or_admin(user.id):
            await update.message.reply_text("❌ Only the bot admin can generate invite links.")
            return False
            
    if chat.type != "private":
        if not admin_tg_id:
            try:
                member = await chat.get_member(user.id)
                if member.status not in ("administrator", "creator"):
                    await update.message.reply_text("❌ Only group admins can generate invite links.")
                    return False
            except Exception:
                pass
            
    return True

async def tg_invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate invite link for the bridged Delta Chat group or the bot itself."""
    if not await _check_invite_permissions(update):
        return

    chat = update.effective_chat
    
    if chat.type == "private":
        try:
            qrdata = dc_bot_instance.rpc.get_chat_securejoin_qr_code(dc_accid, None)
            if qrdata.startswith("OPEN-CHAT:"):
                qrdata = "https://i.delta.chat/#" + qrdata[10:]
            elif qrdata.startswith("OPEN:"):
                qrdata = "https://i.delta.chat/#" + qrdata[5:]
            await update.message.reply_text(f"🔗 <b>Bot Setup Link</b>\n{html.escape(qrdata)}", parse_mode='HTML', disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Failed to generate bot setup link: {e}")
            await update.message.reply_text("❌ Error generating bot setup link.")
        return

    dc_chats = database.get_dc_chats(chat.id)
    if not dc_chats:
        await update.message.reply_text("❌ This group is not bridged to any Delta Chat group.")
        return

    lines = []
    for dc_cid in dc_chats:
        try:
            chat_info = dc_bot_instance.rpc.get_basic_chat_info(dc_accid, dc_cid)
            chat_name = chat_info.get("name") or str(dc_cid)
            
            qrdata = dc_bot_instance.rpc.get_chat_securejoin_qr_code(dc_accid, dc_cid)
            # Make sure it's a clickable i.delta.chat link if using OPEN-CHAT/OPEN protocol
            if qrdata.startswith("OPEN-CHAT:"):
                qrdata = "https://i.delta.chat/#" + qrdata[10:]
            elif qrdata.startswith("OPEN:"):
                qrdata = "https://i.delta.chat/#" + qrdata[5:]
                
            lines.append(f"🔗 Click to join bridged DC Group <b>{html.escape(chat_name)}</b>:\n{html.escape(qrdata)}\n")
        except Exception as e:
            logger.error(f"Failed to generate invite link for DC chat {dc_cid}: {e}")
            lines.append(f"❌ Error generating link for DC Group {dc_cid}.\n")

    await update.message.reply_text("\n".join(lines).strip(), parse_mode='HTML', disable_web_page_preview=True)

async def tg_inviteqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate invite QR code for the bridged Delta Chat group or the bot itself."""
    if not await _check_invite_permissions(update):
        return

    chat = update.effective_chat

    if not qrcode:
        await update.message.reply_text("❌ QR code generation is not supported. Please install 'qrcode[pil]' python package.")
        return

    if chat.type == "private":
        try:
            qrdata = dc_bot_instance.rpc.get_chat_securejoin_qr_code(dc_accid, None)
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(qrdata)
            qr.make(fit=True)
            
            try:
                img = qr.make_image(fill_color="black", back_color="white")
                import io
                bio = io.BytesIO()
                bio.name = 'bot_invite_qr.png'
                img.save(bio, 'PNG')
                bio.seek(0)
                await update.message.reply_photo(
                    photo=bio, 
                    caption="Scan to add bot in Delta Chat"
                )
            except Exception as e:
                logger.error(f"Image generation failed (Pillow might be missing): {e}")
                await update.message.reply_text("❌ Cannot generate image. Ensure 'Pillow' is installed (`pip install Pillow`).")
        except Exception as e:
            logger.error(f"Failed to generate bot setup QR: {e}")
            await update.message.reply_text("❌ Error generating QR code.")
        return

    dc_chats = database.get_dc_chats(chat.id)
    if not dc_chats:
        return

    for dc_cid in dc_chats:
        try:
            chat_info = dc_bot_instance.rpc.get_basic_chat_info(dc_accid, dc_cid)
            chat_name = chat_info.get("name") or str(dc_cid)
            
            qrdata = dc_bot_instance.rpc.get_chat_securejoin_qr_code(dc_accid, dc_cid)
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(qrdata)
            qr.make(fit=True)
            
            try:
                img = qr.make_image(fill_color="black", back_color="white")
                import io
                bio = io.BytesIO()
                bio.name = 'invite_qr.png'
                img.save(bio, 'PNG')
                bio.seek(0)
                await update.message.reply_photo(
                    photo=bio,
                    caption=f"Scan to join bridged DC Group {chat_name}"
                )
            except Exception as e:
                logger.error(f"Image generation failed (Pillow might be missing): {e}")
                await update.message.reply_text("❌ Cannot generate image. Ensure 'Pillow' is installed (`pip install Pillow`).")
                
        except Exception as e:
            logger.error(f"Failed to generate invite QR for DC chat {dc_cid}: {e}")
            await update.message.reply_text(f"❌ Error generating QR code for DC Group {dc_cid}.", parse_mode='HTML')


# ---------------------------------------------------------
# CHANNEL BRIDGING COMMANDS
# ---------------------------------------------------------

async def _check_channel_admin(update: Update) -> bool:
    """Only the bot owner or sub-admin may manage channels. Must be in private chat."""
    chat = update.effective_chat
    user = update.effective_user
    if chat.type != "private":
        await update.message.reply_text("❌ Channel commands can only be used in a private chat with the bot.")
        return False
    admin_tg_id = database.get_config("admin_tg_id")
    if admin_tg_id and not database.is_owner(user.id):
        await update.message.reply_text("❌ Only the bot owner can manage channels.")
        return False
    return True


async def _relay_channel_history(dc_chat_id: int, tg_channel_id: int, ub_target: any, limit: int = 3, invite_link: str = None):
    """Fetch and relay the last N messages from a TG channel as history, with caching."""
    global userbot_client
    if not (userbot_client and userbot_client.is_connected()):
        return

    try:
        now = time.time()
        cached = _history_cache.get(dc_chat_id)
        
        # Check if we have a fresh cache (under 5 minutes old)
        if cached and (now - cached['timestamp'] < HISTORY_RELAY_COOLDOWN):
            logger.debug(f"Using cached history for DC channel {dc_chat_id}")
            history = cached['messages']
        else:
            # Ensure we have an entity Telethon can work with
            ub_entity = None
            try:
                ub_entity = await userbot_client.get_entity(ub_target)
            except Exception as e:
                if invite_link and ("t.me/" in invite_link or "telegram.me/" in invite_link):
                    logger.debug(f"Could not resolve {ub_target} by ID, trying invite link: {e}")
                    try:
                        ub_entity = await userbot_client.get_entity(invite_link)
                    except Exception as e2:
                        logger.debug(f"Could not resolve via link either: {e2}")
                else:
                    # If it's not a TG link or no link at all, we can't do more
                    if not invite_link:
                        raise e
                    else:
                        logger.debug(f"Skipping invite link resolution: {invite_link} is not a TG link.")
                        raise e
            
            if not ub_entity:
                return

            logger.info(f"Fetching fresh history for TG {ub_target}...")
            history = await userbot_client.get_messages(ub_entity, limit=limit)
            if history:
                _history_cache[dc_chat_id] = {
                    'timestamp': now,
                    'messages': history
                }
        
        if history:
            # Reverse to send oldest first
            for msg in reversed(history):
                # Mark as processed to avoid duplicate relay if an event for this message comes in
                _mark_processed(tg_channel_id, msg.id)
                # Small delay to ensure order in DC and avoid rate limits
                await asyncio.sleep(0.5)
                await _relay_userbot_message(dc_chat_id, msg)
    except Exception as e:
        logger.error(f"Failed to relay history for TG {ub_target} to DC {dc_chat_id}: {e}")


async def _relay_channel_history_to_chat(target_dc_chat_id: int, tg_channel_id: int, ub_target: any, limit: int = 3, invite_link: str = None, bot_ref=None, accid=None):
    """
    Fetch and relay the last N TG channel messages to a specific DC chat (e.g. a private chat).
    Uses the same 5-minute cache as _relay_channel_history.
    """
    global userbot_client
    if not (userbot_client and userbot_client.is_connected()):
        return

    try:
        now = time.time()
        # Use dc_chat_id of the channel itself as the cache key
        # ub_target is unique per channel, so we use a string key
        cache_key = f"preview_{tg_channel_id}"
        cached = _history_cache.get(cache_key)

        if cached and (now - cached['timestamp'] < HISTORY_RELAY_COOLDOWN):
            history = cached['messages']
        else:
            ub_entity = None
            try:
                ub_entity = await userbot_client.get_entity(ub_target)
            except Exception as e:
                if invite_link and ("t.me/" in invite_link or "telegram.me/" in invite_link):
                    try:
                        ub_entity = await userbot_client.get_entity(invite_link)
                    except Exception as e2:
                        logger.debug(f"History preview: could not resolve entity: {e2}")
                if not ub_entity:
                    return

            logger.info(f"Fetching fresh history preview for TG {ub_target}...")
            history = await userbot_client.get_messages(ub_entity, limit=limit)
            if history:
                _history_cache[cache_key] = {'timestamp': now, 'messages': history}

        if history:
            if bot_ref and accid:
                bot_ref.rpc.send_msg(accid, target_dc_chat_id, MsgData(text="📜 *Recent posts from this channel:*"))
            for msg in reversed(history):
                _mark_processed(tg_channel_id, msg.id)
                await asyncio.sleep(0.5)
                await _relay_userbot_message(target_dc_chat_id, msg)
    except Exception as e:
        logger.error(f"Failed to relay history preview to DC chat {target_dc_chat_id}: {e}")


async def _add_channel_bridge(target: str, creator_tg_id: int | None = None) -> str:
    """Core logic to bridge a TG channel/group. Shared between TG and DC commands."""
    global dc_bot_instance, dc_accid, userbot_client
    
    if not dc_bot_instance or not dc_accid:
        return "❌ Error: Delta Chat bot not initialized."

    username = target.strip()
    is_numeric = False
    numeric_id = 0
    
    # Support t.me links
    if "t.me/" in username:
        username = username.split('/')[-1]
    
    if username.startswith('@'):
        username = username[1:]
    
    try:
        numeric_id = int(username)
        is_numeric = True
    except ValueError:
        pass
    
    display_name = f"@{username}" if not is_numeric else f"ID {numeric_id}"
    
    try:
        # 1. Resolve entity
        channel_title = ""
        tg_channel_id = None
        resolved_username = username
        avatar_file = None
        bot_api_ok = False
        
        # Try Bot API first (if it's a public channel or bot is member)
        try:
            chat_arg = numeric_id if is_numeric else f"@{username}"
            tg_chat_info = await tg_app.bot.get_chat(chat_arg)
            if tg_chat_info.type not in ("channel", "group", "supergroup"):
                 return f"❌ <code>{html.escape(display_name)}</code> is not a channel or group."
            
            channel_title = tg_chat_info.title or display_name
            tg_channel_id = tg_chat_info.id
            resolved_username = tg_chat_info.username
            if tg_chat_info.photo:
                avatar_file = await tg_chat_info.photo.get_big_file()
            bot_api_ok = True
        except Exception:
            pass

        # Ensure Userbot also "sees" and joins the channel if possible (needed for history)
        if userbot_client and userbot_client.is_connected():
            try:
                # Use resolved username if available, fallback to original input
                ub_arg = numeric_id if is_numeric else (resolved_username or username)
                entity = await userbot_client.get_entity(ub_arg)
                if getattr(entity, 'left', True):
                    if JoinChannelRequest:
                        logger.info(f"Userbot: Joining {channel_title or display_name} for history/relay...")
                        await userbot_client(JoinChannelRequest(entity))
                
                # If Bot API failed, use Userbot info
                if not bot_api_ok:
                    from telethon.utils import get_peer_id
                    tg_channel_id = get_peer_id(entity)
                    channel_title = getattr(entity, 'title', display_name)
                    resolved_username = getattr(entity, 'username', username)
                    bot_api_ok = True
            except Exception as e:
                if not bot_api_ok:
                    return f"❌ Failed to resolve chat (Userbot error): {html.escape(str(e))}"
                logger.debug(f"Userbot could not resolve/join channel (already resolved by Bot API): {e}")

        if not bot_api_ok:
             return f"❌ Could not resolve <code>{html.escape(display_name)}</code> (Bot API failed and Userbot not connected)."

        # 2. Check if already bridged
        existing = database.get_dc_channel_chat_id(tg_channel_id)
        if existing:
            return f"⚠️ Channel {html.escape(channel_title)} is already bridged to DC Chat ID {existing}."

        # 3. Create DC Broadcast Group
        dc_chat_id = dc_bot_instance.rpc.create_broadcast(dc_accid, channel_title)
        
        # Avatar copying
        try:
            if avatar_file:
                avatar_path = f"tmp_avatar_{tg_channel_id}.jpg"
                await avatar_file.download_to_drive(custom_path=avatar_path)
                dc_bot_instance.rpc.set_chat_profile_image(dc_accid, dc_chat_id, avatar_path)
                try: os.unlink(avatar_path)
                except: pass
            elif userbot_client:
                photo = await userbot_client.download_profile_photo(tg_channel_id)
                if photo:
                    dc_bot_instance.rpc.set_chat_profile_image(dc_accid, dc_chat_id, photo)
                    try: os.unlink(photo)
                    except: pass
        except Exception as e:
            logger.warning(f"Could not copy avatar for {channel_title}: {e}")

        # 4. Generate invite link
        invite_link = dc_bot_instance.rpc.get_chat_securejoin_qr_code(dc_accid, dc_chat_id)
        if invite_link.startswith("OPEN-CHAT:"):
            invite_link = "https://i.delta.chat/#" + invite_link[10:]
        elif invite_link.startswith("OPEN:"):
            invite_link = "https://i.delta.chat/#" + invite_link[5:]

        # 5. Save to DB
        row_id = database.add_channel_by_id(tg_channel_id, dc_chat_id, invite_link, username=resolved_username, created_by_tg_id=creator_tg_id)

        if row_id:
            # Register in cooldown so subsequent joins immediately after creation don't trigger history relay again
            _history_cooldowns[dc_chat_id] = time.time()
            
            # Relay last 3 messages as history
            ub_target = resolved_username if resolved_username else tg_channel_id
            await _relay_channel_history(dc_chat_id, tg_channel_id, ub_target, limit=3, invite_link=invite_link)

            # Sync subscriber stats immediately
            try:
                # Use cached username or ID to resolve entity correctly
                ub_target = resolved_username if resolved_username else tg_channel_id
                ub_entity = await userbot_client.get_entity(ub_target)
                await update_tg_channel_stats(row_id, ub_entity)
            except Exception as e:
                logger.debug(f"Failed to sync immediate stats for {channel_title}: {e}")
                
            title_display = f"<b>{html.escape(channel_title)}</b>"
            if resolved_username:
                title_display += f" (@{html.escape(resolved_username)})"
            
            return (
                f"✅ Channel {title_display} bridged!\n\n"
                f"📺 DC Channel: <b>{html.escape(channel_title)}</b>\n"
                f"🆔 Channel #{row_id}\n\n"
                f"🔗 Subscribe in Delta Chat:\n{invite_link}\n\n"
                f"<i>(The last 3 posts have been relayed as history)</i>"
            )
        else:
            return "❌ Failed to save channel to database (may already exist)."

    except Exception as e:
        logger.error(f"Failed to bridge channel: {e}")
        return f"❌ Internal Error: {html.escape(str(e))}"

async def tg_channeladd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bridge a Telegram channel to a new DC broadcast channel."""
    if not await _check_channel_admin(update):
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: <code>/channeladd @name</code>\n"
            "or: <code>/channeladd https://t.me/name</code>\n"
            "or: <code>/channeladd -1001234567890</code>\n\n"
            "Example: <code>/channeladd @ia_panorama</code>\n"
            "Supports both <b>channels</b> and <b>groups</b> in read-only mode.",
            parse_mode='HTML'
        )
        return

    raw_arg = context.args[0].strip()
    status_msg = await update.message.reply_text("⏳ Processing bridge request...")
    
    result = await _add_channel_bridge(raw_arg, creator_tg_id=update.effective_user.id)
    await status_msg.edit_text(result, parse_mode='HTML', disable_web_page_preview=True)
    return

async def tg_userbotsync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger a Userbot subscription sync."""
    if not database.is_owner(update.effective_user.id):
        return
    
    if not (userbot_client and userbot_client.is_connected()):
        await update.message.reply_text("❌ Userbot is not connected.")
        return
    
    await update.message.reply_text("⏳ Starting Userbot synchronization...")
    asyncio.create_task(sync_userbot_channels(force=True))


async def tg_groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all groups the Userbot is in that aren't bridged yet (Owner only)."""
    if not database.is_owner(update.effective_user.id):
        return
    
    if not (userbot_client and userbot_client.is_connected()):
        await update.message.reply_text("❌ Userbot is not connected.")
        return
    
    status_msg = await update.message.reply_text("⏳ Fetching your groups from Telegram...")
    
    try:
        # Get already bridged IDs from BOTH channels and regular bridges
        bridged_channels = database.get_all_channels()
        bridged_ids = {c['tg_channel_id'] for c in bridged_channels if c['tg_channel_id']}
        
        regular_bridges = database.get_all_bridges()
        for b in regular_bridges:
            if len(b) > 1 and b[1]:  # b[1] is tg_chat_id
                bridged_ids.add(b[1])
        
        # Get dialogs via Userbot
        groups = []
        async for dialog in userbot_client.iter_dialogs():
            if dialog.is_group:
                if dialog.id not in bridged_ids:
                    groups.append(dialog)
        
        if not groups:
            await status_msg.edit_text("✅ No new groups found (all are already bridged or you aren't in any groups).")
            return
        
        # Format response
        lines = ["<b>Your Telegram Groups:</b>\n"]
        for g in groups:
            line = f"• <b>{html.escape(g.name)}</b> (ID: <code>{g.id}</code>)\n"
            line += f"  └ Command: <code>/channeladd {g.id}</code>\n"
            lines.append(line)
        
        # Split into chunks if too long (Telegram limit ~4096 chars)
        full_text = "".join(lines)
        if len(full_text) > 4000:
            # Simple split by line
            current_chunk = ""
            for line in lines:
                if len(current_chunk) + len(line) > 4000:
                    await update.message.reply_text(current_chunk, parse_mode='HTML')
                    current_chunk = ""
                current_chunk += line
            await update.message.reply_text(current_chunk, parse_mode='HTML')
            await status_msg.delete()
        else:
            await status_msg.edit_text(full_text, parse_mode='HTML')
            
    except Exception as e:
        logger.error(f"Failed to fetch groups: {e}")
        await status_msg.edit_text(f"❌ Error fetching groups: {html.escape(str(e))}", parse_mode='HTML')


async def tg_channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List bridged channels (owner sees all, sub-admin sees own)."""
    if not await _check_channel_admin(update):
        return

    user = update.effective_user
    if database.is_owner(user.id):
        channels = database.get_all_channels()
    else:
        channels = database.get_channels_by_creator(user.id)

    if not channels:
        await update.message.reply_text("📺 No channels are bridged.")
        return

    lines = [f"📺 <b>Bridged Channels</b> ({len(channels)})\n"]
    for ch in channels:
        dc_cid = ch['dc_chat_id']
        tg_id = ch.get('tg_channel_id', 0)
        r_count = ch.get('reactions_count', 0)
        r_count = ch.get('reactions_count', 0)
        m_count = database.get_bridge_message_count(dc_cid, tg_id)
        tg_sub_count = ch.get('tg_participants_count', 0)
        
        try:
            chat_info = dc_bot_instance.rpc.get_basic_chat_info(dc_accid, dc_cid)
            title = chat_info.get("name", "Unknown Channel")
            # Get subscriber count (minus the bot itself)
            contacts = dc_bot_instance.rpc.get_chat_contacts(dc_accid, dc_cid)
            dc_sub_count = len(contacts) - 1 if contacts else 0
        except Exception:
            title = "Unknown Channel"
            dc_sub_count = "?"

        tg_ref = f"t.me/{ch['tg_channel_username']}" if ch['tg_channel_username'] else f"ID: {ch['tg_channel_id']}"
        stats_str = f"👤 {tg_sub_count:,} TG / {dc_sub_count} DC — 💬 {m_count}"
        lines.append(f"/channel{ch['id']} — <b>{html.escape(title)}</b> ({tg_ref}) — {stats_str}")
    lines.append(f"\nUse <code>/channel N</code> for invite link")
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


async def tg_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show invite link for a specific channel by number."""
    if not await _check_channel_admin(update):
        return

    channel_id = None
    # Support both "/channel 1" and "/channel1"
    cmd = update.message.text.split()[0].lower()
    if len(cmd) > 8 and cmd.startswith("/channel"):
        try:
            channel_id = int(cmd[8:])
        except ValueError:
            pass
            
    if channel_id is None:
        if not context.args or len(context.args) < 1:
            await update.message.reply_text("Usage: <code>/channel N</code> (use /channels to see numbers)", parse_mode='HTML')
            return
        try:
            channel_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Please provide a valid channel number.")
            return

    ch = database.get_channel_by_id(channel_id)
    if not ch:
        await update.message.reply_text(f"❌ Channel #{channel_id} not found.")
        return

    # Sub-admin can only view own channels
    user = update.effective_user
    if not database.is_owner(user.id):
        creator = database.get_channel_creator(channel_id)
        if creator != user.id:
            await update.message.reply_text(f"❌ Channel #{channel_id} not found.")
            return

    invite_link = ch['invite_link'] or "No invite link available"
    if ch['tg_channel_username']:
        name_str = f"@{html.escape(ch['tg_channel_username'])}"
    else:
        name_str = f"ID <code>{ch['tg_channel_id']}</code>"
        
    await update.message.reply_text(
        f"📺 Channel #{ch['id']} — <b>{name_str}</b>\n\n"
        f"🔗 Subscribe in Delta Chat:\n{html.escape(invite_link)}",
        parse_mode='HTML',
        disable_web_page_preview=True
    )


async def tg_channelqr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show QR invite for a specific channel by number."""
    if not await _check_channel_admin(update):
        return

    if not qrcode:
        await update.message.reply_text("❌ QR code generation is not supported. Please install 'qrcode[pil]' python package.")
        return


    channel_id = None
    # Support both "/channelqr 1" and "/channel1qr" and "/channelqr1"
    cmd = update.message.text.split()[0].lower()
    if "qr" in cmd:
        # Extract digits
        import re
        digits = re.findall(r'\d+', cmd)
        if digits:
            try:
                channel_id = int(digits[0])
            except ValueError:
                pass
            
    if channel_id is None:
        if not context.args or len(context.args) < 1:
            await update.message.reply_text("Usage: <code>/channelqr N</code>", parse_mode='HTML')
            return
        try:
            channel_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Please provide a valid channel number.")
            return

    ch = database.get_channel_by_id(channel_id)
    if not ch:
        await update.message.reply_text(f"❌ Channel #{channel_id} not found.")
        return

    # Sub-admin can only view own channels
    user = update.effective_user
    if not database.is_owner(user.id):
        creator = database.get_channel_creator(channel_id)
        if creator != user.id:
            await update.message.reply_text(f"❌ Channel #{channel_id} not found.")
            return

    if not ch['invite_link']:
        await update.message.reply_text("❌ No invite link available for this channel.")
        return

    try:
        # Get the original QR data (securejoin format, not the https link)
        if dc_bot_instance and dc_accid:
            qrdata = dc_bot_instance.rpc.get_chat_securejoin_qr_code(dc_accid, ch['dc_chat_id'])
        else:
            qrdata = ch['invite_link']

        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(qrdata)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        bio = io.BytesIO()
        bio.name = 'channel_invite_qr.png'
        img.save(bio, 'PNG')
        bio.seek(0)
        
        if ch['tg_channel_username']:
            caption = f"Scan to subscribe to @{ch['tg_channel_username']} in Delta Chat"
        else:
            caption = f"Scan to subscribe to channel ID {ch['tg_channel_id']} in Delta Chat"
            
        await update.message.reply_photo(
            photo=bio,
            caption=caption
        )
    except Exception as e:
        logger.error(f"Failed to generate channel QR: {e}")
        await update.message.reply_text("❌ Error generating QR code.")


async def tg_channelremove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a channel bridge."""
    if not await _check_channel_admin(update):
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Usage: <code>/channelremove N</code> (use /channels to see numbers)", parse_mode='HTML')
        return

    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please provide a valid channel number.")
        return

    ch = database.get_channel_by_id(channel_id)
    if not ch:
        await update.message.reply_text(f"❌ Channel #{channel_id} not found.")
        return

    # Sub-admin can only remove own channels
    user = update.effective_user
    if not database.is_owner(user.id):
        creator = database.get_channel_creator(channel_id)
        if creator != user.id:
            await update.message.reply_text(f"❌ Channel #{channel_id} not found.")
            return

    channel_name = ch['tg_channel_username']
    if channel_name:
        display_name = f"@{html.escape(channel_name)}"
    else:
        display_name = f"ID <code>{ch['tg_channel_id']}</code>"

    if database.remove_channel(channel_id):
        await update.message.reply_text(f"✅ Channel #{channel_id} (<b>{display_name}</b>) removed.", parse_mode='HTML')
    else:
        await update.message.reply_text("❌ Failed to remove channel.")


# ---------------------------------------------------------
# CHANNEL POST HANDLER
# ---------------------------------------------------------

async def handle_tg_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay Telegram channel posts to corresponding DC broadcast channels."""
    global dc_bot_instance, dc_accid

    post = update.channel_post
    if not post:
        return

    tg_channel_id = post.chat.id
    if _mark_processed(tg_channel_id, post.message_id):
        return
    
    tg_username = post.chat.username

    # Look up by numeric ID first, then by username
    dc_chat_id = database.get_dc_channel_chat_id(tg_channel_id)

    if not dc_chat_id and tg_username:
        # First post — resolve username to numeric ID (for channels added by @username)
        ch = database.get_channel_by_tg_username(tg_username)
        if ch:
            if not ch.get('tg_channel_id'):
                database.update_channel_tg_id(tg_username, tg_channel_id)
            dc_chat_id = ch['dc_chat_id']

    if not dc_chat_id or not dc_bot_instance or not dc_accid:
        return

    # Rate limit
    if _is_rate_limited(tg_channel_id):
        return

    text = post.text or post.caption or ""
    # Inline hidden links so they are not lost in DC
    entities = post.entities or post.caption_entities or []
    text = _inline_links(text, entities)

    # Author signature (shown for channel posts with signatures enabled)
    author = getattr(post, 'author_signature', None)

    # Detect media
    tg_file = None
    file_name = None
    if post.photo:
        tg_file = post.photo[-1]
        file_name = "photo.jpg"
    elif post.video:
        vid = post.video
        tg_file = vid
        file_name = vid.file_name or "video.mp4"
        v_size = getattr(vid, 'file_size', 0) or 0
        if v_size > 20 * 1024 * 1024:
            qualities = getattr(vid, 'qualities', []) or []
            valid_q = [q for q in qualities if (getattr(q, 'file_size', 0) or 0) < 20 * 1024 * 1024]
            if valid_q:
                valid_q.sort(key=lambda q: getattr(q, 'file_size', 0) or getattr(q, 'height', 0) or 0, reverse=True)
                tg_file = valid_q[0]
                text = (text + f"\n\n[Video was too large ({v_size//1024//1024} MB), forwarded in lower resolution]").strip()
            else:
                tg_file = None
                text = (text + f"\n\n[🎥 Video is too large ({v_size//1024//1024} MB) to be forwarded]").strip()
    elif post.animation:
        tg_file = post.animation
        file_name = "animation.gif"
    elif post.voice:
        tg_file = post.voice
        file_name = "voice.ogg"
    elif post.audio:
        tg_file = post.audio
        file_name = post.audio.file_name or "audio.mp3"
    elif post.document:
        tg_file = post.document
        file_name = post.document.file_name or "file"
    elif post.sticker:
        tg_file = post.sticker
        file_name = "sticker.webp"
    elif post.video_note:
        tg_file = post.video_note
        file_name = "video_note.mp4"

    # Handle location / venue
    if post.venue:
        loc_text = f"📍 Venue: {post.venue.title}\n{post.venue.address}\nhttps://maps.google.com/?q={post.venue.location.latitude},{post.venue.location.longitude}"
        text = (text + "\n\n" + loc_text).strip()
    elif post.location:
        is_live = getattr(post.location, 'live_period', None) is not None
        if is_live:
            live_mins = post.location.live_period // 60
            loc_text = f"📍 Live Location ({live_mins} min)\nhttps://maps.google.com/?q={post.location.latitude},{post.location.longitude}\n\n(Reply with /locupdate to get the latest coordinates)"
            LIVE_LOCATIONS[post.message_id] = (post.location.latitude, post.location.longitude)
        else:
            loc_text = f"📍 Location: https://maps.google.com/?q={post.location.latitude},{post.location.longitude}"
        text = (text + "\n\n" + loc_text).strip()

    # Skip posts with no text and no media
    if not text and not tg_file:
        return

    # Build message text
    formatted_msg = text if text else ""

    # Add link to original Telegram post
    if tg_username:
        formatted_msg = (formatted_msg + f"\n\n🔗 t.me/{tg_username}/{post.message_id}").strip()

    formatted_msg = _truncate(formatted_msg, DC_MAX_MSG_LEN)

    # Download media if present
    local_file_path = None
    if tg_file:
        try:
            tg_file_obj = await tg_file.get_file()
            suffix = os.path.splitext(file_name)[1] if file_name else ""
            tmp_fd, local_file_path = tempfile.mkstemp(suffix=suffix)
            os.close(tmp_fd)
            await retry_async(tg_file_obj.download_to_drive, custom_path=local_file_path)
        except (TimeoutError, asyncio.TimeoutError):
            logger.error(f"Timeout downloading channel media {file_name}")
            local_file_path = None
            formatted_msg += f"\n\n[Failed to download media: {file_name}]"
        except Exception as e:
            logger.error(f"Failed to download channel media: {e}")
            local_file_path = None

    try:
        msg_data = MsgData(text=formatted_msg)
        if author:
            msg_data.override_sender_name = author
        if local_file_path and os.path.exists(local_file_path):
            msg_data.file = local_file_path
        dc_msg_id = dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, msg_data)
        if dc_msg_id:
            c_hash = _get_content_hash(post)
            database.save_message_map(dc_msg_id, dc_chat_id, post.message_id, tg_channel_id, content_hash=c_hash)
        # Register in edit debounce so link-preview "edits" within 60s are suppressed
        _edit_timestamps[(tg_channel_id, post.message_id)] = time.time()
        
        logger.info(f"Relayed channel post from @{tg_username or tg_channel_id} to DC broadcast {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to relay channel post to DC: {e}")
    finally:
        if local_file_path and os.path.exists(local_file_path):
            try:
                os.unlink(local_file_path)
            except Exception:
                pass


async def handle_tg_edited_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay edited Telegram channel posts to DC broadcast with [Edited] prefix."""
    global dc_bot_instance, dc_accid

    post = update.edited_channel_post
    if not post:
        return

    tg_channel_id = post.chat.id
    if _mark_processed(tg_channel_id, post.message_id):
        return
    
    tg_username = post.chat.username

    # Check if this is a live location update
    if post.location:
        is_live = getattr(post.location, 'live_period', None) is not None
        if is_live or post.message_id in LIVE_LOCATIONS:
            lat, lon = post.location.latitude, post.location.longitude
            LIVE_LOCATIONS[post.message_id] = (lat, lon)
            
            if not is_live:
                # Live location ended either manually or expired.
                LIVE_LOCATIONS.pop(post.message_id, None)
                final_text = f"🛑 Live Location Ended\nFinal coordinates: https://maps.google.com/?q={lat},{lon}"
                dc_chat_id = database.get_dc_channel_chat_id(tg_channel_id)
                if not dc_chat_id and tg_username:
                    ch = database.get_channel_by_tg_username(tg_username)
                    if ch:
                        dc_chat_id = ch['dc_chat_id']
                if dc_chat_id and dc_bot_instance and dc_accid:
                    try:
                        dc_reply_id = database.get_dc_msg_id(post.message_id, tg_channel_id, dc_chat_id)
                        msg_data = MsgData(text=final_text)
                        if dc_reply_id:
                            msg_data.quoted_message_id = dc_reply_id
                        dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, msg_data)
                    except Exception as e:
                        logger.error(f"Failed to send final location: {e}")
                return

            if not (post.text or post.caption):
                return

    # Look up DC broadcast
    dc_chat_id = database.get_dc_channel_chat_id(tg_channel_id)
    if not dc_chat_id and tg_username:
        ch = database.get_channel_by_tg_username(tg_username)
        if ch:
            dc_chat_id = ch['dc_chat_id']

    if not dc_chat_id or not dc_bot_instance or not dc_accid:
        return

    # Filter out updates where content (text/caption) hasn't changed (e.g. reactions or view count updates)
    new_hash = _get_content_hash(post)
    old_hash = database.get_message_content_hash(post.message_id, tg_channel_id, dc_chat_id)
    if old_hash and old_hash == new_hash:
        # Content hasn't changed, ignore this "edit"
        return

    # Debounce: max 1 edit relay per message per minute
    if _is_edit_debounced(tg_channel_id, post.message_id):
        return

    text = post.text or post.caption or ""
    entities = post.entities or post.caption_entities or []
    text = _inline_links(text, entities)

    author = getattr(post, 'author_signature', None)

    if not text:
        return

    formatted_msg = text

    # Add link to original Telegram post
    if tg_username:
        formatted_msg = (formatted_msg + f"\n\n🔗 t.me/{tg_username}/{post.message_id}").strip()

    formatted_msg = _truncate(formatted_msg, DC_MAX_MSG_LEN)

    try:
        msg_data = MsgData(text=formatted_msg)
        if author:
            msg_data.override_sender_name = f"✏️ [Edited] {author}"
        else:
            msg_data.override_sender_name = "✏️ [Edited]"
        dc_sent_id = dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, msg_data)
        if dc_sent_id:
            # Update hash mapping (keep the same mapping but with new hash)
            database.save_message_map(dc_sent_id, dc_chat_id, post.message_id, tg_channel_id, content_hash=new_hash)
        logger.info(f"Relayed edited channel post from @{tg_username or tg_channel_id} to DC broadcast {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to relay edited channel post to DC: {e}")


async def handle_tg_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay edited Telegram group messages to Delta Chat with [Edited] prefix."""
    global dc_bot_instance, dc_accid

    msg = update.edited_message
    if not msg:
        return

    tg_chat_id = msg.chat.id
    if _mark_processed(tg_chat_id, msg.message_id):
        return

    # Check if this is a live location update
    if msg.location:
        is_live = getattr(msg.location, 'live_period', None) is not None
        if is_live or msg.message_id in LIVE_LOCATIONS:
            lat, lon = msg.location.latitude, msg.location.longitude
            LIVE_LOCATIONS[msg.message_id] = (lat, lon)
            
            if not is_live:
                # Live location ended
                LIVE_LOCATIONS.pop(msg.message_id, None)
                final_text = f"🛑 Live Location Ended\nFinal coordinates: https://maps.google.com/?q={lat},{lon}"
                dc_chats = database.get_dc_chats(tg_chat_id)
                if dc_chats and dc_bot_instance and dc_accid:
                    for dc_chat_id in dc_chats:
                        dc_reply_id = database.get_dc_msg_id(msg.message_id, tg_chat_id, dc_chat_id)
                        msg_data = MsgData(text=final_text)
                        if dc_reply_id:
                            msg_data.quoted_message_id = dc_reply_id
                        try:
                            await _wait_for_global_dc_rate_limit()
                            dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, msg_data)
                        except Exception as e:
                            logger.error(f"Failed to send final location edit: {e}")
                return

            if not (msg.text or msg.caption):
                return

    # Only bridge group chats
    if msg.chat.type == "private":
        return

    # Skip bot messages
    if msg.from_user and msg.from_user.is_bot:
        return

    dc_chats = database.get_dc_chats(tg_chat_id)
    if not dc_chats or not dc_bot_instance or not dc_accid:
        return

    # Filter out updates where content (text/caption) hasn't changed
    new_hash = _get_content_hash(msg)
    # We check against the first bridged DC chat; usually hashes are sync'd.
    old_hash = database.get_message_content_hash(msg.message_id, tg_chat_id, dc_chats[0])
    if old_hash and old_hash == new_hash:
        return

    # Debounce: max 1 edit relay per message per minute
    if _is_edit_debounced(tg_chat_id, msg.message_id):
        return

    sender = msg.from_user
    sender_name = sender.first_name
    if sender.last_name:
        sender_name += f" {sender.last_name}"

    text = msg.text or msg.caption or ""
    
    # Filter out commands in edits ONLY if no media
    if text.startswith('/') and not msg.photo and not msg.video and not msg.document:
        return

    entities = msg.entities or msg.caption_entities or []
    text = _inline_links(text, entities)

    if not text:
        return

    formatted_msg = f"✏️ [Edited]:\n{text}"
    formatted_msg = _truncate(formatted_msg, DC_MAX_MSG_LEN)

    for dc_chat_id in dc_chats:
        try:
            msg_data = MsgData(text=formatted_msg)
            msg_data.override_sender_name = sender_name
            await _wait_for_global_dc_rate_limit()
            dc_sent_id = dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, msg_data)
            if dc_sent_id:
                database.save_message_map(dc_sent_id, dc_chat_id, msg.message_id, tg_chat_id, content_hash=new_hash)
            logger.info(f"Relayed edited TG msg to DC chat {dc_chat_id}")
        except Exception as e:
            logger.error(f"Failed to relay edited msg to DC chat {dc_chat_id}: {e}")


async def handle_tg_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay Telegram messages to Delta Chat."""
    global dc_bot_instance, dc_accid

    if not update.effective_chat or not update.message:
        return

    tg_chat_id = update.effective_chat.id
    if _mark_processed(tg_chat_id, update.message.message_id):
        return

    # Only bridge group chats
    if update.effective_chat.type == "private":
        return

    # Skip messages from bots (including self) to prevent echo loops
    if update.message.from_user and update.message.from_user.is_bot:
        return

    dc_chats = database.get_dc_chats(tg_chat_id)
    if not dc_chats or not dc_bot_instance or not dc_accid:
        return

    # Rate limit check
    if _is_rate_limited(tg_chat_id):
        return

    sender = update.message.from_user
    sender_name = sender.first_name
    if sender.last_name:
        sender_name += f" {sender.last_name}"

    text = update.message.text or update.message.caption or ""
    
    # Filter out commands ONLY if it's likely a bot command and not a post with media
    if text.startswith('/') and not update.message.photo and not update.message.video and not update.message.document:
        return

    # Inline hidden links so they are not lost in DC
    entities = update.message.entities or update.message.caption_entities or []
    text = _inline_links(text, entities)

    # Detect and format polls
    if update.message.poll:
        poll = update.message.poll
        poll_text = f"📊 {poll.question}\n"
        for option in poll.options:
            poll_text += f"▫️ {option.text}\n"
        text = (text + "\n\n" + poll_text).strip()
        
        # Save context so we can update DC when the poll closes
        for dc_chat_id in dc_chats:
            database.save_poll_context(poll.id, tg_chat_id, dc_chat_id)

    # Detect media
    tg_file = None
    file_name = None
    if update.message.photo:
        tg_file = update.message.photo[-1]  # Largest resolution
        file_name = "photo.jpg"
    elif update.message.video:
        vid = update.message.video
        tg_file = vid
        file_name = vid.file_name or "video.mp4"
        v_size = getattr(vid, 'file_size', 0) or 0
        if v_size > 20 * 1024 * 1024:
            qualities = getattr(vid, 'qualities', []) or []
            valid_q = [q for q in qualities if (getattr(q, 'file_size', 0) or 0) < 20 * 1024 * 1024]
            if valid_q:
                valid_q.sort(key=lambda q: getattr(q, 'file_size', 0) or getattr(q, 'height', 0) or 0, reverse=True)
                tg_file = valid_q[0]
                text = (text + f"\n\n[Video was too large ({v_size//1024//1024} MB), forwarded in lower resolution]").strip()
            else:
                tg_file = None
                text = (text + f"\n\n[🎥 Video is too large ({v_size//1024//1024} MB) to be forwarded]").strip()
    elif update.message.animation:  # GIF
        tg_file = update.message.animation
        file_name = "animation.gif"
    elif update.message.voice:
        tg_file = update.message.voice
        file_name = "voice.ogg"
    elif update.message.audio:
        tg_file = update.message.audio
        file_name = update.message.audio.file_name or "audio.mp3"
    elif update.message.document:
        tg_file = update.message.document
        file_name = update.message.document.file_name or "file"
    elif update.message.sticker:
        tg_file = update.message.sticker
        file_name = "sticker.webp"
    elif update.message.video_note:
        tg_file = update.message.video_note
        file_name = "video_note.mp4"

    # Handle location / venue
    if update.message.venue:
        loc_text = f"📍 Venue: {update.message.venue.title}\n{update.message.venue.address}\nhttps://maps.google.com/?q={update.message.venue.location.latitude},{update.message.venue.location.longitude}"
        text = (text + "\n\n" + loc_text).strip()
    elif update.message.location:
        is_live = getattr(update.message.location, 'live_period', None) is not None
        if is_live:
            live_mins = update.message.location.live_period // 60
            loc_text = f"📍 Live Location ({live_mins} min)\nhttps://maps.google.com/?q={update.message.location.latitude},{update.message.location.longitude}\n\n(Reply with /locupdate to get the latest coordinates)"
            LIVE_LOCATIONS[update.message.message_id] = (update.message.location.latitude, update.message.location.longitude)
        else:
            loc_text = f"📍 Location: https://maps.google.com/?q={update.message.location.latitude},{update.message.location.longitude}"
        text = (text + "\n\n" + loc_text).strip()

    # Skip messages with no text and no media
    if not text and not tg_file:
        return

    # Check if this is a reply to another message
    reply_prefix = ""
    tg_reply_to_msg_id = None
    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        tg_reply_to_msg_id = replied.message_id
        replied_text = replied.text or replied.caption or ""
        if replied_text:
            short_quote = _truncate(replied_text, 80)
            reply_prefix = f"↩ {short_quote}\n"

    # Download the TG file to a temp path if present
    local_file_path = None
    timeout_error_text = ""
    if tg_file:
        suffix = os.path.splitext(file_name)[1] if file_name else ""
        f_size = getattr(tg_file, 'file_size', 0) or 0
        
        # If file is potentially too large for Bot API, try Userbot first
        if f_size > 20 * 1024 * 1024:
            local_file_path = await _download_via_userbot(tg_chat_id, update.message.message_id, suffix=suffix)

        if not local_file_path:
            # Fallback (or first attempt if < 20MB) using regular Bot API
            try:
                tg_file_obj = await tg_file.get_file()
                tmp_fd, local_file_path_tmp = tempfile.mkstemp(suffix=suffix)
                os.close(tmp_fd)
                await retry_async(tg_file_obj.download_to_drive, custom_path=local_file_path_tmp)
                local_file_path = local_file_path_tmp
            except (TimeoutError, asyncio.TimeoutError):
                logger.error(f"Timeout downloading file {file_name} after retries")
                local_file_path = None
                timeout_error_text = f"\n\n*[Failed to download media: {html.escape(file_name)} - timeout exceeded after retries]*"
            except Exception as e:
                # If it's the "File is too big" error from Telegram
                if "File is too big" in str(e) and not local_file_path:
                    # Final attempt with Userbot if we haven't tried yet
                    local_file_path = await _download_via_userbot(tg_chat_id, update.message.message_id, suffix=suffix)
                
                if not local_file_path:
                    logger.error(f"Failed to download TG file {file_name}: {e}")
                    local_file_path = None



    try:
        for dc_chat_id in dc_chats:
            try:
                dc_reply_id = None
                if tg_reply_to_msg_id:
                    dc_reply_id = database.get_dc_msg_id(tg_reply_to_msg_id, tg_chat_id, dc_chat_id)

                chat_reply_prefix = reply_prefix if not dc_reply_id else ""
                
                formatted_msg = f"{chat_reply_prefix}{text}" if text else ""
                formatted_msg = _truncate(formatted_msg, DC_MAX_MSG_LEN)
                if timeout_error_text:
                    formatted_msg += timeout_error_text

                msg_data = MsgData(text=formatted_msg)
                msg_data.override_sender_name = sender_name
                if dc_reply_id:
                    msg_data.quoted_message_id = dc_reply_id
                if local_file_path and os.path.exists(local_file_path):
                    msg_data.file = local_file_path
                try:
                    await _wait_for_global_dc_rate_limit()
                    sent_msg_id = dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, msg_data)
                except Exception as e:
                    # If quoting failed because the message was deleted in DC, retry without the quote
                    if "does not exist" in str(e).lower() and "message" in str(e).lower():
                        logger.warning(f"Quoted message {dc_reply_id} not found in DC chat {dc_chat_id}. Retrying without quote.")
                        msg_data.quoted_message_id = None
                        # Add a hint to the text that it was a reply to something now missing
                        if chat_reply_prefix:
                           # Already has the prefix if dc_reply_id was NOT found initially
                           pass
                        else:
                            # We thought we had a dc_reply_id, but it's gone from DC DB.
                            # Add the "↩" prefix back to the text
                            tg_reply_to_msg_id = update.message.reply_to_message.message_id
                            replied = update.message.reply_to_message
                            replied_text = replied.text or replied.caption or ""
                            if replied_text:
                                short_quote = _truncate(replied_text, 80)
                                chat_reply_prefix = f"↩ {short_quote}\n"
                                msg_data.text = _truncate(f"{chat_reply_prefix}{text}" if text else "", DC_MAX_MSG_LEN)
                        
                        await _wait_for_global_dc_rate_limit()
                        sent_msg_id = dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, msg_data)
                    else:
                        raise  # Re-raise other exceptions
                
                if sent_msg_id:
                    c_hash = _get_content_hash(update.message)
                    database.save_message_map(sent_msg_id, dc_chat_id, update.message.message_id, tg_chat_id, content_hash=c_hash)
                # Register in edit debounce so link-preview "edits" within 60s are suppressed
                _edit_timestamps[(tg_chat_id, update.message.message_id)] = time.time()
                logger.info(f"Relayed TG msg to DC chat {dc_chat_id}")
            except Exception as e:
                logger.error(f"Failed to relay msg to DC chat {dc_chat_id}: {e}")
    finally:
        # Clean up temp file
        if local_file_path and os.path.exists(local_file_path):
            try:
                os.unlink(local_file_path)
            except Exception:
                pass

async def handle_tg_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle poll updates (e.g. when a poll is closed)."""
    global dc_bot_instance, dc_accid
    
    poll = update.poll
    if not poll or not poll.is_closed:
        return

    ctx = database.get_poll_context(poll.id)
    if not ctx:
        return
        
    tg_chat_id, dc_chat_id = ctx
    
    # Check if bridged and not rate limited
    if not dc_bot_instance or not dc_accid:
        return
    if _is_rate_limited(tg_chat_id):
        return

    # Format final results
    total_voter_count = poll.total_voter_count
    
    text = f"🏁 **Poll closed:** {html.escape(poll.question)}\n\n"
    
    # Sort options by voter count descending
    sorted_options = sorted(poll.options, key=lambda x: x.voter_count, reverse=True)
    
    for option in sorted_options:
        percentage = (option.voter_count / total_voter_count * 100) if total_voter_count > 0 else 0
        text += f"▫️ {html.escape(option.text)} — {option.voter_count} votes ({percentage:.1f}%)\n"
        
    text += f"\n*Total votes: {total_voter_count}*"
    
    try:
        msg_data = MsgData(text=text)
        dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, msg_data)
        logger.info(f"Relayed TG poll results to DC chat {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to relay poll results to DC chat {dc_chat_id}: {e}")

async def handle_tg_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay Telegram reactions to Delta Chat."""
    global dc_bot_instance, dc_accid
    if not dc_bot_instance or not dc_accid:
        return
        
    reaction_update = update.message_reaction
    if not reaction_update:
        return

    # Ignore events triggered by the bot itself to prevent echo loops
    if reaction_update.user and reaction_update.user.id == context.bot.id:
        return
        
    tg_chat_id = reaction_update.chat.id
    tg_msg_id = reaction_update.message_id
    logger.info(f"TG Reaction update in {tg_chat_id} msg {tg_msg_id} from user {reaction_update.user.id if reaction_update.user else 'unknown'}")
    
    dc_chats = database.get_dc_chats(tg_chat_id)
    if not dc_chats:
        chan_dc_id = database.get_dc_channel_chat_id(tg_chat_id)
        if chan_dc_id:
            dc_chats = [chan_dc_id]
        else:
            return
        
    primary_emoji = None
    if reaction_update.new_reaction:
        from telegram import ReactionTypeEmoji
        for r in reaction_update.new_reaction:
            if isinstance(r, ReactionTypeEmoji):
                primary_emoji = r.emoji
                break
                
    for dc_chat_id in dc_chats:
        dc_msg_id = database.get_dc_msg_id(tg_msg_id, tg_chat_id, dc_chat_id)
        if dc_msg_id:
            try:
                # send_reaction expects a list of emojis; empty list to clear
                emoji_list = [primary_emoji] if primary_emoji else []
                dc_bot_instance.rpc.send_reaction(dc_accid, dc_msg_id, emoji_list)
                if primary_emoji:
                    if database.get_dc_channel_chat_id(tg_chat_id):
                        database.increment_channel_reaction_count(tg_chat_id)
                    else:
                        database.increment_bridge_reaction_count(dc_chat_id, tg_chat_id)
                logger.info(f"Relayed TG reaction '{primary_emoji or '(cleared)'}' to DC chat {dc_chat_id}")
            except Exception as e:
                logger.error(f"Failed to relay TG reaction to DC: {e}")

async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Notify owner and sub-admins when the bot is added as admin to a channel."""
    member_update = update.my_chat_member
    if not member_update:
        return

    chat = member_update.chat
    new_status = member_update.new_chat_member.status if member_update.new_chat_member else None

    # Only interested in becoming admin in channels
    if chat.type != "channel" or new_status != "administrator":
        return

    admin_tg_id = database.get_config("admin_tg_id")
    if not admin_tg_id:
        return  # No admin configured — don't leak info

    channel_title = chat.title or "Unknown"
    channel_id = chat.id
    channel_username = chat.username

    # Check if already bridged
    already = database.get_channel_by_tg_id(channel_id)
    if already:
        return  # Already bridged, no need to notify

    if channel_username:
        msg_text = (
            f"📺 I was added as admin to channel <b>{html.escape(channel_title)}</b> "
            f"(@{html.escape(channel_username)}, ID: <code>{channel_id}</code>).\n\n"
            f"To bridge it:\n"
            f"<code>/channeladd @{html.escape(channel_username)}</code>\n"
            f"or\n"
            f"<code>/channeladd {channel_id}</code>"
        )
    else:
        msg_text = (
            f"📺 I was added as admin to private channel <b>{html.escape(channel_title)}</b> "
            f"(ID: <code>{channel_id}</code>).\n\n"
            f"To bridge it:\n"
            f"<code>/channeladd {channel_id}</code>"
        )

    # Notify owner only (not sub-admins — channels may be private)
    try:
        await context.bot.send_message(chat_id=int(admin_tg_id), text=msg_text, parse_mode='HTML')
        logger.info(f"Notified owner about new channel: {channel_title} ({channel_id})")
    except Exception as e:
        logger.error(f"Failed to notify owner about channel {channel_id}: {e}")


async def handle_tg_migration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle group -> supergroup migration by updating stored chat IDs."""
    msg = update.message
    if not msg:
        return

    old_id = msg.chat.id
    new_id = msg.migrate_to_chat_id
    if not new_id:
        # This is the message in the NEW chat with migrate_from_chat_id
        old_id = msg.migrate_from_chat_id
        new_id = msg.chat.id
        if not old_id:
            return

    logger.info(f"TG group migrated: {old_id} -> {new_id}")
    database.update_bridge_tg_chat_id(old_id, new_id)

# ---------------------------------------------------------
# MAIN ASYNC RUNNER
# ---------------------------------------------------------

async def tg_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and handle specific network exceptions."""
    exc = context.error
    exc_str = str(exc)
    exc_type = type(exc).__name__

    # Handle group -> supergroup migration errors
    from telegram.error import ChatMigrated
    if isinstance(exc, ChatMigrated):
        new_id = exc.new_chat_id
        old_id = None
        if isinstance(update, Update) and update.effective_chat:
            old_id = update.effective_chat.id
        if old_id and new_id:
            logger.info(f"TG group migrated (from error): {old_id} -> {new_id}")
            database.update_bridge_tg_chat_id(old_id, new_id)
        else:
            logger.warning(f"ChatMigrated error but could not determine old chat ID: {exc_str}")
        return

    # Common network/timeout errors that we handle via retries or just want to log less noisily
    network_errors = ("ReadTimeout", "ConnectTimeout", "ProxyError", "NetworkError", "RemoteProtocolError")
    is_network = any(err in exc_str or err in exc_type for err in network_errors) or "Server disconnected" in exc_str

    if is_network:
        logger.warning(f"Telegram network error: {exc_type}: {exc_str}")
        return

    logger.error(f"Telegram Application Error [{exc_type}]: {exc_str}", exc_info=exc)

def start_dc_bot():
    """Runs the DC Bot CLI (it is blocking)"""
    try:
        dc_cli.start()
    except SystemExit:
        pass
    except Exception as e:
        logger.error(f"DC CLI error: {e}")


async def db_cleanup_loop():
    while True:
        try:
            database.cleanup_old_messages()
            await asyncio.sleep(3600)  # Every hour
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            await asyncio.sleep(60)

# ---------------------------------------------------------
# TELETHON USERBOT HANDLERS
# ---------------------------------------------------------

_is_syncing_userbot = False

async def update_tg_channel_stats(channel_id: int, tg_peer):
    """Fetch real participant count and username from TG and save to DB."""
    if not (userbot_client and userbot_client.is_connected()):
        return
    
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest
        from telethon.tl.types import Channel, Chat
        
        full = None
        username = getattr(tg_peer, 'username', None)
        
        if isinstance(tg_peer, Channel):
             full = await userbot_client(GetFullChannelRequest(tg_peer))
        elif isinstance(tg_peer, Chat):
             full = await userbot_client(GetFullChatRequest(tg_peer.id))
        
        count = 0
        if full and hasattr(full, 'full_chat'):
            count = getattr(full.full_chat, 'participants_count', 0)
        
        # Update both count and username (if we found one now)
        database.update_channel_info(channel_id, participants_count=count, username=username)
        return count
    except Exception as e:
        logger.warning(f"Failed to fetch stats for TG peer: {e}")
    return 0

async def sync_userbot_channels(force=False):
    """
    Ensures the userbot is a member of all bridged channels.
    Runs when a new account is detected or manually triggered.
    """
    global userbot_client, _is_syncing_userbot
    if not (userbot_client and userbot_client.is_connected()):
        return
    
    if _is_syncing_userbot and not force:
        logger.info("Userbot sync is already in progress.")
        return

    _is_syncing_userbot = True
    try:
        me = await userbot_client.get_me()
        if not me:
            return

        logger.info(f"Starting Userbot sync for account {me.id} (@{getattr(me, 'username', 'N/A')})...")
        channels = database.get_all_channels()
        
        joined_count = 0
        failed_reports = []
        
        for chan in channels:
            tg_id = chan.get('tg_channel_id')
            username = chan.get('tg_channel_username')
            invite_link = chan.get('invite_link')
            
            # For a new account, we MUST use the @username if available, 
            # because numeric IDs are not resolvable until the account "sees" the channel.
            target = (f"@{username}" if username else None) or tg_id
            
            if not target:
                continue

            try:
                # 1. Try to get entity normally (via @username or numeric ID if seen before)
                entity = None
                try:
                    entity = await userbot_client.get_entity(target)
                except Exception as e:
                    # 2. Fallback to invite link if available
                    if invite_link and ("t.me/" in invite_link or "telegram.me/" in invite_link):
                        logger.debug(f"Sync: Could not resolve {target} by ID, trying invite link: {e}")
                        try:
                            entity = await userbot_client.get_entity(invite_link)
                        except Exception as e2:
                            logger.debug(f"Sync: Could not resolve via link either: {e2}")
                    
                if not entity:
                    # Reraise original exception if both failed
                    raise Exception(f"Could not resolve {target} (and no working invite link)")
                
                if getattr(entity, 'left', True):
                    logger.info(f"Userbot: Joining channel {target}...")
                    if JoinChannelRequest:
                        await userbot_client(JoinChannelRequest(entity))
                        joined_count += 1
                        # Update stats after joining
                        chan_id = chan.get('id')
                        if chan_id:
                            await update_tg_channel_stats(chan_id, entity)
                        # Human-like delay
                        delay = random.uniform(5, 20)
                        await asyncio.sleep(delay)
                else:
                    # Already a member, update stats
                    chan_id = chan.get('id')
                    if chan_id:
                        await update_tg_channel_stats(chan_id, entity)
            except Exception as e:
                logger.warning(f"Userbot: Could not sync/join channel {target}: {e}")
                chan_id = chan.get('id', '?')
                tg_id = chan.get('tg_channel_id', '?')
                username = chan.get('tg_channel_username')
                # Try to get title from DB if we know it
                title = "Unknown Channel"
                try:
                    # We might have stored the title in our DB previously
                    # But if not, we just use the ID
                    title = f"Channel #{chan_id}"
                except Exception:
                    pass
                
                # Make ID clickable/searchable
                clean_id = str(tg_id).replace("-100", "")
                report = f"• <b>{html.escape(title)}</b> (ID: <code>{tg_id}</code>)"
                if username:
                    report += f" (@{html.escape(username)})"
                else:
                    report += f" — <a href='https://t.me/c/{clean_id}/1'>Search Link</a>"
                failed_reports.append(report)
        
        # Save last successful sync user ID
        database.set_config("userbot_last_user_id", str(me.id))
        logger.info(f"Userbot sync completed. Joined {joined_count} channels.")
        
        # Notify owner if there are failed channels (skip repeating warnings on periodic syncs)
        if failed_reports and (force or joined_count > 0):
            admin_tg_id = database.get_config("admin_tg_id")
            if admin_tg_id and tg_app:
                try:
                    report_text = (
                        f"⚠️ <b>Userbot Sync Summary</b>\n\n"
                        f"Completed. Joined {joined_count} channels.\n"
                        f"The following {len(failed_reports)} channels require <b>manual join</b> on your new technical account:\n\n"
                        + "\n".join(failed_reports) +
                        f"\n\n<i>Note: Click the ID links to find the group if you are already a member, or search for these IDs in your Telegram history.</i>"
                    )
                    # Chunk it if too long
                    if len(report_text) > 4000:
                        report_text = report_text[:3900] + "...\n(List too long)"
                    
                    await tg_app.bot.send_message(chat_id=int(admin_tg_id), text=report_text, parse_mode='HTML')
                except Exception as notify_e:
                    logger.error(f"Failed to send sync report to owner: {notify_e}")
        
    finally:
        _is_syncing_userbot = False

async def _relay_userbot_message(dc_chat_id, msg, is_edit=False, display_author=None):
    """Core logic to relay a Telethon message (from event or history) to Delta Chat."""
    global dc_bot_instance, dc_accid, userbot_client
    if not (userbot_client and userbot_client.is_connected()):
        return

    tg_channel_id = msg.chat_id
    text = msg.text or ""
    
    # Filter out commands in Userbot mode
    if text.startswith('/') and not msg.media:
        return

    # Base formatted message
    formatted_msg = text
    
    # Add link to original post
    edit_prefix = "✏️ [Edited]" if is_edit else ""
    if msg.chat and getattr(msg.chat, 'username', None):
        formatted_msg = (formatted_msg + f"\n\n🔗 t.me/{msg.chat.username}/{msg.id}").strip()

    if not formatted_msg and not msg.media:
        return

    formatted_msg = _truncate(formatted_msg, DC_MAX_MSG_LEN)

    # Note: downloading media with Telethon if needed
    file_path = None
    if msg.media:
        try:
            media_size = 0
            if hasattr(msg, 'file') and msg.file:
                media_size = msg.file.size or 0
                
            if media_size > 50 * 1024 * 1024:
                logger.warning(f"Userbot: Media in channel {tg_channel_id} is too large: {media_size // 1024 // 1024}MB > 50MB")
                formatted_msg += f"\n\n[Media is too large to be forwarded (limit 50MB)]"
            else:
                suffix = getattr(msg.file, 'ext', "") if hasattr(msg, 'file') and msg.file else ""
                tmp_fd, file_path_tmp = tempfile.mkstemp(suffix=suffix)
                os.close(tmp_fd)
                file_path = await userbot_client.download_media(msg.media, file=file_path_tmp)
        except Exception as e:
            logger.error(f"Failed to download userbot media: {e}")

    try:
        msg_data = MsgData(text=formatted_msg)
        
        # Determine author if not provided
        if not display_author:
            if hasattr(msg, 'post_author') and msg.post_author:
                display_author = msg.post_author
            else:
                sender = await msg.get_sender()
                if sender:
                    if hasattr(sender, 'first_name'):
                        display_author = f"{sender.first_name} {getattr(sender, 'last_name', '') or ''}".strip()
                    elif hasattr(sender, 'title'):
                        display_author = sender.title

        if display_author:
            msg_data.override_sender_name = display_author if not is_edit else f"✏️ [Edited] {display_author}"
        elif is_edit:
            msg_data.override_sender_name = "✏️ [Edited]"
        
        if file_path:
            msg_data.file = file_path
            
        await _wait_for_global_dc_rate_limit()
        sent_id = dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, msg_data)
        if sent_id:
            c_hash = _get_content_hash(msg)
            database.save_message_map(sent_id, dc_chat_id, msg.id, tg_channel_id, content_hash=c_hash)
        logger.info(f"Relayed userbot {'edited ' if is_edit else ''}post from {tg_channel_id} to DC chat {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to relay userbot message: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
            except Exception:
                pass

async def _process_userbot_event(event, is_edit=False):
    """Common logic for userbot new and edited posts."""
    global dc_bot_instance, dc_accid, _userbot_tasks
    
    # Track this task to allow cancellation on restart
    current_task = asyncio.current_task()
    if current_task:
        _userbot_tasks.add(current_task)
    
    try:
        await _process_userbot_event_internal(event, is_edit)
    finally:
        if current_task:
            _userbot_tasks.discard(current_task)

async def _process_userbot_event_internal(event, is_edit=False):
    """Internal logic for userbot events."""
    global dc_bot_instance, dc_accid
    msg = event.message
    if not msg:
        return

    # Safety check: ensure client is still active
    if not (userbot_client and userbot_client.is_connected()):
        return

    tg_channel_id = msg.chat_id
    if _mark_processed(tg_channel_id, msg.id):
        return

    if is_edit and _is_edit_debounced(tg_channel_id, msg.id):
        return

    # Check database
    dc_chat_id = database.get_dc_channel_chat_id(tg_channel_id)

    # Fallback: if not found by ID, try finding by username (useful if added via Bot API without ID)
    if not dc_chat_id:
        chat_username = getattr(msg.chat, 'username', None)
        if chat_username:
            chan_data = database.get_channel_by_tg_username(chat_username)
            if chan_data:
                dc_chat_id = chan_data['dc_chat_id']
                logger.info(f"USERBOT: Found matching channel by username @{chat_username}. Updating numeric ID to {tg_channel_id}...")
                database.update_channel_tg_id(chat_username, tg_channel_id)

    if not dc_chat_id or not dc_bot_instance or not dc_accid:
        return

    # Content-based change detection (prevent ghost edits from reactions/views)
    new_hash = _get_content_hash(msg)
    old_hash = database.get_message_content_hash(msg.id, tg_channel_id, dc_chat_id)
    
    if is_edit and old_hash and old_hash == new_hash:
        # Content hasn't changed, ignore metadata update (reactions, views)
        return

    # Relay the message using the shared helper
    await _relay_userbot_message(dc_chat_id, msg, is_edit=is_edit)

async def start_userbot():
    """Initialize and start the Telethon Userbot client."""
    global userbot_client, _userbot_tasks
    if not TelegramClient:
        return
    
    api_id = database.get_config("api_id")
    api_hash = database.get_config("api_hash")
    if not (api_id and api_hash):
        return

    try:
        # 1. Stop and clear all pending tasks from the old client
        if _userbot_tasks:
            logger.info(f"Cancelling {len(_userbot_tasks)} pending Userbot tasks during restart...")
            for task in _userbot_tasks:
                if not task.done():
                    task.cancel()
            
            # Wait for tasks to actually exit
            await asyncio.gather(*_userbot_tasks, return_exceptions=True)
            _userbot_tasks.clear()

        # 2. Disconnect existing client
        if userbot_client:
            try:
                # Use a timeout to ensure we don't hang during shutdown
                await asyncio.wait_for(userbot_client.disconnect(), timeout=10)
            except Exception as e:
                logger.warning(f"Error disconnecting old Userbot client: {e}")
            finally:
                userbot_client = None
        
        logger.info("Initializing new Telethon Userbot client...")
        userbot_client = TelegramClient('userbot_session', int(api_id), api_hash, sequential_updates=False)
        
        @userbot_client.on(tg_events.NewMessage())
        async def on_new_userbot_msg(event):
            asyncio.create_task(_process_userbot_event(event, is_edit=False))
            
        @userbot_client.on(tg_events.MessageEdited())
        async def on_edited_userbot_msg(event):
            asyncio.create_task(_process_userbot_event(event, is_edit=True))

        await userbot_client.start()
        logger.info("Telethon Userbot client started successfully.")
        
        # Auto-sync detection
        me = await userbot_client.get_me()
        if me:
            last_id = database.get_config("userbot_last_user_id")
            if last_id != str(me.id):
                logger.info(f"New Userbot account detected ({me.id}). Triggering auto-sync...")
                database.set_config("userbot_last_user_id", str(me.id))
                asyncio.create_task(sync_userbot_channels())
    except Exception as e:
        logger.error(f"Failed to start Userbot client: {e}")
        userbot_client = None

async def main():
    global tg_app, main_loop

    args = sys.argv[:]
    is_serve = "serve" in args

    if not is_serve:
        start_dc_bot()
        return

    token = database.get_config("telegram_token") or os.environ.get("TELEGRAM_TOKEN")

    if not token:
        print("Error: Telegram token not found. Setup using 'python bot.py init tg <TOKEN>'.")
        sys.exit(1)

    main_loop = asyncio.get_running_loop()

    # 1. Setup Telegram Application
    tg_app = Application.builder().token(token).build()
    tg_app.add_error_handler(tg_error_handler)
    tg_app.add_handler(CommandHandler("start", tg_start_command))
    tg_app.add_handler(CommandHandler("help", tg_help_command))
    tg_app.add_handler(CommandHandler("donate", tg_donate_command))
    tg_app.add_handler(CommandHandler("id", tg_id_command))
    tg_app.add_handler(CommandHandler("stats", tg_stats_command))
    tg_app.add_handler(CommandHandler("invite", tg_invite_command))
    tg_app.add_handler(CommandHandler("inviteqr", tg_inviteqr_command))
    # Bridge commands (TG side)
    tg_app.add_handler(CommandHandler("bridge", tg_bridge_command))
    tg_app.add_handler(CommandHandler("unbridge", tg_unbridge_command))
    # Admin management commands
    tg_app.add_handler(CommandHandler("adminadd", tg_adminadd_command))
    tg_app.add_handler(CommandHandler("adminremove", tg_adminremove_command))
    tg_app.add_handler(CommandHandler("admins", tg_admins_command))
    # Channel bridging commands
    tg_app.add_handler(CommandHandler("channeladd", tg_channeladd_command))
    tg_app.add_handler(CommandHandler("channels", tg_channels_command))
    tg_app.add_handler(CommandHandler("groups", tg_groups_command))
    tg_app.add_handler(CommandHandler("channel", tg_channel_command))
    tg_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/channel\d+$'), tg_channel_command))
    tg_app.add_handler(CommandHandler("channelqr", tg_channelqr_command))
    tg_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/channel\d+qr$'), tg_channelqr_command))
    tg_app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r'^/channelqr\d+$'), tg_channelqr_command))
    tg_app.add_handler(CommandHandler("channelremove", tg_channelremove_command))
    tg_app.add_handler(CommandHandler("userbotsync", tg_userbotsync_command))

    # Handler for bot being added to / removed from chats (my_chat_member updates)
    tg_app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER), group=1)
    # Handler for group -> supergroup migration (must be before the general message handler)
    tg_app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, handle_tg_migration), group=1)
    # Handler for channel posts (TG channel -> DC broadcast)
    tg_app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST & (
            filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO |
            filters.Document.ALL | filters.VOICE | filters.AUDIO |
            filters.Sticker.ALL | filters.ANIMATION | filters.VIDEO_NOTE |
            filters.LOCATION | filters.VENUE
        ),
        handle_tg_channel_post
    ), group=1)
    # Handler for edited channel posts
    tg_app.add_handler(MessageHandler(
        filters.UpdateType.EDITED_CHANNEL_POST & (filters.TEXT | filters.CAPTION | filters.LOCATION),
        handle_tg_edited_channel_post
    ), group=1)
    # Handler for group messages
    tg_app.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & (
            filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO |
            filters.Document.ALL | filters.VOICE | filters.AUDIO |
            filters.Sticker.ALL | filters.ANIMATION | filters.POLL |
            filters.VIDEO_NOTE | filters.LOCATION | filters.VENUE
        ),
        handle_tg_message
    ), group=1)
    # Handler for edited group messages
    tg_app.add_handler(MessageHandler(
        filters.UpdateType.EDITED_MESSAGE & (filters.TEXT | filters.CAPTION | filters.LOCATION),
        handle_tg_edited_message
    ), group=1)
    # Handler for poll state changes
    from telegram.ext import PollHandler
    tg_app.add_handler(PollHandler(handle_tg_poll))
    
    # Handler for message reactions
    tg_app.add_handler(MessageReactionHandler(handle_tg_reaction))

    await tg_app.initialize()
    await tg_app.start()
    
    # allowed_updates=Update.ALL_TYPES implicitly includes message_reaction
    await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    # Start DB cleanup loop
    asyncio.create_task(db_cleanup_loop())

    # Start Userbot if configured
    await start_userbot()

    # 2. Start DC Bot in a background thread
    loop = asyncio.get_running_loop()
    dc_task = loop.run_in_executor(None, start_dc_bot)

    # Main heart-beat and sync loop
    import time
    last_sync = time.time()
    last_userbot_check = time.time()
    
    logger.info("Bridge is now fully running. Waiting for events...")
    try:
        while True:
            await asyncio.sleep(10)
            now = time.time()
            
            # Watchdog for Userbot (check connectivity every 60 seconds)
            if (now - last_userbot_check) > 60:
                api_id = database.get_config("api_id")
                api_hash = database.get_config("api_hash")
                if api_id and api_hash:
                    # If client is None OR disconnected OR in a bad state (AttributeError scenario)
                    is_healthy = False
                    try:
                        # Improved health check: also ensure we can call an API method
                        if userbot_client and userbot_client.is_connected() and await userbot_client.is_user_authorized():
                            # Trigger a very cheap request to ensure connection is actually responding
                            await userbot_client.get_me() 
                            is_healthy = True
                    except (AttributeError, Exception) as e:
                        logger.warning(f"Userbot health check failed: {e}. Attempting restart...")
                    
                    if not is_healthy:
                        logger.info("Userbot client is offline or unhealthy. Restarting...")
                        # Run restart in background to not block the main heartbeat
                        asyncio.create_task(start_userbot())
                last_userbot_check = now

            if (now - last_sync) > 3600: # 1 hour interval
                if userbot_client and userbot_client.is_connected():
                    asyncio.create_task(sync_userbot_channels())
                last_sync = now
    except asyncio.CancelledError:
        pass

    # Cleanup when DC bot exits
    logger.info("Shutting down Telegram bot...")
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    if "init" in sys.argv:
        init_idx = sys.argv.index("init")
        if len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "tg":
            if len(sys.argv) > init_idx + 2:
                database.set_config("telegram_token", sys.argv[init_idx + 2])
            else:
                token = getpass.getpass("Enter your Telegram Bot Token (input will be hidden): ").strip()
                database.set_config("telegram_token", token)
            print("Telegram token saved in bridge.db.")
            sys.exit(0)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "dc":
            sys.argv.pop(init_idx + 1)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "admin_tg":
            if len(sys.argv) > init_idx + 2:
                database.set_config("admin_tg_id", sys.argv[init_idx + 2])
                print("Admin Telegram ID saved in bridge.db.")
            else:
                print("Usage: python bot.py init admin_tg <telegram_user_id>")
            sys.exit(0)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "admin_dc":
            if len(sys.argv) > init_idx + 2:
                database.set_config("admin_dc_email", sys.argv[init_idx + 2])
                print("Admin Delta Chat email saved in bridge.db.")
            else:
                print("Usage: python bot.py init admin_dc <deltachat_account_email>")
            sys.exit(0)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "api_id":
            if len(sys.argv) > init_idx + 2:
                database.set_config("api_id", sys.argv[init_idx + 2])
                print("API ID saved in bridge.db.")
            else:
                print("Usage: python bot.py init api_id <api_id>")
            sys.exit(0)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "api_hash":
            if len(sys.argv) > init_idx + 2:
                database.set_config("api_hash", sys.argv[init_idx + 2])
                print("API HASH saved in bridge.db.")
            else:
                print("Usage: python bot.py init api_hash <api_hash>")
            sys.exit(0)
        elif len(sys.argv) > init_idx + 1 and sys.argv[init_idx + 1] == "userbot":
            api_id = database.get_config("api_id")
            api_hash = database.get_config("api_hash")
            if not api_id or not api_hash:
                print("Error: Provide api_id and api_hash first before initializing userbot.")
                sys.exit(1)
            
            if not TelegramClient:
                print("Error: telethon is not installed.")
                sys.exit(1)
            
            print("Initializing userbot interactive login...")
            client = TelegramClient('userbot_session', int(api_id), api_hash)
            client.start()
            print("Userbot session successfully created in userbot_session.session")
            sys.exit(0)
        else:
            print("Usage:")
            print("  python bot.py init dc <email> [password]  - Initialize Delta Chat account")
            print("  python bot.py init tg [token]             - Initialize Telegram bot token")
            print("  python bot.py init admin_tg <tg_id>       - Set Admin Telegram ID for error logs")
            print("  python bot.py init admin_dc <email>       - Set Admin Delta Chat email for error logs")
            print("  python bot.py init api_id <id>            - Set MTProto API ID for userbot")
            print("  python bot.py init api_hash <hash>        - Set MTProto API HASH for userbot")
            print("  python bot.py init userbot                - Interactive sign-in for userbot channels")
            sys.exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
