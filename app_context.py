from typing import Optional, Any
from deltabot_cli import BotCli


class AppContext:
    def __init__(self):
        self.dc_cli: BotCli = BotCli("tgbridge")
        self.dc_bot: Any = None
        self.dc_accid: int = None
        self.bot_contact_id: int = None
        self.tg_app: Any = None
        self.userbot_client: Any = None
        self.main_loop: Any = None


app_ctx = AppContext()
