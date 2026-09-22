"""Logging infrastructure: suppression filter for transient network/polling
noise, the admin-notification pipeline (background worker thread + bounded
queue delivering error logs to the bot owner over DC and Telegram), and
Telethon ban-detection wired into channel-access-revoked handling.

References to the pervasive bot.py singletons (dc_bot_instance, dc_accid,
tg_app, main_loop) and to DC_MAX_MSG_LEN (defined later in bot.py than
this module's own re-export) go through a function-local
`import bot as _bot_module`, same pattern as the rest of this refactor.
Note this module has two logging.Handler subclasses whose emit() methods
each need their own local import — a shared import at the top of a
sibling function in the same file does not carry into a class method's
scope.
"""
import asyncio
import html
import logging
import queue
import re
import threading
import time
from typing import Optional

import database
from deltachat2 import MsgData

from formatting import _truncate

logger = logging.getLogger("tg_dc_bridge")


_TRANSIENT_POLLING_ERRORS = (
    "Server disconnected without sending a response",
    "ReadTimeout",
    "All connection attempts failed",
    "ConnectError",
    "httpcore.ConnectError",
    "httpx.ConnectError",
    "ConnectTimeout",
    "httpcore.ConnectTimeout",
    "httpx.ConnectTimeout",
    "TimedOut",
    "Unexpected exception reconnecting",
    "Automatic reconnection failed",
    "AttributeError: 'NoneType' object has no attribute 'connect'",
    "ReadError",
    "httpcore.ReadError",
    "httpx.ReadError",
    "RemoteProtocolError",
    "ProxyError",
    "NetworkError",
    "Exception happened while polling for updates",
    "Task was destroyed but it is pending",
    "Error while calling `get_updates`",
    "Error while calling get_updates",
    "Cannot send requests while disconnected",
    "'NoneType' object has no attribute",
    "Reconciliation failed",
)


class PollingErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc_str = str(record.exc_info[1])
            if any(pat in exc_str for pat in _TRANSIENT_POLLING_ERRORS):
                return False
        msg = record.getMessage()
        if any(pat in msg for pat in _TRANSIENT_POLLING_ERRORS):
            return False
        return True


_admin_dc_chat_id_cache = None


_admin_dc_chat_id_lock = threading.Lock()


_admin_dc_queue = queue.Queue(maxsize=100)


_admin_dc_worker_started = False


_admin_dc_worker_lock = threading.Lock()


_admin_cached_at = 0.0


_cached_admin_tg_id = None


_cached_admin_dc_email = None


def _get_cached_admin_targets() -> tuple[Optional[str], Optional[str]]:
    global _admin_cached_at, _cached_admin_tg_id, _cached_admin_dc_email
    now = time.time()
    if now - _admin_cached_at > 60.0:
        try:
            _cached_admin_tg_id = database.get_config("admin_tg_id")
            _cached_admin_dc_email = database.get_config("admin_dc_email")
            _admin_cached_at = now
        except Exception:
            pass
    return _cached_admin_tg_id, _cached_admin_dc_email


def _admin_dc_worker_loop():
    while True:
        try:
            text = _admin_dc_queue.get()
            _send_admin_dc_message_bg(text)
        except Exception:
            pass
        finally:
            _admin_dc_queue.task_done()


def _enqueue_admin_dc_message(text: str):
    global _admin_dc_worker_started
    with _admin_dc_worker_lock:
        if not _admin_dc_worker_started:
            t = threading.Thread(target=_admin_dc_worker_loop, daemon=True, name="AdminDCLogWorker")
            t.start()
            _admin_dc_worker_started = True
    try:
        _admin_dc_queue.put_nowait(text)
    except queue.Full:
        pass  # Drop if queue is full during error storms


def _send_admin_dc_message_bg(text: str):
    import bot as _bot_module
    _, admin_dc_email = _get_cached_admin_targets()
    if not (admin_dc_email and _bot_module.dc_bot_instance and _bot_module.dc_accid):
        return

    with _admin_dc_chat_id_lock:
        try:
            if _admin_dc_chat_id_cache is None:
                contact_id = _bot_module.dc_bot_instance.rpc.create_contact(_bot_module.dc_accid, admin_dc_email, "Admin")
                _admin_dc_chat_id_cache = _bot_module.dc_bot_instance.rpc.create_chat_by_contact_id(_bot_module.dc_accid, contact_id)
            
            _bot_module.dc_bot_instance.rpc.send_msg(_bot_module.dc_accid, _admin_dc_chat_id_cache, MsgData(text=text))
        except Exception:
            _admin_dc_chat_id_cache = None
            try:
                contact_id = _bot_module.dc_bot_instance.rpc.create_contact(_bot_module.dc_accid, admin_dc_email, "Admin")
                chat_id = _bot_module.dc_bot_instance.rpc.create_chat_by_contact_id(_bot_module.dc_accid, contact_id)
                _admin_dc_chat_id_cache = chat_id
                _bot_module.dc_bot_instance.rpc.send_msg(_bot_module.dc_accid, chat_id, MsgData(text=text))
            except Exception:
                pass


ADMIN_ALERT_THROTTLE_SECONDS = 3600
_admin_alert_last_sent: dict[str, float] = {}
_admin_alert_suppressed: dict[str, int] = {}
_admin_alert_lock = threading.Lock()


def _admin_alert_key(record) -> str:
    """Group similar errors: numbers (post/chat/msg IDs, sizes) are normalized away."""
    exc_type = type(record.exc_info[1]).__name__ if record.exc_info and record.exc_info[1] else ""
    return f"{record.name}|{exc_type}|{re.sub(r'\d+', 'N', record.getMessage())}"


def _admin_alert_check(key: str, now: Optional[float] = None) -> tuple[bool, int]:
    """Return (should_send, similar_suppressed_since_last_send) for an alert key."""
    now = time.time() if now is None else now
    with _admin_alert_lock:
        last = _admin_alert_last_sent.get(key)
        if last is not None and now - last < ADMIN_ALERT_THROTTLE_SECONDS:
            _admin_alert_suppressed[key] = _admin_alert_suppressed.get(key, 0) + 1
            return False, 0
        _admin_alert_last_sent[key] = now
        return True, _admin_alert_suppressed.pop(key, 0)


class AdminLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self._is_emitting = threading.local()

    def emit(self, record):
        if record.levelno < logging.ERROR:
            return

        # Suppress transient polling/network errors — they self-resolve and should not spam admin.
        try:
            exc_str = str(record.exc_info[1]) if record.exc_info else ""
            msg_str = record.getMessage()
            if any(pat in exc_str or pat in msg_str for pat in _TRANSIENT_POLLING_ERRORS):
                return
            
            # Prevent E2E/failover/sending error logging loops by ignoring failover-related errors.
            msg_lower = msg_str.lower()
            exc_lower = exc_str.lower()
            loop_patterns = ["encryption", "failover", "resend", "msg_failed", "failed to send dc message", "error executing scheduled resend"]
            if any(pat in msg_lower or pat in exc_lower for pat in loop_patterns):
                return
        except Exception:
            pass
            
        if getattr(self._is_emitting, 'flag', False):
            return

        should_send, suppressed = _admin_alert_check(_admin_alert_key(record))
        if not should_send:
            return

        self._is_emitting.flag = True
        try:
            import bot as _bot_module
            log_entry = self.format(record)
            if suppressed:
                log_entry += f"\n\n(+{suppressed} similar error(s) suppressed in the last hour)"
            
            # Use local refs for globals to be safe
            local_tg_app = _bot_module.tg_app
            local_main_loop = _bot_module.main_loop
            local_dc_bot = _bot_module.dc_bot_instance
            local_dc_accid = _bot_module.dc_accid
            
            admin_tg_id, admin_dc_email = _get_cached_admin_targets()

            # Send to TG
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

            # Send to DC (offloaded to persistent bounded queue worker to prevent thread exhaustion)
            if admin_dc_email and local_dc_bot and local_dc_accid:
                dc_msg_text = f"⚠️ Bot Error Log\n\n{_truncate(log_entry, _bot_module.DC_MAX_MSG_LEN - 100)}"
                _enqueue_admin_dc_message(dc_msg_text)
        finally:
            self._is_emitting.flag = False


_reported_inaccessible_channels = set()


_reported_inaccessible_lock = threading.Lock()


async def _handle_channel_access_revoked(target_id: int | str, reason: str = "Account was banned or channel is private"):
    """Notify DC broadcast channel and admin when userbot loses access to a channel."""
    import bot as _bot_module
    
    chan = database.find_channel_by_any_id(target_id)
    if not chan:
        return

    chan_db_id = chan.get('id')
    with _reported_inaccessible_lock:
        if chan_db_id in _reported_inaccessible_channels:
            return  # Debounced, already notified
        _reported_inaccessible_channels.add(chan_db_id)

    tg_id = chan.get('tg_channel_id')
    username = chan.get('tg_channel_username')
    dc_chat_id = chan.get('dc_chat_id')
    display_name = f"@{username}" if username else f"ID {tg_id}"

    logger.warning(f"Channel access revoked for {display_name} (Channel #{chan_db_id}): {reason}")

    # 1. Notify Delta Chat Broadcast Channel
    if dc_chat_id and _bot_module.dc_bot_instance and _bot_module.dc_accid:
        try:
            dc_notice = (
                "⚠️ **Bridge Alert**\n"
                "The technical account (Userbot) was banned or removed from this Telegram channel, or the channel was made private.\n\n"
                "Message forwarding from Telegram has been paused."
            )
            _bot_module.dc_bot_instance.rpc.send_msg(_bot_module.dc_accid, dc_chat_id, MsgData(text=dc_notice))
        except Exception as dc_err:
            logger.debug(f"Could not send access revoked alert to DC channel {dc_chat_id}: {dc_err}")

    # 2. Notify TG Admin / Owner
    admin_tg_id = database.get_config("admin_tg_id")
    if admin_tg_id and _bot_module.tg_app:
        try:
            tg_text = (
                f"⚠️ <b>Channel Access Lost / Banned</b>\n\n"
                f"• <b>Channel:</b> {html.escape(display_name)} (ID: <code>{tg_id}</code>)\n"
                f"• <b>Channel Bridge:</b> #{chan_db_id} (DC Chat: <code>{dc_chat_id}</code>)\n"
                f"• <b>Reason:</b> <code>{html.escape(reason)}</code>\n\n"
                f"To remove this channel bridge:\n"
                f"<code>/channelremove {chan_db_id}</code>"
            )
            await _bot_module.tg_app.bot.send_message(chat_id=int(admin_tg_id), text=tg_text, parse_mode='HTML')
        except Exception as tg_err:
            logger.error(f"Failed to notify TG admin about lost channel access: {tg_err}")

    # 3. Notify DC Admin
    admin_dc_email = database.get_config("admin_dc_email")
    if admin_dc_email and _bot_module.dc_bot_instance and _bot_module.dc_accid:
        dc_admin_text = (
            f"⚠️ Channel Access Lost / Banned\n\n"
            f"• Channel: {display_name} (ID: {tg_id})\n"
            f"• Channel Bridge: #{chan_db_id} (DC Chat: {dc_chat_id})\n"
            f"• Reason: {reason}\n\n"
            f"To remove: /channelremove {chan_db_id}"
        )
        threading.Thread(target=_send_admin_dc_message_bg, args=(dc_admin_text,), daemon=True).start()


class TelethonBanLogHandler(logging.Handler):
    """Intercepts Telethon logs indicating the userbot was banned/restricted in a channel."""
    def emit(self, record):
        import bot as _bot_module
        try:
            msg = record.getMessage()
            if "Account is now banned in" in msg or "so we can no longer fetch updates from it" in msg:
                import re
                m = re.search(r'banned in\s+(\d+)', msg)
                if m:
                    chan_id_raw = int(m.group(1))
                    if _bot_module.main_loop and _bot_module.main_loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            _handle_channel_access_revoked(chan_id_raw, reason="Account was banned in channel (Telethon update)"),
                            _bot_module.main_loop
                        )
        except Exception:
            pass


logging.getLogger("telegram.ext.Updater").addFilter(PollingErrorFilter())
logging.getLogger("telegram.ext.Application").addFilter(PollingErrorFilter())
logging.getLogger("httpx").addFilter(PollingErrorFilter())
logging.getLogger("httpcore").addFilter(PollingErrorFilter())
logging.getLogger("telethon.network.mtprotosender").addFilter(PollingErrorFilter())

# Attach it to root and major component loggers
admin_handler = AdminLogHandler()
logging.getLogger().addHandler(admin_handler)
logging.getLogger("deltachat2").addHandler(admin_handler)
logging.getLogger("deltachat2").propagate = True
logging.getLogger("telegram").addHandler(admin_handler)

logging.getLogger("telethon.client.updates").addHandler(TelethonBanLogHandler())
