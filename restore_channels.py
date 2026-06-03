#!/usr/bin/env python3
import os
import sys
import sqlite3
import urllib.request
import json
import ssl
from deltachat2 import Rpc, IOTransport
from appdirs import user_config_dir

# Bypass SSL verification if needed for simple HTTP requests
ssl_context = ssl._create_unverified_context()

def get_telegram_chat_title(token, chat_id_or_username):
    if not token:
        return None
    try:
        # If it's a username without @, prepend it
        chat_arg = chat_id_or_username
        if isinstance(chat_arg, str) and not chat_arg.startswith("-") and not chat_arg.startswith("@"):
            chat_arg = f"@{chat_arg}"
            
        url = f"https://api.telegram.org/bot{token}/getChat?chat_id={chat_arg}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, context=ssl_context, timeout=5) as response:
            data = json.loads(response.read().decode())
            if data.get("ok"):
                return data["result"].get("title")
    except Exception:
        pass
    return None

def main():
    DB_PATH = os.environ.get("DB_PATH", "bridge.db")
    if not os.path.exists(DB_PATH):
        print(f"❌ Error: Database {DB_PATH} not found!")
        sys.exit(1)

    config_dir = os.environ.get("DC_DB_DIR") or user_config_dir("tgbridge")
    accounts_dir = os.path.join(config_dir, "accounts")
    print(f"Using database: {DB_PATH}")
    print(f"Using Delta Chat accounts directory: {accounts_dir}")

    # Fetch TG Token from database
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = 'telegram_token'")
    row = cursor.fetchone()
    token = row[0] if row else None
    if not token:
        print("⚠️ Warning: telegram_token not found in database. Chat titles might fallback to defaults.")
    else:
        print("✅ Telegram token loaded successfully.")

    print("\nConnecting to Delta Chat core...")
    try:
        with IOTransport(accounts_dir=accounts_dir) as trans:
            rpc = Rpc(trans)
            accids = rpc.get_all_account_ids()
            if not accids:
                print("❌ Error: No accounts configured in Delta Chat! Please run 'init dc' first.")
                sys.exit(1)
            accid = accids[0]
            
            # 1. Restore channels (channels table)
            cursor.execute("SELECT id, tg_channel_username, tg_channel_id, dc_chat_id FROM channels")
            channels = cursor.fetchall()
            
            if not channels:
                print("No channels found in database to restore.")
            else:
                print(f"Found {len(channels)} channels to restore. Recreating broadcast chats in Delta Chat...")
                for row_id, username, tg_id, old_dc_chat_id in channels:
                    chat_ref = username if username else tg_id
                    real_title = get_telegram_chat_title(token, chat_ref)
                    title = real_title or username or f"Channel {tg_id}"
                    
                    print(f"Recreating channel #{row_id}: {title} (TG: {chat_ref})...")
                    
                    try:
                        dc_chat_id = rpc.create_broadcast(accid, title)
                        
                        invite_link = rpc.get_chat_securejoin_qr_code(accid, dc_chat_id)
                        if invite_link.startswith("OPEN-CHAT:"):
                            invite_link = "https://i.delta.chat/#" + invite_link[10:]
                        elif invite_link.startswith("OPEN:"):
                            invite_link = "https://i.delta.chat/#" + invite_link[5:]
                        
                        cursor.execute(
                            "UPDATE channels SET dc_chat_id = ?, invite_link = ? WHERE id = ?",
                            (dc_chat_id, invite_link, row_id)
                        )
                        print(f"  Success! New DC Chat ID: {dc_chat_id}, Invite Link: {invite_link}")
                    except Exception as ch_err:
                        print(f"  Error recreating channel {title}: {ch_err}")
                
            # 2. Restore group bridges (bridges table)
            cursor.execute("SELECT dc_chat_id, tg_chat_id, reactions_count, created_by_tg_id FROM bridges")
            bridges = cursor.fetchall()
            if bridges:
                print(f"\nFound {len(bridges)} group bridges. Recreating groups...")
                for old_dc_chat_id, tg_chat_id, reactions_count, created_by_tg_id in bridges:
                    real_title = get_telegram_chat_title(token, tg_chat_id)
                    title = real_title or f"Bridge {tg_chat_id}"
                    
                    print(f"Recreating group bridge for TG Chat: {tg_chat_id} ({title})...")
                    try:
                        dc_chat_id = rpc.create_group_chat(accid, title, True)
                        
                        cursor.execute(
                            "DELETE FROM bridges WHERE dc_chat_id = ? AND tg_chat_id = ?",
                            (old_dc_chat_id, tg_chat_id)
                        )
                        cursor.execute(
                            "INSERT INTO bridges (dc_chat_id, tg_chat_id, reactions_count, created_by_tg_id) VALUES (?, ?, ?, ?)",
                            (dc_chat_id, tg_chat_id, reactions_count, created_by_tg_id)
                        )
                        print(f"  Success! New DC Chat ID: {dc_chat_id}")
                    except Exception as br_err:
                        print(f"  Error recreating bridge: {br_err}")
                        
            conn.commit()
            print("\n🎉 All done! Database mappings successfully updated and groups recreated.")
            
    except Exception as e:
        print(f"❌ Error during execution: {e}")
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
