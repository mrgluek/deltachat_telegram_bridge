import os
import sqlite3
import threading

DB_PATH = "bridge.db"
_lock = threading.Lock()

def init_db():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bridges (
                dc_chat_id INTEGER,
                tg_chat_id INTEGER,
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
                PRIMARY KEY (dc_msg_id, dc_chat_id, tg_chat_id)
            )
        ''')
        # Clean up old mappings (keep last 10000)
        cursor.execute('''
            DELETE FROM message_map WHERE rowid NOT IN (
                SELECT rowid FROM message_map ORDER BY rowid DESC LIMIT 10000
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS polls (
                poll_id TEXT PRIMARY KEY,
                tg_chat_id INTEGER,
                dc_chat_id INTEGER
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tg_msg ON message_map (tg_msg_id, tg_chat_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_dc_msg ON message_map (dc_msg_id, dc_chat_id)')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_channel_username TEXT NOT NULL UNIQUE,
                tg_channel_id INTEGER,
                dc_chat_id INTEGER NOT NULL,
                invite_link TEXT,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        ''')
        conn.commit()
        conn.close()
        try:
            os.chmod(DB_PATH, 0o600)
        except Exception:
            pass

def set_config(key: str, value: str):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()

def get_config(key: str) -> str:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def add_bridge(dc_chat_id: int, tg_chat_id: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO bridges (dc_chat_id, tg_chat_id) VALUES (?, ?)", (dc_chat_id, tg_chat_id))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

def remove_bridge(dc_chat_id: int):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bridges WHERE dc_chat_id = ?", (dc_chat_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

def get_tg_chats(dc_chat_id: int) -> list[int]:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT tg_chat_id FROM bridges WHERE dc_chat_id = ?", (dc_chat_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

def get_dc_chats(tg_chat_id: int) -> list[int]:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT dc_chat_id FROM bridges WHERE tg_chat_id = ?", (tg_chat_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

def save_message_map(dc_msg_id: int, dc_chat_id: int, tg_msg_id: int, tg_chat_id: int):
    """Save a mapping between a DC message and a TG message."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT OR REPLACE INTO message_map (dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id) VALUES (?, ?, ?, ?)",
                (dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id)
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

def get_tg_msg_id(dc_msg_id: int, dc_chat_id: int, tg_chat_id: int) -> int | None:
    """Look up the TG message ID for a given DC message."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tg_msg_id FROM message_map WHERE dc_msg_id = ? AND dc_chat_id = ? AND tg_chat_id = ?",
            (dc_msg_id, dc_chat_id, tg_chat_id)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def get_tg_mappings_by_dc_msg_id(dc_msg_id: int) -> list[tuple[int, int]]:
    """Look up all (tg_msg_id, tg_chat_id) mappings for a given DC message."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tg_msg_id, tg_chat_id FROM message_map WHERE dc_msg_id = ?",
            (dc_msg_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

def get_dc_msg_id(tg_msg_id: int, tg_chat_id: int, dc_chat_id: int) -> int | None:
    """Look up the DC message ID for a given TG message."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT dc_msg_id FROM message_map WHERE tg_msg_id = ? AND tg_chat_id = ? AND dc_chat_id = ?",
            (tg_msg_id, tg_chat_id, dc_chat_id)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def save_poll_context(poll_id: str, tg_chat_id: int, dc_chat_id: int):
    """Save context for a telegram poll so we know where to send updates."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
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
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tg_chat_id, dc_chat_id FROM polls WHERE poll_id = ?",
            (poll_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return row if row else None

def update_bridge_tg_chat_id(old_tg_id: int, new_tg_id: int):
    """Update all references when a TG group migrates to a supergroup."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
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

def get_all_bridges() -> list[tuple[int, int]]:
    """Return all bridge pairs as (dc_chat_id, tg_chat_id)."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT dc_chat_id, tg_chat_id FROM bridges")
        rows = cursor.fetchall()
        conn.close()
        return rows


def get_bridge_message_count(dc_chat_id: int, tg_chat_id: int) -> int:
    """Return the number of relayed messages for a specific bridge pair."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM message_map WHERE dc_chat_id = ? AND tg_chat_id = ?",
            (dc_chat_id, tg_chat_id)
        )
        count = cursor.fetchone()[0]
        conn.close()
        return count


def cleanup_old_messages(limit=10000):
    """Run this periodically to prevent DB bloat."""
    with _lock:
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM message_map WHERE rowid NOT IN (
                    SELECT rowid FROM message_map ORDER BY rowid DESC LIMIT ?
                )
            ''', (limit,))
            conn.commit()
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------
# CHANNEL BRIDGING
# ---------------------------------------------------------

def add_channel(tg_username: str, dc_chat_id: int, invite_link: str | None = None) -> int | None:
    """Add a TG channel -> DC broadcast mapping. Returns the row id."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO channels (tg_channel_username, dc_chat_id, invite_link) VALUES (?, ?, ?)",
                (tg_username.lower(), dc_chat_id, invite_link)
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
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

def get_channel_by_tg_username(username: str) -> dict | None:
    """Get a channel row by TG username."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels WHERE tg_channel_username = ?", (username.lower(),))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

def get_channel_by_tg_id(tg_channel_id: int) -> dict | None:
    """Get a channel row by TG numeric channel ID."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels WHERE tg_channel_id = ?", (tg_channel_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

def get_all_channels() -> list[dict]:
    """Return all channel rows."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels ORDER BY id")
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

def remove_channel(channel_id: int) -> bool:
    """Remove a channel by its id."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

def update_channel_tg_id(username: str, tg_channel_id: int):
    """Set the numeric TG channel ID once we receive the first post."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE channels SET tg_channel_id = ? WHERE tg_channel_username = ?",
            (tg_channel_id, username.lower())
        )
        conn.commit()
        conn.close()

def get_dc_channel_chat_id(tg_channel_id: int) -> int | None:
    """Get the DC broadcast chat ID for a TG channel."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT dc_chat_id FROM channels WHERE tg_channel_id = ?", (tg_channel_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None


init_db()
