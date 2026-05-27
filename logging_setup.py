import logging
import html
import threading
import asyncio

from constants import TRANSIENT_POLLING_ERRORS, DC_MAX_MSG_LEN
from app_context import app_ctx


class PollingErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc_str = str(record.exc_info[1])
            if any(pat in exc_str for pat in TRANSIENT_POLLING_ERRORS):
                return False
        msg = record.getMessage()
        if any(pat in msg for pat in TRANSIENT_POLLING_ERRORS):
            return False
        return True


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


class AdminLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self._is_emitting = threading.local()

    def emit(self, record):
        if record.levelno < logging.ERROR:
            return

        try:
            exc_str = str(record.exc_info[1]) if record.exc_info else ""
            msg_str = record.getMessage()
            if any(pat in exc_str or pat in msg_str for pat in TRANSIENT_POLLING_ERRORS):
                return
        except Exception:
            pass

        if getattr(self._is_emitting, 'flag', False):
            return

        self._is_emitting.flag = True
        try:
            log_entry = self.format(record)
            import database

            if app_ctx.tg_app and app_ctx.main_loop:
                admin_tg_id = database.get_config("admin_tg_id")
                if admin_tg_id:
                    try:
                        tg_id = int(admin_tg_id)
                        msg_text = f"⚠️ <b>Bot Error Log</b>\n\n<pre>{html.escape(_truncate(log_entry, 3800))}</pre>"
                        asyncio.run_coroutine_threadsafe(
                            app_ctx.tg_app.bot.send_message(chat_id=tg_id, text=msg_text, parse_mode='HTML'),
                            app_ctx.main_loop
                        )
                    except Exception:
                        pass

            if app_ctx.dc_bot and app_ctx.dc_accid:
                admin_dc_email = database.get_config("admin_dc_email")
                if admin_dc_email:
                    try:
                        from deltachat2 import MsgData
                        dc_msg_text = f"⚠️ Bot Error Log\n\n{_truncate(log_entry, DC_MAX_MSG_LEN - 100)}"
                        contact_id = app_ctx.dc_bot.rpc.create_contact(app_ctx.dc_accid, admin_dc_email, "Admin")
                        chat_id = app_ctx.dc_bot.rpc.create_chat_by_contact_id(app_ctx.dc_accid, contact_id)
                        app_ctx.dc_bot.rpc.send_msg(app_ctx.dc_accid, chat_id, MsgData(text=dc_msg_text))
                    except Exception:
                        pass
        finally:
            self._is_emitting.flag = False


def setup_logging():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logging.getLogger("httpx").setLevel(logging.WARNING)

    admin_handler = AdminLogHandler()
    logging.getLogger().addHandler(admin_handler)
    logging.getLogger("deltachat2").addHandler(admin_handler)
    logging.getLogger("deltachat2").propagate = True
    logging.getLogger("telegram").addHandler(admin_handler)

    logging.getLogger("telegram.ext.Updater").addFilter(PollingErrorFilter())
    logging.getLogger("telegram.ext.Application").addFilter(PollingErrorFilter())
    logging.getLogger("httpx").addFilter(PollingErrorFilter())
    logging.getLogger("httpcore").addFilter(PollingErrorFilter())

    return admin_handler
