#!/usr/bin/env python3
"""Delta Chat ↔ Telegram Bridge — Main entry point."""

import asyncio
import sys
import getpass
import os

import database
from logging_setup import setup_logging
from app_context import app_ctx


def _init_tg(args):
    if len(args) >= 2:
        database.set_config("telegram_token", args[1])
    else:
        token = getpass.getpass("Enter your Telegram Bot Token (input will be hidden): ").strip()
        database.set_config("telegram_token", token)
    print("Telegram token saved in bridge.db.")
    sys.exit(0)

def _init_dc(args):
    if len(args) < 1 or not args[0]:
        email = input("Delta Chat email: ").strip()
        password = getpass.getpass("Password (input will be hidden): ").strip()
        args = [email, password] if password else [email]
    elif len(args) < 2:
        password = getpass.getpass("Password (input will be hidden): ").strip()
        args = [args[0], password] if password else args

    name = input("Bot display name (default: TG Bridge): ").strip()
    if name:
        database.set_config("bot_displayname", name)

    avatar = input("Avatar file path or name (default: icon_deltachat.jpg): ").strip()
    if avatar:
        database.set_config("bot_avatar_path", avatar)

    sys.argv = [sys.argv[0], "init"] + args
    try:
        app_ctx.dc_cli.start()
    except SystemExit:
        pass
    sys.exit(0)

def _init_admin_tg(args):
    if len(args) >= 2:
        database.set_config("admin_tg_id", args[1])
        print("Admin Telegram ID saved in bridge.db.")
    else:
        print("Usage: python bot.py init admin_tg <telegram_user_id>")
    sys.exit(0)

def _init_admin_dc(args):
    if len(args) >= 2:
        database.set_config("admin_dc_email", args[1])
        database.set_config("admin_dc_fingerprint", "")
        print("Admin Delta Chat email saved. (Fingerprint verification reset, use /initadmin in the bot to re-link securely).")
    else:
        print("Usage: python bot.py init admin_dc <deltachat_account_email>")
    sys.exit(0)

def _init_api_id(args):
    if len(args) >= 2:
        database.set_config("api_id", args[1])
        print("API ID saved in bridge.db.")
    else:
        print("Usage: python bot.py init api_id <api_id>")
    sys.exit(0)

def _init_api_hash(args):
    if len(args) >= 2:
        database.set_config("api_hash", args[1])
        print("API HASH saved in bridge.db.")
    else:
        print("Usage: python bot.py init api_hash <api_hash>")
    sys.exit(0)

def _init_userbot(args):
    api_id = database.get_config("api_id")
    api_hash = database.get_config("api_hash")
    if not api_id or not api_hash:
        print("Error: Provide api_id and api_hash first before initializing userbot.")
        sys.exit(1)

    try:
        from telethon import TelegramClient
    except ImportError:
        print("Error: telethon is not installed.")
        sys.exit(1)

    print("Initializing userbot interactive login...")
    client = TelegramClient('userbot_session', int(api_id), api_hash)
    client.start()
    print("Userbot session successfully created in userbot_session.session")
    sys.exit(0)

def _init_transport(args):
    if len(args) < 2:
        print("Usage:")
        print("  python bot.py init transport DCACCOUNT:uri")
        print("  python bot.py init transport addr password")
        sys.exit(1)

    from deltachat2 import Rpc, IOTransport
    from appdirs import user_config_dir

    config_dir = user_config_dir("deltachat_telegram_bridge")
    accounts_dir = os.path.join(config_dir, "accounts")

    try:
        with IOTransport(accounts_dir=accounts_dir) as trans:
            rpc = Rpc(trans)
            accids = rpc.get_all_account_ids()
            if not accids:
                print("Error: No accounts configured. Run 'python bot.py init dc addr password' first.")
                sys.exit(1)
            accid = accids[0]

            payload = args[1]
            if payload.startswith("DCACCOUNT:"):
                rpc.add_transport_from_qr(accid, payload)
                print(f"Success: Backup transport added via chatmail URI.")
            elif len(args) >= 3:
                addr, password = args[1], args[2]
                rpc.add_or_update_transport(accid, {"addr": addr, "password": password})
                print(f"Success: Backup transport {addr} added.")
            else:
                print("Error: For email accounts, provide both address and password.")
                sys.exit(1)
    except Exception as e:
        print(f"Error adding transport: {e}")
        sys.exit(1)
    sys.exit(0)

INIT_DISPATCH = {
    "tg": _init_tg,
    "dc": _init_dc,
    "admin_tg": _init_admin_tg,
    "admin_dc": _init_admin_dc,
    "api_id": _init_api_id,
    "api_hash": _init_api_hash,
    "userbot": _init_userbot,
    "transport": _init_transport,
}


def handle_init():
    init_idx = sys.argv.index("init")
    args = sys.argv[init_idx + 1:]
    cmd = args[0] if args else ""

    handler = INIT_DISPATCH.get(cmd)
    if handler:
        handler(args)
    else:
        print("Usage:")
        print("  python bot.py init dc <email> [password]  - Initialize Delta Chat account")
        print("  python bot.py init tg [token]             - Initialize Telegram bot token")
        print("  python bot.py init admin_tg <tg_id>       - Set Admin Telegram ID for error logs")
        print("  python bot.py init admin_dc <email>       - Set Admin Delta Chat email (resets fingerprint)")
        print("  python bot.py init transport <uri|addr>   - Add backup mail relay (transport)")
        print("  python bot.py init api_id <id>            - Set MTProto API ID for userbot")
        print("  python bot.py init api_hash <hash>        - Set MTProto API HASH for userbot")
        print("  python bot.py init userbot                - Interactive sign-in for userbot channels")
        sys.exit(1)


def main():
    setup_logging()

    if "init" in sys.argv:
        handle_init()

    try:
        from main import main as async_main
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nExiting...")


if __name__ == "__main__":
    main()
