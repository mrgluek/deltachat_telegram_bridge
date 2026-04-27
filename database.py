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
                tg_channel_username TEXT UNIQUE,
                tg_channel_id INTEGER UNIQUE,
                dc_chat_id INTEGER NOT NULL,
                invite_link TEXT,
                reactions_count INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                created_by_tg_id INTEGER
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
                            created_at INTEGER DEFAULT (strftime('%s','now'))
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
                created_at INTEGER DEFAULT (strftime('%s','now'))
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
        # Migration: add reactions_count to bridges
        try:
            col_names = [c[1] for c in cursor.execute("PRAGMA table_info(bridges)").fetchall()]
            if 'reactions_count' not in col_names:
                cursor.execute("ALTER TABLE bridges ADD COLUMN reactions_count INTEGER DEFAULT 0")
        except Exception:
            pass
        # Migration: add content_hash to message_map
        try:
            col_names = [c[1] for c in cursor.execute("PRAGMA table_info(message_map)").fetchall()]
            if 'content_hash' not in col_names:
                cursor.execute("ALTER TABLE message_map ADD COLUMN content_hash TEXT")
        except Exception:
            pass
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

def add_bridge(dc_chat_id: int, tg_chat_id: int, created_by_tg_id: int | None = None):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO bridges (dc_chat_id, tg_chat_id, created_by_tg_id, reactions_count) VALUES (?, ?, ?, 0)", (dc_chat_id, tg_chat_id, created_by_tg_id))
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

def remove_bridge_by_tg(tg_chat_id: int):
    """Remove all bridges for a given TG chat ID."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bridges WHERE tg_chat_id = ?", (tg_chat_id,))
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

def save_message_map(dc_msg_id: int, dc_chat_id: int, tg_msg_id: int, tg_chat_id: int, content_hash: str | None = None):
    """Save a mapping between a DC message and a TG message."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT OR REPLACE INTO message_map (dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id, content_hash) VALUES (?, ?, ?, ?, ?)",
                (dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id, content_hash)
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

def get_dc_msgs_by_tg_msg_id(tg_msg_id: int, tg_chat_id: int) -> list[tuple[int, int]]:
    """Return all (dc_msg_id, dc_chat_id) pairs for a given TG message (across all DC chats)."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT dc_msg_id, dc_chat_id FROM message_map WHERE tg_msg_id = ? AND tg_chat_id = ?",
            (tg_msg_id, tg_chat_id)
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

def delete_message_map_entry_by_dc(dc_msg_id: int, dc_chat_id: int) -> None:
    """Remove a message map entry by DC message ID."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM message_map WHERE dc_msg_id = ? AND dc_chat_id = ?", (dc_msg_id, dc_chat_id))
        conn.commit()
        conn.close()

def delete_message_map_entry_by_tg(tg_msg_id: int, tg_chat_id: int) -> None:
    """Remove message map entries by TG message ID."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM message_map WHERE tg_msg_id = ? AND tg_chat_id = ?", (tg_msg_id, tg_chat_id))
        conn.commit()
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

def get_message_content_hash(tg_msg_id: int, tg_chat_id: int, dc_chat_id: int) -> str | None:
    """Look up the stored content hash for a TG message."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT content_hash FROM message_map WHERE tg_msg_id = ? AND tg_chat_id = ? AND dc_chat_id = ?",
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

def get_all_bridges() -> list[tuple]:
    """Return all bridge info as (dc_chat_id, tg_chat_id, reactions_count)."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT dc_chat_id, tg_chat_id, reactions_count FROM bridges")
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


def increment_bridge_reaction_count(dc_chat_id: int, tg_chat_id: int):
    """Increment the reaction counter for a specific bridge."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE bridges SET reactions_count = reactions_count + 1 WHERE dc_chat_id = ? AND tg_chat_id = ?",
            (dc_chat_id, tg_chat_id)
        )
        conn.commit()
        conn.close()


def increment_channel_reaction_count(tg_channel_id: int):
    """Increment the reaction counter for a specific channel."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE channels SET reactions_count = reactions_count + 1 WHERE tg_channel_id = ?",
            (tg_channel_id,)
        )
        conn.commit()
        conn.close()


def get_bridge_reaction_count(dc_chat_id: int, tg_chat_id: int) -> int:
    """Return the reaction count for a specific bridge."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT reactions_count FROM bridges WHERE dc_chat_id = ? AND tg_chat_id = ?",
            (dc_chat_id, tg_chat_id)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0


def get_channel_reaction_count(tg_channel_id: int) -> int:
    """Return the reaction count for a specific channel."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT reactions_count FROM channels WHERE tg_channel_id = ?",
            (tg_channel_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0


# ---------------------------------------------------------
# CHANNEL BRIDGING
# ---------------------------------------------------------

def add_channel(tg_username: str, dc_chat_id: int, invite_link: str | None = None, created_by_tg_id: int | None = None) -> int | None:
    """Add a TG channel -> DC broadcast mapping by username. Returns the row id."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
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
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
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

def update_channel_info(channel_id: int, participants_count: int = None, username: str = None, title: str = None):
    """Update metadata for a channel."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        if participants_count is not None:
            cursor.execute("UPDATE channels SET tg_participants_count = ? WHERE id = ?", (participants_count, channel_id))
        if username is not None:
            cursor.execute("UPDATE channels SET tg_channel_username = ? WHERE id = ?", (username.lower(), channel_id))
        # Note: we don't store title in the DB currently, it's fetched from DC.
        # But if we want to store it, we'd need to add a column.
        # However, we can use the username to make it "public".
        conn.commit()
        conn.close()

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

def get_channel_by_dc_chat_id(dc_chat_id: int) -> dict | None:
    """Get a channel row by its Delta Chat chat ID."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels WHERE dc_chat_id = ?", (dc_chat_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None


# ---------------------------------------------------------
# ADMIN MANAGEMENT
# ---------------------------------------------------------

def add_admin(tg_user_id: int) -> bool:
    """Add a sub-admin. Returns False if already exists."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
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
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admins WHERE tg_user_id = ?", (tg_user_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

def get_all_admins() -> list[int]:
    """Return all sub-admin TG user IDs."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT tg_user_id FROM admins ORDER BY created_at")
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

def is_admin(tg_user_id: int) -> bool:
    """Check if a TG user is a sub-admin."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM admins WHERE tg_user_id = ?", (tg_user_id,))
        found = cursor.fetchone() is not None
        conn.close()
        return found

def is_owner_or_admin(tg_user_id: int) -> bool:
    """Check if a TG user is the owner or a sub-admin."""
    admin_tg_id = get_config("admin_tg_id")
    if admin_tg_id and str(tg_user_id) == str(admin_tg_id):
        return True
    return is_admin(tg_user_id)

def is_owner(tg_user_id: int) -> bool:
    """Check if a TG user is the owner."""
    admin_tg_id = get_config("admin_tg_id")
    return bool(admin_tg_id and str(tg_user_id) == str(admin_tg_id))


# ---------------------------------------------------------
# CREATOR-FILTERED QUERIES
# ---------------------------------------------------------

def get_bridge_creator(dc_chat_id: int) -> int | None:
    """Get the TG user ID of the bridge creator, or None."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT created_by_tg_id FROM bridges WHERE dc_chat_id = ?", (dc_chat_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def get_bridge_creator_by_tg(tg_chat_id: int) -> int | None:
    """Get the TG user ID of the bridge creator by TG chat ID, or None."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT created_by_tg_id FROM bridges WHERE tg_chat_id = ?", (tg_chat_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def get_bridges_by_creator(tg_user_id: int) -> list[tuple[int, int]]:
    """Return bridge pairs created by a specific TG user."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT dc_chat_id, tg_chat_id FROM bridges WHERE created_by_tg_id = ?", (tg_user_id,))
        rows = cursor.fetchall()
        conn.close()
        return rows

def get_channel_creator(channel_id: int) -> int | None:
    """Get the TG user ID of the channel creator, or None."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT created_by_tg_id FROM channels WHERE id = ?", (channel_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def get_channels_by_creator(tg_user_id: int) -> list[dict]:
    """Return channel rows created by a specific TG user."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels WHERE created_by_tg_id = ? ORDER BY id", (tg_user_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]


init_db()
