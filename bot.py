import asyncio
import html
import logging
import os
import tempfile
import time
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
                fallback_text = formatted_msg + f"\n\n<i>[Не удалось загрузить медиа: {html.escape(filename)} - превышено время ожидания]</i>"
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

HELP_TEXT_DC = """👋 Hi! I'm the TG Bridge bot.

I relay messages between Delta Chat and Telegram groups.

Commands (group admins only):
/bridge <tg_group_id> — Link this DC group to a Telegram group
/unbridge — Remove the bridge from this group
/help — Show this help message

To get started, add me to a Delta Chat group and a Telegram group, then use /bridge to connect them."""

HELP_TEXT_TG = """👋 Hi! I'm the DC Bridge bot.

I relay messages between Telegram and Delta Chat groups.

Commands (group admins only):
/id — Show this group's chat ID (needed for bridging)
/help — Show this help message

To get started, add me to a Telegram group and use /id to get the group ID. Then use /bridge in the Delta Chat group to connect them.

ℹ️ Make sure Group Privacy is turned off in @BotFather → Bot Settings."""

@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    """Reply with help text."""
    msg = event.msg
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=HELP_TEXT_DC))

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

    # Admin check: only allow from contacts that are chat admins
    try:
        contacts = bot.rpc.get_chat_contacts(accid, chat_id)
        # In Delta Chat, the first contact in the list is the group creator/admin
        # Also check if the sender is the bot owner (contact ID 1 = self)
        if msg.from_id not in contacts[:1] and msg.from_id != 1:
            # Try a broader check - get contact info
            bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ Only group admins can use /bridge."))
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
    try:
        contacts = bot.rpc.get_chat_contacts(accid, chat_id)
        if msg.from_id not in contacts[:1] and msg.from_id != 1:
            bot.rpc.send_msg(accid, chat_id, MsgData(text="❌ Only group admins can use /unbridge."))
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
    """Reply to /start in private chat with help text."""
    await update.message.reply_text(HELP_TEXT_TG)

async def tg_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply to /help with help text."""
    await update.message.reply_text(HELP_TEXT_TG)

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

    await update.message.reply_text(f"Group ID: {chat.id}")

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
            formatted_msg = formatted_msg + f"\n\n<i>[Не удалось загрузить медиа: {html.escape(file_name)} - превышено время ожидания]</i>"
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
        else:
            print("Usage:")
            print("  python bot.py init dc <email> [password]  - Initialize Delta Chat account")
            print("  python bot.py init tg [token]             - Initialize Telegram bot token")
            sys.exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
