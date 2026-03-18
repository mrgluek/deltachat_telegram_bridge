import asyncio
import html
import logging
import os
import tempfile
import time
import threading
from collections import defaultdict
from typing import Optional

from deltachat2 import EventType, MsgData, events
from deltabot_cli import BotCli

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import database
import io
import sys
import getpass
try:
    import qrcode
except ImportError:
    qrcode = None

# Initialize logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

class PollingErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc_str = str(record.exc_info[1])
            if "Server disconnected without sending a response" in exc_str:
                return False
        msg = record.getMessage()
        if "Server disconnected without sending a response" in msg:
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
            
            # Send to TG
            admin_tg_id = database.get_config("admin_tg_id")
            if admin_tg_id and tg_app and main_loop:
                try:
                    tg_id = int(admin_tg_id)
                    msg_text = f"⚠️ <b>Bot Error Log</b>\n\n<pre>{html.escape(_truncate(log_entry, 3800))}</pre>"
                    asyncio.run_coroutine_threadsafe(
                        tg_app.bot.send_message(chat_id=tg_id, text=msg_text, parse_mode='HTML'),
                        main_loop
                    )
                except Exception:
                    pass

            # Send to DC
            admin_dc_email = database.get_config("admin_dc_email")
            if admin_dc_email and dc_bot_instance and dc_accid:
                try:
                    dc_msg_text = f"⚠️ Bot Error Log\n\n{_truncate(log_entry, DC_MAX_MSG_LEN - 100)}"
                    contact_id = dc_bot_instance.rpc.create_contact(dc_accid, admin_dc_email, "Admin")
                    chat_id = dc_bot_instance.rpc.create_chat_by_contact_id(dc_accid, contact_id)
                    dc_bot_instance.rpc.send_msg(dc_accid, chat_id, MsgData(text=dc_msg_text))
                except Exception:
                    pass
        finally:
            self._is_emitting.flag = False

# Attach it to root logger
logging.getLogger().addHandler(AdminLogHandler())

# Limits
TG_MAX_MSG_LEN = 4000   # Telegram limit is 4096; leave margin
DC_MAX_MSG_LEN = 10000   # Practical DC limit
RATE_LIMIT_WINDOW = 60   # seconds
RATE_LIMIT_MAX = 30       # max messages per window per chat

# Initialize DeltaBot CLI
dc_cli = BotCli("tgbridge")

# Global references
tg_app: Optional[Application] = None
dc_bot_instance = None
dc_accid = None
main_loop = None
bot_contact_id = None  # To detect and skip own messages

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

def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, appending '…' if truncated."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


async def async_relay_to_tg(tg_chat_id, dc_chat_id, msg_id, file_path, formatted_msg, tg_reply_id, is_image, is_video, is_voice):
    try:
        tg_msg = None
        if file_path and os.path.exists(file_path):
            filename = os.path.basename(file_path)
            try:
                coro = None
                if is_image:
                    f = open(file_path, 'rb')
                    coro = tg_app.bot.send_photo(chat_id=tg_chat_id, photo=f, caption=formatted_msg, parse_mode='HTML', reply_to_message_id=tg_reply_id)
                elif is_video:
                    f = open(file_path, 'rb')
                    coro = tg_app.bot.send_video(chat_id=tg_chat_id, video=f, caption=formatted_msg, parse_mode='HTML', reply_to_message_id=tg_reply_id)
                elif is_voice:
                    f = open(file_path, 'rb')
                    coro = tg_app.bot.send_voice(chat_id=tg_chat_id, voice=f, caption=formatted_msg, parse_mode='HTML', reply_to_message_id=tg_reply_id)
                else:
                    f = open(file_path, 'rb')
                    coro = tg_app.bot.send_document(chat_id=tg_chat_id, document=f, caption=formatted_msg, parse_mode='HTML', reply_to_message_id=tg_reply_id)
                
                # using asyncio.wait_for for Python 3.9+ compatibility
                tg_msg = await asyncio.wait_for(coro, timeout=30.0)
            except (TimeoutError, asyncio.TimeoutError):
                fallback_text = formatted_msg + f"\n\n<i>[Failed to download media: {html.escape(filename)} - timeout exceeded]</i>"
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
    for accid in bot.rpc.get_all_account_ids():
        bot.rpc.set_config(accid, "displayname", "TG Bridge")
        bot.rpc.set_config(accid, "selfstatus", "I bridge Telegram and Delta Chat groups")
        # Auto-delete messages after 1 hour to save disk space
        bot.rpc.set_config(accid, "delete_device_after", "3600")
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
        f"I'm the TG Bridge bot. Mode: {mode}\n\n"
        f"I relay messages between Delta Chat and Telegram groups.\n\n"
        f"Commands:\n"
        f"/bridge <tg_group_id> — Link this DC group to a Telegram group\n"
        f"/unbridge — Remove the bridge from this group\n"
        f"/help — Show this help message\n\n"
        f"To get started, add me to a Delta Chat group and a Telegram group, then use /bridge to connect them."
    )

def get_tg_help_text(name: str, user_id: int) -> str:
    admin_tg = database.get_config("admin_tg_id")
    mode = "Private (bot owner only)" if admin_tg else "Public (group admins only)"
    return (
        f"👋 Hi {name} (<code>{user_id}</code>)!\n\n"
        f"I'm the DC Bridge bot. Mode: {mode}\n\n"
        f"I relay messages between Telegram and Delta Chat groups.\n\n"
        f"Commands:\n"
        f"/id — Show this group's chat ID (needed for bridging)\n"
        f"/help — Show this help message\n\n"
        f"To get started, add me to a Telegram group and use /id to get the group ID. Then use /bridge in the Delta Chat group to connect them.\n\n"
        f"ℹ️ Make sure Group Privacy is turned off in @BotFather → Bot Settings."
    )


@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    """Reply with help text."""
    msg = event.msg
    
    # Get sender info
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    
    help_msg = get_dc_help_text(sender_email)
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=help_msg))

@dc_cli.on(events.NewMessage(command="/bridge"))
def bridge_command(bot, accid, event):
    """Bridge a Delta Chat group to a Telegram group. Admin only."""
    msg = event.msg
    chat_id = msg.chat_id

    # Check if it's a group chat
    chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
    if chat_info.get("type") == 1:
        bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ Bridging is supported in group chats only."))
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


@dc_cli.on(events.NewMessage(is_info=False))
def handle_dc_message(bot, accid, event):
    """Relay Delta Chat messages to Telegram."""
    global tg_app

    if bot.has_command(event.command):
        return  # Ignore commands

    msg = event.msg
    dc_chat_id = msg.chat_id

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

    # Check if this chat is bridged
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
    file_path = getattr(msg, 'file', None) or None

    # Skip messages with neither text nor file
    if not text and not file_path:
        return

    # HTML-escape both name and text to prevent injection
    safe_name = html.escape(sender_name)
    safe_text = html.escape(text) if text else ""

    # Check if this is a reply to another message
    reply_quote = None
    if hasattr(msg, 'quote') and msg.quote:
        quote_text = msg.quote.get('text', '') if isinstance(msg.quote, dict) else ''
        if quote_text:
            short_quote = _truncate(quote_text, 50)
            reply_quote = f"<i>↩ {html.escape(short_quote)}</i>\n"

    # Build caption/text
    if safe_text:
        if reply_quote:
            formatted_msg = f"{reply_quote}<b>{safe_name}</b>: {safe_text}"
        else:
            formatted_msg = f"<b>{safe_name}</b>: {safe_text}"
    else:
        formatted_msg = f"<b>{safe_name}</b>"
    formatted_msg = _truncate(formatted_msg, TG_MAX_MSG_LEN)

    # Determine if this is a media message
    viewtype = getattr(msg, 'viewtype', None) or ''
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

            if main_loop:
                asyncio.run_coroutine_threadsafe(
                    async_relay_to_tg(tg_chat_id, dc_chat_id, msg.id, file_path, formatted_msg, tg_reply_id, is_image, is_video, is_voice),
                    main_loop
                )
            bot.logger.info(f"Relayed DC msg {msg.id} to TG chat {tg_chat_id}")
        except Exception as e:
            bot.logger.error(f"Failed to relay msg to TG chat {tg_chat_id}: {e}")

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

async def handle_tg_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay Telegram messages to Delta Chat."""
    global dc_bot_instance, dc_accid

    if not update.effective_chat or not update.message:
        return

    tg_chat_id = update.effective_chat.id

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
        tg_file = update.message.video
        file_name = update.message.video.file_name or "video.mp4"
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

    # Skip messages with no text and no media
    if not text and not tg_file:
        return

    # Check if this is a reply to another message
    reply_prefix = ""
    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        replied_text = replied.text or replied.caption or ""
        if replied_text:
            short_quote = _truncate(replied_text, 80)
            reply_prefix = f"↩ {short_quote}\n"

    formatted_msg = f"{reply_prefix}{sender_name}: {text}" if text else f"{sender_name}:"
    formatted_msg = _truncate(formatted_msg, DC_MAX_MSG_LEN)

    # Download the TG file to a temp path if present
    local_file_path = None
    if tg_file:
        try:
            tg_file_obj = await tg_file.get_file()
            suffix = os.path.splitext(file_name)[1] if file_name else ""
            tmp_fd, local_file_path = tempfile.mkstemp(suffix=suffix)
            os.close(tmp_fd)
            await asyncio.wait_for(tg_file_obj.download_to_drive(local_file_path), timeout=30.0)
        except (TimeoutError, asyncio.TimeoutError):
            logger.error(f"Timeout formatting file {file_name}")
            local_file_path = None
            formatted_msg = formatted_msg + f"\n\n<i>[Failed to download media: {html.escape(file_name)} - timeout exceeded]</i>"
        except Exception as e:
            logger.error(f"Failed to download TG file: {e}")
            local_file_path = None

    try:
        for dc_chat_id in dc_chats:
            try:
                msg_data = MsgData(text=formatted_msg)
                if local_file_path and os.path.exists(local_file_path):
                    msg_data.file = local_file_path
                sent_msg_id = dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, msg_data)
                if sent_msg_id:
                    database.save_message_map(sent_msg_id, dc_chat_id, update.message.message_id, tg_chat_id)
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
    
    text = f"🏁 <b>Poll closed:</b> {html.escape(poll.question)}\n\n"
    
    # Sort options by voter count descending
    sorted_options = sorted(poll.options, key=lambda x: x.voter_count, reverse=True)
    
    for option in sorted_options:
        percentage = (option.voter_count / total_voter_count * 100) if total_voter_count > 0 else 0
        text += f"▫️ {html.escape(option.text)} — {option.voter_count} votes ({percentage:.1f}%)\n"
        
    text += f"\n<i>Total votes: {total_voter_count}</i>"
    
    try:
        msg_data = MsgData(text=text)
        dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, msg_data)
        logger.info(f"Relayed TG poll results to DC chat {dc_chat_id}")
    except Exception as e:
        logger.error(f"Failed to relay poll results to DC chat {dc_chat_id}: {e}")

# ---------------------------------------------------------
# MAIN ASYNC RUNNER
# ---------------------------------------------------------

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
    tg_app.add_handler(CommandHandler("start", tg_start_command))
    tg_app.add_handler(CommandHandler("help", tg_help_command))
    tg_app.add_handler(CommandHandler("id", tg_id_command))
    tg_app.add_handler(MessageHandler(
        filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO |
        filters.Document.ALL | filters.VOICE | filters.AUDIO |
        filters.Sticker.ALL | filters.ANIMATION | filters.POLL,
        handle_tg_message
    ))
    # Handler for poll state changes
    from telegram.ext import PollHandler
    tg_app.add_handler(PollHandler(handle_tg_poll))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()

    # Start DB cleanup loop
    asyncio.create_task(db_cleanup_loop())

    # 2. Start DC Bot in a background thread
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, start_dc_bot)

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
        else:
            print("Usage:")
            print("  python bot.py init dc <email> [password]  - Initialize Delta Chat account")
            print("  python bot.py init tg [token]             - Initialize Telegram bot token")
            print("  python bot.py init admin_tg <tg_id>       - Set Admin Telegram ID for error logs")
            print("  python bot.py init admin_dc <email>       - Set Admin Delta Chat email for error logs")
            sys.exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
