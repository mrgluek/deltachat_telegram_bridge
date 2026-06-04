import os
import sys
import logging
from deltachat2 import Rpc, IOTransport

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("cleanup_db")

def main():
    config_dir = "/app/data/tgbridge"
    accounts_dir = os.path.join(config_dir, "accounts")
    
    if not os.path.exists(accounts_dir):
        logger.error(f"Accounts directory not found at {accounts_dir}")
        sys.exit(1)

    logger.info("Connecting to Delta Chat RPC...")
    try:
        with IOTransport(accounts_dir=accounts_dir) as trans:
            rpc = Rpc(trans)
            accids = rpc.get_all_account_ids()
            if not accids:
                logger.info("No accounts found.")
                return

            for accid in accids:
                addr = rpc.get_config(accid, "addr")
                logger.info(f"Processing account {accid} ({addr})...")
                
                chat_ids = rpc.get_chat_ids(accid)
                logger.info(f"Found {len(chat_ids)} chats.")
                
                total_deleted = 0
                for chat_id in chat_ids:
                    # Retrieve all message IDs for this chat
                    try:
                        msg_ids = rpc.get_chat_message_ids(accid, chat_id)
                        if msg_ids:
                            logger.info(f"  Chat {chat_id}: Deleting {len(msg_ids)} messages...")
                            rpc.delete_messages(accid, msg_ids)
                            total_deleted += len(msg_ids)
                    except Exception as chat_e:
                        logger.error(f"  Failed to process chat {chat_id}: {chat_e}")

                logger.info(f"Deleted {total_deleted} messages on account {accid}.")
                
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
