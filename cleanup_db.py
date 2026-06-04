import os
import sys
import glob
import sqlite3
import logging
from deltachat2 import Rpc, IOTransport

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("cleanup_db")

def get_db_email(db_path):
    """Get account email from SQLite config table, with schema fallbacks."""
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Determine column names by inspecting table info
        cursor.execute("PRAGMA table_info(config)")
        columns = [col[1] for col in cursor.fetchall()]
        
        key_col, val_col = None, None
        # Look for key-like column
        for k in ["key", "name", "c_key", "conf_key"]:
            if k in columns:
                key_col = k
                break
        # Look for value-like column
        for v in ["value", "val", "c_value", "c_val", "conf_val"]:
            if v in columns:
                val_col = v
                break
                
        if not key_col or not val_col:
            # Fallback guess if columns not found by keyword
            key_col = columns[0] if len(columns) > 0 else "key"
            val_col = columns[1] if len(columns) > 1 else "value"
            
        cursor.execute(f"SELECT {val_col} FROM config WHERE {key_col} = 'addr'")
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"Failed to read email from {db_path}: {e}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return None

def get_db_message_ids(db_path):
    """Get all message IDs from SQLite msgs table."""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM msgs")
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception as e:
        logger.warning(f"Failed to read message IDs from {db_path}: {e}")
        return []

def main():
    config_dir = "/app/data/tgbridge"
    accounts_dir = os.path.join(config_dir, "accounts")
    
    if not os.path.exists(accounts_dir):
        logger.error(f"Accounts directory not found at {accounts_dir}")
        sys.exit(1)

    # Find all database files in the accounts directory
    db_files = []
    for pattern in ["db.sqlite", "dc.db"]:
        db_files.extend(glob.glob(os.path.join(accounts_dir, "*", pattern)))
    
    if not db_files:
        logger.error("No Delta Chat databases found.")
        sys.exit(1)

    logger.info(f"Found {len(db_files)} database files.")
    db_map = {}
    for db_file in db_files:
        email = get_db_email(db_file)
        if email:
            db_map[email.lower()] = db_file
            logger.info(f"Database {db_file} belongs to {email}")

    logger.info("Connecting to Delta Chat RPC...")
    try:
        with IOTransport(accounts_dir=accounts_dir) as trans:
            rpc = Rpc(trans)
            accids = rpc.get_all_account_ids()
            if not accids:
                logger.info("No active accounts found in RPC.")
                return

            for i, accid in enumerate(accids):
                addr = rpc.get_config(accid, "addr")
                if not addr:
                    continue
                
                addr_lower = addr.lower()
                logger.info(f"Processing account {accid} ({addr})...")
                
                db_path = db_map.get(addr_lower)
                
                # Fallback 1: Single account and single database file
                if not db_path and len(db_files) == 1 and len(accids) == 1:
                    db_path = db_files[0]
                    logger.info(f"Mapping single database {db_path} to single account {accid}")
                
                # Fallback 2: Index-based mapping if email matching failed
                if not db_path and i < len(db_files):
                    db_path = db_files[i]
                    logger.info(f"Fallback mapping database {db_path} to account {accid} by index")
                    
                if not db_path:
                    logger.warning(f"No local database file found matching address {addr}")
                    continue
                
                msg_ids = get_db_message_ids(db_path)
                if not msg_ids:
                    logger.info(f"No messages found for account {accid}.")
                    continue
                
                logger.info(f"Deleting {len(msg_ids)} messages on account {accid}...")
                
                # Delete in chunks of 500 to avoid buffer limits
                chunk_size = 500
                total_deleted = 0
                for i in range(0, len(msg_ids), chunk_size):
                    chunk = msg_ids[i:i + chunk_size]
                    try:
                        rpc.delete_messages(accid, chunk)
                        total_deleted += len(chunk)
                    except Exception as del_e:
                        logger.error(f"Failed to delete chunk: {del_e}")
                
                logger.info(f"Successfully deleted {total_deleted} messages on account {accid}.")
                
                # Try running vacuum via RPC
                try:
                    logger.info("Running database vacuum via RPC...")
                    rpc.vacuum_database(accid)
                except Exception as vac_e:
                    logger.warning(f"Vacuum RPC call failed (might not be supported on this version): {vac_e}")
                    
    except Exception as e:
        logger.error(f"RPC cleanup failed: {e}")
        sys.exit(1)

    logger.info("Cleanup completed successfully.")

if __name__ == "__main__":
    main()
