import os
import re
import sqlite3
import threading
import time
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "bridge.db")
_lock = threading.Lock()

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

def init_db():
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bridges (
                    dc_chat_id INTEGER,
                    tg_chat_id INTEGER,
                    reactions_count INTEGER DEFAULT 0,
                    PRIMARY KEY (dc_chat_id, tg_chat_id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS message_map (
                    dc_msg_id INTEGER,
                    dc_chat_id INTEGER,
                    tg_msg_id INTEGER,
                    tg_chat_id INTEGER,
                    content_hash TEXT,
                    created_at INTEGER DEFAULT 0,
                    PRIMARY KEY (dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id)
                )
            ''')
            # Migration: ensure message_map composite primary key includes tg_msg_id
            try:
                pk_cols = [c[1] for c in cursor.execute("PRAGMA table_info(message_map)").fetchall() if c[5] > 0]
                if pk_cols and 'tg_msg_id' not in pk_cols:
                    cursor.execute("ALTER TABLE message_map RENAME TO message_map_old")
                    cursor.execute('''
                        CREATE TABLE message_map (
                            dc_msg_id INTEGER,
                            dc_chat_id INTEGER,
                            tg_msg_id INTEGER,
                            tg_chat_id INTEGER,
                            content_hash TEXT,
                            created_at INTEGER DEFAULT 0,
                            PRIMARY KEY (dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id)
                        )
                    ''')
                    cursor.execute('''
                        INSERT OR IGNORE INTO message_map (dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id, content_hash, created_at)
                        SELECT dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id, content_hash, created_at FROM message_map_old
                    ''')
                    cursor.execute("DROP TABLE message_map_old")
            except Exception:
                pass
            # Clean up old mappings (keep last 10000)
            cursor.execute('''
                DELETE FROM message_map WHERE rowid NOT IN (
                    SELECT rowid FROM message_map ORDER BY rowid DESC LIMIT 10000
                )
            ''')
            # Migration: add created_at to message_map if missing
            try:
                col_names = [c[1] for c in cursor.execute("PRAGMA table_info(message_map)").fetchall()]
                if 'created_at' not in col_names:
                    cursor.execute("ALTER TABLE message_map ADD COLUMN created_at INTEGER DEFAULT 0")
            except Exception:
                pass
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS polls (
                    poll_id TEXT PRIMARY KEY,
                    tg_chat_id INTEGER,
                    dc_chat_id INTEGER
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tg_msg ON message_map (tg_msg_id, tg_chat_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_dc_msg ON message_map (dc_msg_id, dc_chat_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_dc_chat ON message_map (dc_chat_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tg_chat ON message_map (tg_chat_id)')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_channel_username TEXT UNIQUE,
                    tg_channel_id INTEGER UNIQUE,
                    dc_chat_id INTEGER NOT NULL,
                    invite_link TEXT,
                    reactions_count INTEGER DEFAULT 0,
                    created_at INTEGER DEFAULT (strftime(\'%s\',\'now\')),
                    created_by_tg_id INTEGER,
                    last_msg_id INTEGER DEFAULT 0,
                    tg_participants_count INTEGER DEFAULT 0
                )
            ''')
            # Migrate old channels table: allow NULL username and add UNIQUE on tg_channel_id
            try:
                col_info = cursor.execute("PRAGMA table_info(channels)").fetchall()
                for col in col_info:
                    # col = (cid, name, type, notnull, default_value, pk)
                    if col[1] == 'tg_channel_username' and col[3] == 1:  # notnull == 1
                        cursor.execute("ALTER TABLE channels RENAME TO channels_old")
                        cursor.execute('''
                            CREATE TABLE channels (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                tg_channel_username TEXT UNIQUE,
                                tg_channel_id INTEGER UNIQUE,
                                dc_chat_id INTEGER NOT NULL,
                                invite_link TEXT,
                                created_at INTEGER DEFAULT (strftime(\'%s\',\'now\'))
                            )
                        ''')
                        cursor.execute('''
                            INSERT INTO channels (id, tg_channel_username, tg_channel_id, dc_chat_id, invite_link, created_at)
                            SELECT id, tg_channel_username, tg_channel_id, dc_chat_id, invite_link, created_at
                            FROM channels_old
                        ''')
                        cursor.execute("DROP TABLE channels_old")
                        break
            except Exception:
                pass
            # Admins table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS admins (
                    tg_user_id INTEGER PRIMARY KEY,
                    created_at INTEGER DEFAULT (strftime(\'%s\',\'now\'))
                )
            ''')
            # Migration: add created_by_tg_id to bridges
            try:
                col_names = [c[1] for c in cursor.execute("PRAGMA table_info(bridges)").fetchall()]
                if 'created_by_tg_id' not in col_names:
                    cursor.execute("ALTER TABLE bridges ADD COLUMN created_by_tg_id INTEGER")
            except Exception:
                pass
            # Migration: add created_by_tg_id to channels
            try:
                col_names = [c[1] for c in cursor.execute("PRAGMA table_info(channels)").fetchall()]
                if 'created_by_tg_id' not in col_names:
                    cursor.execute("ALTER TABLE channels ADD COLUMN created_by_tg_id INTEGER")
            except Exception:
                pass
            # Migration: add last_msg_id to channels
            try:
                col_names = [c[1] for c in cursor.execute("PRAGMA table_info(channels)").fetchall()]
                if 'last_msg_id' not in col_names:
                    cursor.execute("ALTER TABLE channels ADD COLUMN last_msg_id INTEGER DEFAULT 0")
            except Exception:
                pass
            # Migration: add tg_participants_count to channels
            try:
                col_names = [c[1] for c in cursor.execute("PRAGMA table_info(channels)").fetchall()]
                if 'tg_participants_count' not in col_names:
                    cursor.execute("ALTER TABLE channels ADD COLUMN tg_participants_count INTEGER DEFAULT 0")
            except Exception:
                pass
            # Migration: add reactions_count to bridges
            try:
                col_names = [c[1] for c in cursor.execute("PRAGMA table_info(bridges)").fetchall()]
                if 'reactions_count' not in col_names:
                    cursor.execute("ALTER TABLE bridges ADD COLUMN reactions_count INTEGER DEFAULT 0")
            except Exception:
                pass
            # Migration: add created_at to bridges
            try:
                col_names = [c[1] for c in cursor.execute("PRAGMA table_info(bridges)").fetchall()]
                if 'created_at' not in col_names:
                    cursor.execute("ALTER TABLE bridges ADD COLUMN created_at INTEGER DEFAULT (strftime(\'%s\',\'now\'))")
            except Exception:
                pass
            # Migration: add content_hash to message_map
            try:
                col_names = [c[1] for c in cursor.execute("PRAGMA table_info(message_map)").fetchall()]
                if 'content_hash' not in col_names:
                    cursor.execute("ALTER TABLE message_map ADD COLUMN content_hash TEXT")
            except Exception:
                pass

            # Transport statistics
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS transport_stats (
                    addr TEXT PRIMARY KEY,
                    msgs_sent INTEGER DEFAULT 0,
                    msgs_received INTEGER DEFAULT 0,
                    last_sent_at INTEGER,
                    last_received_at INTEGER
                )
            ''')

            # Message content filters
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS message_filters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern TEXT UNIQUE NOT NULL,
                    created_at INTEGER DEFAULT (strftime(\'%s\',\'now\'))
                )
            ''')

            # Processed media groups / albums tracking (for reliable deduplication across restarts and edit events)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS processed_media_groups (
                    group_id TEXT PRIMARY KEY,
                    tg_channel_id INTEGER,
                    dc_msg_id INTEGER,
                    created_at INTEGER DEFAULT (strftime(\'%s\',\'now\'))
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_pmg_created ON processed_media_groups (created_at)')

            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(DB_PATH, 0o600)
        except Exception:
            pass

def set_config(key: str, value: str):
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value) if value is not None else None))
            conn.commit()
        finally:
            conn.close()

def get_config(key: str) -> str | None:
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def get_rich_mode() -> str:
    """Get rich mode configuration ('webxdc', 'split', 'both', 'off'). Default is 'webxdc'."""
    val = get_config("rich_mode")
    if val in ("webxdc", "split", "both", "off"):
        return val
    return "webxdc"

def set_rich_mode(mode: str) -> bool:
    """Set rich mode configuration."""
    if mode in ("webxdc", "split", "both", "off"):
        set_config("rich_mode", mode)
        return True
    return False

def add_bridge(dc_chat_id: int, tg_chat_id: int, created_by_tg_id: int | None = None):
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO bridges (dc_chat_id, tg_chat_id, created_by_tg_id, reactions_count, created_at) VALUES (?, ?, ?, 0, strftime(\'%s\',\'now\'))", (dc_chat_id, tg_chat_id, created_by_tg_id))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

def remove_bridge(dc_chat_id: int) -> list[int]:
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT tg_chat_id FROM bridges WHERE dc_chat_id = ?", (dc_chat_id,))
            tg_chat_ids = [row[0] for row in cursor.fetchall()]
            cursor.execute("DELETE FROM bridges WHERE dc_chat_id = ?", (dc_chat_id,))
            cursor.execute("DELETE FROM message_map WHERE dc_chat_id = ?", (dc_chat_id,))
            conn.commit()
            return tg_chat_ids
        finally:
            conn.close()

def remove_bridge_by_tg(tg_chat_id: int):
    """Remove all bridges for a given TG chat ID."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bridges WHERE tg_chat_id = ?", (tg_chat_id,))
            cursor.execute("DELETE FROM message_map WHERE tg_chat_id = ?", (tg_chat_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted
        finally:
            conn.close()

def remove_bridge_pair(dc_chat_id: int, tg_chat_id: int) -> bool:
    """Remove a specific bridge pair and its message mappings."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM bridges WHERE dc_chat_id = ? AND tg_chat_id = ?", (dc_chat_id, tg_chat_id))
            cursor.execute("DELETE FROM message_map WHERE dc_chat_id = ? AND tg_chat_id = ?", (dc_chat_id, tg_chat_id))
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted
        finally:
            conn.close()

def get_tg_chats(dc_chat_id: int) -> list[int]:
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT tg_chat_id FROM bridges WHERE dc_chat_id = ?", (dc_chat_id,))
            rows = cursor.fetchall()
            return [row[0] for row in rows]
        finally:
            conn.close()

def count_bridges_for_tg(tg_chat_id: int) -> int:
    """Return the total number of bridges (group or channel) for a given TG ID."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM bridges WHERE tg_chat_id = ?", (tg_chat_id,))
            count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM channels WHERE tg_channel_id = ?", (tg_chat_id,))
            count += cursor.fetchone()[0]
            return count
        finally:
            conn.close()

def get_dc_chats(tg_chat_id: int) -> list[int]:
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT dc_chat_id FROM bridges WHERE tg_chat_id = ?", (tg_chat_id,))
            rows = cursor.fetchall()
            return [row[0] for row in rows]
        finally:
            conn.close()

def save_message_map(dc_msg_id: int, dc_chat_id: int, tg_msg_id: int, tg_chat_id: int, content_hash: str | None = None):
    """Save a mapping between a DC message and a TG message."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO message_map (dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id, content_hash, int(time.time()))
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

def get_dc_msgs_by_tg_msg_id(tg_msg_id: int, tg_chat_id: int) -> list[tuple[int, int]]:
    """Return all (dc_msg_id, dc_chat_id) pairs for a given TG message (across all DC chats)."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT dc_msg_id, dc_chat_id FROM message_map WHERE tg_msg_id = ? AND tg_chat_id = ?",
                (tg_msg_id, tg_chat_id)
            )
            rows = cursor.fetchall()
            return rows
        finally:
            conn.close()

def delete_message_map_entry_by_dc(dc_msg_id: int, dc_chat_id: int | None = None) -> None:
    """Remove a message map entry by DC message ID."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            if dc_chat_id is not None:
                cursor.execute("DELETE FROM message_map WHERE dc_msg_id = ? AND dc_chat_id = ?", (dc_msg_id, dc_chat_id))
            else:
                cursor.execute("DELETE FROM message_map WHERE dc_msg_id = ?", (dc_msg_id,))
            conn.commit()
        finally:
            conn.close()

def delete_message_map_entry_by_tg(tg_msg_id: int, tg_chat_id: int | None = None) -> None:
    """Remove message map entries by TG message ID."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            if tg_chat_id is not None:
                cursor.execute("DELETE FROM message_map WHERE tg_msg_id = ? AND tg_chat_id = ?", (tg_msg_id, tg_chat_id))
            else:
                cursor.execute("DELETE FROM message_map WHERE tg_msg_id = ?", (tg_msg_id,))
            conn.commit()
        finally:
            conn.close()

def get_tg_msg_id(dc_msg_id: int, dc_chat_id: int, tg_chat_id: int) -> int | None:
    """Look up the TG message ID for a given DC message."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tg_msg_id FROM message_map WHERE dc_msg_id = ? AND dc_chat_id = ? AND tg_chat_id = ?",
                (dc_msg_id, dc_chat_id, tg_chat_id)
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def get_tg_mappings_by_dc_msg_id(dc_msg_id: int) -> list[tuple]:
    """Look up all (tg_msg_id, tg_chat_id, dc_chat_id, created_at) mappings for a given DC message."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tg_msg_id, tg_chat_id, dc_chat_id, created_at FROM message_map WHERE dc_msg_id = ?",
                (dc_msg_id,)
            )
            rows = cursor.fetchall()
            return rows
        finally:
            conn.close()

def get_tg_mappings_with_hash_by_dc_msg_id(dc_msg_id: int) -> list[tuple]:
    """Look up all (tg_msg_id, tg_chat_id, dc_chat_id, created_at, content_hash) mappings for a given DC message."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tg_msg_id, tg_chat_id, dc_chat_id, created_at, content_hash FROM message_map WHERE dc_msg_id = ?",
                (dc_msg_id,)
            )
            rows = cursor.fetchall()
            return rows
        finally:
            conn.close()

def get_dc_msg_id(tg_msg_id: int, tg_chat_id: int, dc_chat_id: int) -> int | None:
    """Look up the DC message ID for a given TG message."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT dc_msg_id FROM message_map WHERE tg_msg_id = ? AND tg_chat_id = ? AND dc_chat_id = ?",
                (tg_msg_id, tg_chat_id, dc_chat_id)
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def get_message_content_hash(tg_msg_id: int, tg_chat_id: int, dc_chat_id: int) -> str | None:
    """Look up the stored content hash for a TG message."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT content_hash FROM message_map WHERE tg_msg_id = ? AND tg_chat_id = ? AND dc_chat_id = ?",
                (tg_msg_id, tg_chat_id, dc_chat_id)
            )
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def save_poll_context(poll_id: str, tg_chat_id: int, dc_chat_id: int):
    """Save context for a telegram poll so we know where to send updates."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO polls (poll_id, tg_chat_id, dc_chat_id) VALUES (?, ?, ?)",
                (poll_id, tg_chat_id, dc_chat_id)
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

def get_poll_context(poll_id: str) -> tuple[int, int] | None:
    """Retrieve chat IDs associated with a poll."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tg_chat_id, dc_chat_id FROM polls WHERE poll_id = ?",
                (poll_id,)
            )
            row = cursor.fetchone()
            return row if row else None
        finally:
            conn.close()

def update_bridge_tg_chat_id(old_tg_id: int, new_tg_id: int):
    """Update all references when a TG group migrates to a supergroup."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE bridges SET tg_chat_id = ? WHERE tg_chat_id = ?", (new_tg_id, old_tg_id))
            cursor.execute("UPDATE message_map SET tg_chat_id = ? WHERE tg_chat_id = ?", (new_tg_id, old_tg_id))
            cursor.execute("UPDATE polls SET tg_chat_id = ? WHERE tg_chat_id = ?", (new_tg_id, old_tg_id))
            conn.commit()
            print(f"Database updated for migration: {old_tg_id} -> {new_tg_id}")
        except Exception as e:
            conn.rollback()
            print(f"Error updating database for migration: {e}")
        finally:
            conn.close()

def get_all_bridges() -> list[tuple]:
    """Return all bridge info as (dc_chat_id, tg_chat_id, reactions_count)."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT dc_chat_id, tg_chat_id, reactions_count FROM bridges")
            rows = cursor.fetchall()
            return rows
        finally:
            conn.close()

def get_bridge_message_count(dc_chat_id: int, tg_chat_id: int) -> int:
    """Return the number of relayed messages for a specific bridge pair."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM message_map WHERE dc_chat_id = ? AND tg_chat_id = ?",
                (dc_chat_id, tg_chat_id)
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

def cleanup_old_messages(limit=10000):
    """Run this periodically to prevent DB bloat."""
    cleanup_old_records(limit=limit)

def increment_bridge_reaction_count(dc_chat_id: int, tg_chat_id: int):
    """Increment the reaction counter for a specific bridge."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE bridges SET reactions_count = reactions_count + 1 WHERE dc_chat_id = ? AND tg_chat_id = ?",
                (dc_chat_id, tg_chat_id)
            )
            conn.commit()
        finally:
            conn.close()

def increment_channel_reaction_count(tg_channel_id: int):
    """Increment the reaction counter for a specific channel."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE channels SET reactions_count = reactions_count + 1 WHERE tg_channel_id = ?",
                (tg_channel_id,)
            )
            conn.commit()
        finally:
            conn.close()

def get_bridge_reaction_count(dc_chat_id: int, tg_chat_id: int) -> int:
    """Return the reaction count for a specific bridge."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT reactions_count FROM bridges WHERE dc_chat_id = ? AND tg_chat_id = ?",
                (dc_chat_id, tg_chat_id)
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

def get_channel_reaction_count(tg_channel_id: int) -> int:
    """Return the reaction count for a specific channel."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT reactions_count FROM channels WHERE tg_channel_id = ?",
                (tg_channel_id,)
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

def add_channel(tg_username: str, dc_chat_id: int, invite_link: str | None = None, created_by_tg_id: int | None = None) -> int | None:
    """Add a TG channel -> DC broadcast mapping by username. Returns the row id."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO channels (tg_channel_username, dc_chat_id, invite_link, created_by_tg_id, reactions_count) VALUES (?, ?, ?, ?, 0)",
                (tg_username.lower(), dc_chat_id, invite_link, created_by_tg_id)
            )
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None
        finally:
            conn.close()

def add_channel_by_id(tg_channel_id: int, dc_chat_id: int, invite_link: str | None = None, username: str | None = None, created_by_tg_id: int | None = None) -> int | None:
    """Add a TG channel -> DC broadcast mapping by numeric ID (for private channels). Returns the row id."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO channels (tg_channel_username, tg_channel_id, dc_chat_id, invite_link, created_by_tg_id, reactions_count) VALUES (?, ?, ?, ?, ?, 0)",
                (username.lower() if username else None, tg_channel_id, dc_chat_id, invite_link, created_by_tg_id)
            )
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None
        finally:
            conn.close()

def get_channel_by_id(channel_id: int) -> dict | None:
    """Get a channel row by its autoincrement id."""
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

def get_channel_by_tg_username(username: str) -> dict | None:
    """Get a channel row by TG username."""
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM channels WHERE tg_channel_username = ?", (username.lower(),))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

def get_channel_by_tg_id(tg_channel_id: int) -> dict | None:
    """Get a channel row by TG numeric channel ID."""
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM channels WHERE tg_channel_id = ?", (tg_channel_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

def find_channel_by_any_id(raw_id: int | str) -> dict | None:
    """Find a channel by numeric ID with or without -100 prefix, internal row ID, or username."""
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 1. Try direct integer match against tg_channel_id or internal id
            try:
                val = int(raw_id)
                cursor.execute("SELECT * FROM channels WHERE tg_channel_id = ? OR id = ?", (val, val))
                row = cursor.fetchone()
                if row:
                    return dict(row)
                
                # 2. Try prefix variations (-100 prefix vs raw positive ID)
                clean_str = str(abs(val))
                if clean_str.startswith("100"):
                    clean_str = clean_str[3:]
                
                id_with_prefix = int(f"-100{clean_str}")
                id_without_prefix = int(clean_str)
                
                cursor.execute("SELECT * FROM channels WHERE tg_channel_id IN (?, ?)", (id_with_prefix, id_without_prefix))
                row = cursor.fetchone()
                if row:
                    return dict(row)
            except (ValueError, TypeError):
                pass

            # 3. Try username match
            if isinstance(raw_id, str):
                clean_name = raw_id.strip().lstrip('@').lower()
                cursor.execute("SELECT * FROM channels WHERE tg_channel_username = ?", (clean_name,))
                row = cursor.fetchone()
                if row:
                    return dict(row)

            return None
        finally:
            conn.close()

def update_channel_info(channel_id: int, participants_count: int = None, username: str = None, title: str = None):
    """Update metadata for a channel."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            if participants_count is not None:
                cursor.execute("UPDATE channels SET tg_participants_count = ? WHERE id = ?", (participants_count, channel_id))
            if username is not None:
                cursor.execute("UPDATE channels SET tg_channel_username = ? WHERE id = ?", (username.lower(), channel_id))
            conn.commit()
        finally:
            conn.close()

def get_all_channels() -> list[dict]:
    """Return all channel rows."""
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM channels ORDER BY id")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

def remove_channel(channel_id: int) -> int | None:
    """Remove a channel by its id and return its tg_channel_id."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT dc_chat_id, tg_channel_id FROM channels WHERE id = ?", (channel_id,))
            row = cursor.fetchone()
            tg_channel_id = None
            if row:
                dc_chat_id, tg_channel_id = row
                cursor.execute("DELETE FROM message_map WHERE dc_chat_id = ?", (dc_chat_id,))
            cursor.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
            conn.commit()
            return tg_channel_id
        finally:
            conn.close()

def update_channel_tg_id(username: str, tg_channel_id: int):
    """Set the numeric TG channel ID once we receive the first post."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE channels SET tg_channel_id = ? WHERE tg_channel_username = ?",
                (tg_channel_id, username.lower())
            )
            conn.commit()
        finally:
            conn.close()

def update_channel_invite_link(channel_id: int, invite_link: str):
    """Update the invite_link for a channel by its autoincrement id."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE channels SET invite_link = ? WHERE id = ?",
                (invite_link, channel_id)
            )
            conn.commit()
        finally:
            conn.close()

def get_dc_channel_chat_id(tg_channel_id: int) -> int | None:
    """Get the DC broadcast chat ID for a TG channel."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT dc_chat_id FROM channels WHERE tg_channel_id = ?", (tg_channel_id,))
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def get_channel_by_dc_chat_id(dc_chat_id: int) -> dict | None:
    """Get a channel row by its Delta Chat chat ID."""
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM channels WHERE dc_chat_id = ?", (dc_chat_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

def get_channel_last_msg_id(tg_channel_id: int) -> int:
    """Get the message ID of the last post forwarded to Delta Chat for a channel."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT last_msg_id FROM channels WHERE tg_channel_id = ?", (tg_channel_id,))
            row = cursor.fetchone()
            return row[0] if row and row[0] is not None else 0
        finally:
            conn.close()

def update_channel_last_msg_id(tg_channel_id: int, last_msg_id: int):
    """Update the message ID of the last post forwarded to Delta Chat for a channel (monotonically)."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE channels SET last_msg_id = MAX(COALESCE(last_msg_id, 0), ?) WHERE tg_channel_id = ?", (last_msg_id, tg_channel_id))
            conn.commit()
        finally:
            conn.close()

def is_media_group_processed(group_id: str | int) -> bool:
    """Check if a Telegram media group / album has already been processed."""
    if not group_id:
        return False
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM processed_media_groups WHERE group_id = ?", (str(group_id),))
            return cursor.fetchone() is not None
        finally:
            conn.close()

def mark_media_group_processed(group_id: str | int, tg_channel_id: int, dc_msg_id: Optional[int] = None):
    """Mark a Telegram media group / album as processed in persistent storage."""
    if not group_id:
        return
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO processed_media_groups (group_id, tg_channel_id, dc_msg_id) VALUES (?, ?, ?)",
                (str(group_id), tg_channel_id, dc_msg_id)
            )
            conn.commit()
        finally:
            conn.close()

def add_admin(tg_user_id: int) -> bool:
    """Add a sub-admin. Returns False if already exists."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO admins (tg_user_id) VALUES (?)", (tg_user_id,))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

def remove_admin(tg_user_id: int) -> bool:
    """Remove a sub-admin. Returns False if not found."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM admins WHERE tg_user_id = ?", (tg_user_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted
        finally:
            conn.close()

def get_all_admins() -> list[int]:
    """Return all sub-admin TG user IDs."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT tg_user_id FROM admins ORDER BY created_at")
            return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

def is_admin(tg_user_id: int) -> bool:
    """Check if a TG user is a sub-admin."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM admins WHERE tg_user_id = ?", (tg_user_id,))
            return cursor.fetchone() is not None
        finally:
            conn.close()

def is_owner_or_admin(tg_user_id: int) -> bool:
    """Check if a TG user is the owner or a sub-admin."""
    admin_tg_id = get_config("admin_tg_id")
    if admin_tg_id and str(tg_user_id) == str(admin_tg_id):
        return True
    return is_admin(tg_user_id)

def is_owner(tg_user_id: int) -> bool:
    admin_tg_id = get_config("admin_tg_id")
    return bool(admin_tg_id and str(tg_user_id) == str(admin_tg_id))

def get_bridge_creator(dc_chat_id: int) -> int | None:
    """Get the TG user ID of the bridge creator, or None."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT created_by_tg_id FROM bridges WHERE dc_chat_id = ?", (dc_chat_id,))
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def get_bridge_creator_by_tg(tg_chat_id: int) -> int | None:
    """Get the TG user ID of the bridge creator by TG chat ID, or None."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT created_by_tg_id FROM bridges WHERE tg_chat_id = ?", (tg_chat_id,))
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def get_bridges_by_creator(tg_user_id: int) -> list[tuple[int, int]]:
    """Return bridge pairs created by a specific TG user."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT dc_chat_id, tg_chat_id FROM bridges WHERE created_by_tg_id = ?", (tg_user_id,))
            rows = cursor.fetchall()
            return rows
        finally:
            conn.close()

def get_channel_creator(channel_id: int) -> int | None:
    """Get the TG user ID of the channel creator, or None."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT created_by_tg_id FROM channels WHERE id = ?", (channel_id,))
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def get_channels_by_creator(tg_user_id: int) -> list[dict]:
    """Return channel rows created by a specific TG user."""
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM channels WHERE created_by_tg_id = ? ORDER BY id", (tg_user_id,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

# Transport statistics tracking (buffered in memory)
_transport_stats_buffer: dict[str, dict[str, int]] = {}
_transport_stats_lock = threading.Lock()
_last_transport_flush = time.time()
TRANSPORT_FLUSH_INTERVAL = 30.0  # seconds

def increment_transport_sent(addr: str):
    """Increment the sent counter for a transport address (buffered in memory)."""
    if not addr or not isinstance(addr, str) or "@" not in addr:
        return
    now = int(time.time())
    should_flush = False
    with _transport_stats_lock:
        if addr not in _transport_stats_buffer:
            _transport_stats_buffer[addr] = {"sent": 0, "recv": 0, "last_sent": 0, "last_recv": 0}
        _transport_stats_buffer[addr]["sent"] += 1
        _transport_stats_buffer[addr]["last_sent"] = now
        global _last_transport_flush
        if now - _last_transport_flush >= TRANSPORT_FLUSH_INTERVAL:
            should_flush = True
    if should_flush:
        flush_transport_stats()

def increment_transport_received(addr: str):
    """Increment the received counter for a transport address (buffered in memory)."""
    if not addr or not isinstance(addr, str) or "@" not in addr:
        return
    now = int(time.time())
    should_flush = False
    with _transport_stats_lock:
        if addr not in _transport_stats_buffer:
            _transport_stats_buffer[addr] = {"sent": 0, "recv": 0, "last_sent": 0, "last_recv": 0}
        _transport_stats_buffer[addr]["recv"] += 1
        _transport_stats_buffer[addr]["last_recv"] = now
        global _last_transport_flush
        if now - _last_transport_flush >= TRANSPORT_FLUSH_INTERVAL:
            should_flush = True
    if should_flush:
        flush_transport_stats()

def flush_transport_stats():
    """Flush buffered transport stats to the database in a single transaction."""
    global _last_transport_flush
    with _transport_stats_lock:
        if not _transport_stats_buffer:
            _last_transport_flush = time.time()
            return
        pending = dict(_transport_stats_buffer)
        _transport_stats_buffer.clear()
        _last_transport_flush = time.time()

    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            for addr, counts in pending.items():
                if not isinstance(addr, str) or "@" not in addr:
                    continue
                sent = int(counts.get("sent", 0))
                recv = int(counts.get("recv", 0))
                last_s = counts.get("last_sent") or None
                last_r = counts.get("last_recv") or None
                cursor.execute('''
                    INSERT INTO transport_stats (addr, msgs_sent, msgs_received, last_sent_at, last_received_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(addr) DO UPDATE SET
                        msgs_sent = msgs_sent + excluded.msgs_sent,
                        msgs_received = msgs_received + excluded.msgs_received,
                        last_sent_at = COALESCE(excluded.last_sent_at, transport_stats.last_sent_at),
                        last_received_at = COALESCE(excluded.last_received_at, transport_stats.last_received_at)
                ''', (addr, sent, recv, last_s, last_r))
            conn.commit()
        finally:
            conn.close()

def get_all_transport_stats() -> list[dict]:
    """Get statistics for all tracked transports."""
    flush_transport_stats()
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM transport_stats ORDER BY msgs_sent + msgs_received DESC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

def get_recent_message_maps(limit: int = 5) -> list[dict]:
    """Get the most recent message map entries with channel username if available."""
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.*, c.tg_channel_username 
                FROM message_map m
                LEFT JOIN channels c ON m.tg_chat_id = c.tg_channel_id
                ORDER BY m.created_at DESC, m.rowid DESC 
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

# ---------------------------------------------------------
# MESSAGE FILTERS
# ---------------------------------------------------------

def add_filter(pattern: str) -> int | None:
    """Add a keyword or phrase filter (normalized lowercase). Returns row id or None if exists."""
    clean = pattern.strip().strip('"\'').lower()
    if not clean:
        return None
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO message_filters (pattern) VALUES (?)", (clean,))
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None
        finally:
            conn.close()

def remove_filter(target: str | int) -> tuple[bool, str | None]:
    """
    Remove a filter by ID number or by pattern string.
    Returns (success, deleted_pattern).
    """
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            deleted_pattern = None

            # 1. Try removing by ID if target is integer or numeric string
            try:
                val = int(target)
                cursor.execute("SELECT pattern FROM message_filters WHERE id = ?", (val,))
                row = cursor.fetchone()
                if row:
                    deleted_pattern = row['pattern']
                    cursor.execute("DELETE FROM message_filters WHERE id = ?", (val,))
                    conn.commit()
                    return True, deleted_pattern
            except (ValueError, TypeError):
                pass

            # 2. Try removing by pattern string
            clean = str(target).strip().strip('"\'').lower()
            if clean:
                cursor.execute("SELECT pattern FROM message_filters WHERE pattern = ?", (clean,))
                row = cursor.fetchone()
                if row:
                    deleted_pattern = row['pattern']
                    cursor.execute("DELETE FROM message_filters WHERE pattern = ?", (clean,))
                    conn.commit()
                    return True, deleted_pattern

            return False, None
        finally:
            conn.close()

def get_all_filters() -> list[dict]:
    """Get all configured filters sorted by ID."""
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM message_filters ORDER BY id ASC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

def get_all_filter_patterns() -> list[str]:
    """Get all active filter patterns as a list of strings."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pattern FROM message_filters ORDER BY id ASC")
            rows = cursor.fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

def cleanup_old_records(limit: int = 10000) -> dict[str, int]:
    """Clean up old message mappings and flush buffered transport stats."""
    flush_transport_stats()
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM message_map WHERE rowid NOT IN (
                    SELECT rowid FROM message_map ORDER BY rowid DESC LIMIT ?
                )
            ''', (limit,))
            pruned = cursor.rowcount

            cursor.execute('''
                DELETE FROM processed_media_groups WHERE rowid NOT IN (
                    SELECT rowid FROM processed_media_groups ORDER BY rowid DESC LIMIT ?
                )
            ''', (limit,))
            pruned_pmg = cursor.rowcount

            conn.commit()
            return {"message_map": pruned, "processed_media_groups": pruned_pmg}
        finally:
            conn.close()

def get_admin_email() -> str | None:
    db_val = get_config("admin_dc_email")
    if db_val and db_val.strip():
        return db_val.strip().lower()
    env_val = os.getenv("ADMIN_DC_EMAIL")
    return env_val.strip().lower() if env_val else None

def set_admin_email(email: str):
    if email:
        email = email.strip().lower()
    set_config("admin_dc_email", email)

def get_admin_fingerprint() -> str | None:
    fp = get_config("admin_dc_fingerprint")
    if not fp:
        fp = os.getenv("ADMIN_DC_FINGERPRINT", "")
    if fp:
        cleaned = fp.strip().replace(" ", "").replace(":", "").upper()
        if re.match(r"^[0-9A-F]{32,64}$", cleaned):
            return cleaned
    return None

def set_admin_fingerprint(fp: str):
    if fp:
        cleaned = fp.strip().replace(" ", "").replace(":", "").upper()
        set_config("admin_dc_fingerprint", cleaned)
    else:
        set_config("admin_dc_fingerprint", "")

def is_authorized_sender(sender_addr: str, fingerprint: str | None = None) -> bool:
    admin_email = get_admin_email()
    admin_fp = get_admin_fingerprint()

    if not admin_email and not admin_fp:
        return False

    sender_addr_clean = (sender_addr or "").strip().lower()

    if admin_email and sender_addr_clean == admin_email:
        if admin_fp:
            if fingerprint:
                fp_clean = fingerprint.strip().replace(" ", "").replace(":", "").upper()
                return admin_fp in fp_clean
            return False
        return True

    if admin_fp and fingerprint:
        fp_clean = fingerprint.strip().replace(" ", "").replace(":", "").upper()
        return admin_fp in fp_clean

    return False

init_db()
