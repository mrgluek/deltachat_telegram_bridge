import asyncio
import os
import sys
import logging
from telethon import TelegramClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("debug_resolve")

# Determine DB and Session paths
DB_PATH = os.environ.get("DB_PATH", "bridge.db")
USERBOT_SESSION_PATH = os.environ.get("USERBOT_SESSION_PATH", "userbot_session")

if not os.path.exists(DB_PATH):
    # Try data/ folder
    if os.path.exists("data/bridge.db"):
        DB_PATH = "data/bridge.db"
        USERBOT_SESSION_PATH = "data/userbot_session"
    else:
        logger.error(f"bridge.db not found (tried {DB_PATH} and data/bridge.db)")
        sys.exit(1)

logger.info(f"Using DB_PATH: {DB_PATH}")
logger.info(f"Using USERBOT_SESSION_PATH: {USERBOT_SESSION_PATH}")

import sqlite3

def get_config(key: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

async def main():
    api_id = get_config("api_id")
    api_hash = get_config("api_hash")
    if not (api_id and api_hash):
        logger.error("api_id or api_hash not found in database config!")
        sys.exit(1)

    logger.info(f"api_id: {api_id}")
    client = TelegramClient(USERBOT_SESSION_PATH, int(api_id), api_hash)
    
    logger.info("Connecting to Telegram...")
    await client.connect()
    
    if not await client.is_user_authorized():
        logger.error("User is NOT authorized! The session might be invalid or expired.")
        sys.exit(1)
        
    me = await client.get_me()
    logger.info(f"Logged in as: {me.id} | @{getattr(me, 'username', 'None')} | Name: {getattr(me, 'first_name', '')} {getattr(me, 'last_name', '')}")
    
    # Try to resolve a public channel
    test_username = "@gluekinfo"
    logger.info(f"Attempting to resolve {test_username}...")
    try:
        entity = await client.get_entity(test_username)
        logger.info(f"SUCCESS! Resolved {test_username} to {entity.title} (ID: {entity.id})")
    except Exception as e:
        logger.exception(f"FAILED to resolve {test_username}!")
        
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
