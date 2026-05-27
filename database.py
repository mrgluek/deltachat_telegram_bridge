import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional

from repositories import ConfigRepo, BridgeRepo, ChannelRepo, DcChannelRepo, AdminRepo, TransportRepo


DB_PATH = "bridge.db"


class Database:
    def __init__(self, path=DB_PATH):
        self._path = path
        self._lock = threading.Lock()
        self.config = ConfigRepo(self)
        self.bridges = BridgeRepo(self)
        self.channels = ChannelRepo(self)
        self.dc_channels = DcChannelRepo(self)
        self.admins = AdminRepo(self)
        self.transports = TransportRepo(self)

    @contextmanager
    def _conn(self, write=True):
        with self._lock:
            conn = sqlite3.connect(self._path)
            c = conn.cursor()
            try:
                yield c
                if write:
                    conn.commit()
            finally:
                conn.close()

    # --- Schema & Migration ---

    def init_db(self):
        with self._conn() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS bridges (
                dc_chat_id INTEGER, tg_chat_id INTEGER, reactions_count INTEGER DEFAULT 0,
                PRIMARY KEY (dc_chat_id, tg_chat_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY, value TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS message_map (
                dc_msg_id INTEGER, dc_chat_id INTEGER, tg_msg_id INTEGER,
                tg_chat_id INTEGER, content_hash TEXT,
                PRIMARY KEY (dc_msg_id, dc_chat_id, tg_chat_id))''')
            c.execute('''DELETE FROM message_map WHERE rowid NOT IN (
                SELECT rowid FROM message_map ORDER BY rowid DESC LIMIT 10000)''')
            c.execute('''CREATE TABLE IF NOT EXISTS polls (
                poll_id TEXT PRIMARY KEY, tg_chat_id INTEGER, dc_chat_id INTEGER)''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_tg_msg ON message_map (tg_msg_id, tg_chat_id)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_dc_msg ON message_map (dc_msg_id, dc_chat_id)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_dc_chat ON message_map (dc_chat_id)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_tg_chat ON message_map (tg_chat_id)')
            c.execute('''CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tg_channel_username TEXT UNIQUE,
                tg_channel_id INTEGER UNIQUE, dc_chat_id INTEGER NOT NULL,
                invite_link TEXT, reactions_count INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT (strftime(\'%s\',\'now\')), created_by_tg_id INTEGER)''')
            self._migrate_channels(c)
            c.execute('''CREATE TABLE IF NOT EXISTS admins (
                tg_user_id INTEGER PRIMARY KEY,
                created_at INTEGER DEFAULT (strftime(\'%s\',\'now\')))''')
            self._add_column_if_missing(c, 'bridges', 'created_by_tg_id', 'INTEGER')
            self._add_column_if_missing(c, 'channels', 'created_by_tg_id', 'INTEGER')
            self._add_column_if_missing(c, 'channels', 'tg_participants_count', 'INTEGER DEFAULT 0')
            self._add_column_if_missing(c, 'bridges', 'reactions_count', 'INTEGER DEFAULT 0')
            self._add_column_if_missing(c, 'message_map', 'content_hash', 'TEXT')
            c.execute('''CREATE TABLE IF NOT EXISTS dc_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT, dc_chat_id INTEGER NOT NULL UNIQUE,
                dc_chat_name TEXT, tg_channel_id INTEGER, tg_invite_link TEXT,
                created_at INTEGER DEFAULT (strftime(\'%s\',\'now\')), created_by_tg_id INTEGER)''')
            self._add_column_if_missing(c, 'dc_channels', 'tg_invite_link', 'TEXT')
            c.execute('''CREATE TABLE IF NOT EXISTS transport_stats (
                addr TEXT PRIMARY KEY, msgs_sent INTEGER DEFAULT 0,
                msgs_received INTEGER DEFAULT 0, last_sent_at INTEGER, last_received_at INTEGER)''')
        try:
            os.chmod(DB_PATH, 0o600)
        except Exception:
            pass

    def _migrate_channels(self, c):
        try:
            col_info = c.execute("PRAGMA table_info(channels)").fetchall()
            for col in col_info:
                if col[1] == 'tg_channel_username' and col[3] == 1:
                    c.execute("ALTER TABLE channels RENAME TO channels_old")
                    c.execute('''CREATE TABLE channels (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tg_channel_username TEXT UNIQUE, tg_channel_id INTEGER UNIQUE,
                        dc_chat_id INTEGER NOT NULL, invite_link TEXT,
                        created_at INTEGER DEFAULT (strftime(\'%s\',\'now\')))''')
                    c.execute('''INSERT INTO channels (id, tg_channel_username, tg_channel_id, dc_chat_id, invite_link, created_at)
                        SELECT id, tg_channel_username, tg_channel_id, dc_chat_id, invite_link, created_at FROM channels_old''')
                    c.execute("DROP TABLE channels_old")
                    break
        except Exception:
            pass

    def _add_column_if_missing(self, c, table, column, definition):
        try:
            col_names = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            if column not in col_names:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except Exception:
            pass

    # --- Delegated methods (thin wrappers around repos) ---

    def set_config(self, key: str, value: str):
        self.config.set_config(key, value)

    def get_config(self, key: str) -> Optional[str]:
        return self.config.get_config(key)

    def add_bridge(self, dc_chat_id: int, tg_chat_id: int, created_by_tg_id: int = None):
        return self.bridges.add_bridge(dc_chat_id, tg_chat_id, created_by_tg_id)

    def remove_bridge(self, dc_chat_id: int) -> list[int]:
        return self.bridges.remove_bridge(dc_chat_id)

    def remove_bridge_by_tg(self, tg_chat_id: int):
        return self.bridges.remove_bridge_by_tg(tg_chat_id)

    def get_tg_chats(self, dc_chat_id: int) -> list[int]:
        return self.bridges.get_tg_chats(dc_chat_id)

    def count_bridges_for_tg(self, tg_chat_id: int) -> int:
        return self.bridges.count_bridges_for_tg(tg_chat_id)

    def get_dc_chats(self, tg_chat_id: int) -> list[int]:
        return self.bridges.get_dc_chats(tg_chat_id)

    def get_all_bridges(self) -> list[tuple]:
        return self.bridges.get_all_bridges()

    def get_bridge_message_count(self, dc_chat_id: int, tg_chat_id: int) -> int:
        return self.bridges.get_bridge_message_count(dc_chat_id, tg_chat_id)

    def get_bridge_creator(self, dc_chat_id: int) -> Optional[int]:
        return self.bridges.get_bridge_creator(dc_chat_id)

    def get_bridge_creator_by_tg(self, tg_chat_id: int) -> Optional[int]:
        return self.bridges.get_bridge_creator_by_tg(tg_chat_id)

    def get_bridges_by_creator(self, tg_user_id: int) -> list[tuple[int, int]]:
        return self.bridges.get_bridges_by_creator(tg_user_id)

    def increment_bridge_reaction_count(self, dc_chat_id: int, tg_chat_id: int):
        self.bridges.increment_bridge_reaction_count(dc_chat_id, tg_chat_id)

    def get_bridge_reaction_count(self, dc_chat_id: int, tg_chat_id: int) -> int:
        return self.bridges.get_bridge_reaction_count(dc_chat_id, tg_chat_id)

    def update_bridge_tg_chat_id(self, old_tg_id: int, new_tg_id: int):
        self.bridges.update_bridge_tg_chat_id(old_tg_id, new_tg_id)

    def save_message_map(self, dc_msg_id: int, dc_chat_id: int, tg_msg_id: int, tg_chat_id: int, content_hash: str = None):
        self.bridges.save_message_map(dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id, content_hash)

    def get_dc_msgs_by_tg_msg_id(self, tg_msg_id: int, tg_chat_id: int) -> list[tuple[int, int]]:
        return self.bridges.get_dc_msgs_by_tg_msg_id(tg_msg_id, tg_chat_id)

    def delete_message_map_entry_by_dc(self, dc_msg_id: int, dc_chat_id: int = None):
        self.bridges.delete_message_map_entry_by_dc(dc_msg_id, dc_chat_id)

    def delete_message_map_entry_by_tg(self, tg_msg_id: int, tg_chat_id: int = None):
        self.bridges.delete_message_map_entry_by_tg(tg_msg_id, tg_chat_id)

    def get_tg_msg_id(self, dc_msg_id: int, dc_chat_id: int, tg_chat_id: int) -> Optional[int]:
        return self.bridges.get_tg_msg_id(dc_msg_id, dc_chat_id, tg_chat_id)

    def get_tg_mappings_by_dc_msg_id(self, dc_msg_id: int) -> list[tuple[int, int, int]]:
        return self.bridges.get_tg_mappings_by_dc_msg_id(dc_msg_id)

    def get_dc_msg_id(self, tg_msg_id: int, tg_chat_id: int, dc_chat_id: int) -> Optional[int]:
        return self.bridges.get_dc_msg_id(tg_msg_id, tg_chat_id, dc_chat_id)

    def get_message_content_hash(self, tg_msg_id: int, tg_chat_id: int, dc_chat_id: int) -> Optional[str]:
        return self.bridges.get_message_content_hash(tg_msg_id, tg_chat_id, dc_chat_id)

    def cleanup_old_messages(self, limit=10000):
        self.bridges.cleanup_old_messages(limit)

    def save_poll_context(self, poll_id: str, tg_chat_id: int, dc_chat_id: int):
        self.bridges.save_poll_context(poll_id, tg_chat_id, dc_chat_id)

    def get_poll_context(self, poll_id: str) -> Optional[tuple[int, int]]:
        return self.bridges.get_poll_context(poll_id)

    def add_channel(self, tg_username: str, dc_chat_id: int, invite_link: str = None, created_by_tg_id: int = None) -> Optional[int]:
        return self.channels.add_channel(tg_username, dc_chat_id, invite_link, created_by_tg_id)

    def add_channel_by_id(self, tg_channel_id: int, dc_chat_id: int, invite_link: str = None, username: str = None, created_by_tg_id: int = None) -> Optional[int]:
        return self.channels.add_channel_by_id(tg_channel_id, dc_chat_id, invite_link, username, created_by_tg_id)

    def get_channel_by_id(self, channel_id: int) -> Optional[dict]:
        return self.channels.get_channel_by_id(channel_id)

    def get_channel_by_tg_username(self, username: str) -> Optional[dict]:
        return self.channels.get_channel_by_tg_username(username)

    def get_channel_by_tg_id(self, tg_channel_id: int) -> Optional[dict]:
        return self.channels.get_channel_by_tg_id(tg_channel_id)

    def get_channel_by_dc_chat_id(self, dc_chat_id: int) -> Optional[dict]:
        return self.channels.get_channel_by_dc_chat_id(dc_chat_id)

    def get_all_channels(self) -> list[dict]:
        return self.channels.get_all_channels()

    def get_channels_by_creator(self, tg_user_id: int) -> list[dict]:
        return self.channels.get_channels_by_creator(tg_user_id)

    def get_channel_creator(self, channel_id: int) -> Optional[int]:
        return self.channels.get_channel_creator(channel_id)

    def update_channel_info(self, channel_id: int, participants_count: int = None, username: str = None, title: str = None):
        self.channels.update_channel_info(channel_id, participants_count, username, title)

    def update_channel_tg_id(self, username: str, tg_channel_id: int):
        self.channels.update_channel_tg_id(username, tg_channel_id)

    def update_channel_invite_link(self, channel_id: int, invite_link: str):
        self.channels.update_channel_invite_link(channel_id, invite_link)

    def remove_channel(self, channel_id: int) -> Optional[int]:
        return self.channels.remove_channel(channel_id)

    def get_dc_channel_chat_id(self, tg_channel_id: int) -> Optional[int]:
        return self.channels.get_dc_channel_chat_id(tg_channel_id)

    def increment_channel_reaction_count(self, tg_channel_id: int):
        self.channels.increment_channel_reaction_count(tg_channel_id)

    def get_channel_reaction_count(self, tg_channel_id: int) -> int:
        return self.channels.get_channel_reaction_count(tg_channel_id)

    def add_dc_channel(self, dc_chat_id: int, dc_chat_name: str = None, tg_channel_id: int = None, tg_invite_link: str = None, created_by_tg_id: int = None) -> Optional[int]:
        return self.dc_channels.add_dc_channel(dc_chat_id, dc_chat_name, tg_channel_id, tg_invite_link, created_by_tg_id)

    def get_dc_channel_by_dc_chat_id(self, dc_chat_id: int) -> Optional[dict]:
        return self.dc_channels.get_dc_channel_by_dc_chat_id(dc_chat_id)

    def get_dc_channel_by_id(self, channel_id: int) -> Optional[dict]:
        return self.dc_channels.get_dc_channel_by_id(channel_id)

    def get_all_dc_channels(self) -> list[dict]:
        return self.dc_channels.get_all_dc_channels()

    def remove_dc_channel(self, dc_chat_id: int) -> Optional[int]:
        return self.dc_channels.remove_dc_channel(dc_chat_id)

    def update_dc_channel_tg_info(self, dc_chat_id: int, tg_channel_id: int, tg_invite_link: str = None):
        self.dc_channels.update_dc_channel_tg_info(dc_chat_id, tg_channel_id, tg_invite_link)

    def add_admin(self, tg_user_id: int) -> bool:
        return self.admins.add_admin(tg_user_id)

    def remove_admin(self, tg_user_id: int) -> bool:
        return self.admins.remove_admin(tg_user_id)

    def get_all_admins(self) -> list[int]:
        return self.admins.get_all_admins()

    def is_admin(self, tg_user_id: int) -> bool:
        return self.admins.is_admin(tg_user_id)

    def is_owner_or_admin(self, tg_user_id: int) -> bool:
        return self.admins.is_owner_or_admin(tg_user_id)

    def is_owner(self, tg_user_id: int) -> bool:
        return self.admins.is_owner(tg_user_id)

    def increment_transport_sent(self, addr: str):
        self.transports.increment_transport_sent(addr)

    def increment_transport_received(self, addr: str):
        self.transports.increment_transport_received(addr)

    def get_all_transport_stats(self) -> list[dict]:
        return self.transports.get_all_transport_stats()


db = Database()

# --- Module-level backward-compatible API (all 59 functions preserved) ---

def init_db():
    db.init_db()

def set_config(key: str, value: str):
    db.set_config(key, value)

def get_config(key: str) -> Optional[str]:
    return db.get_config(key)

def add_bridge(dc_chat_id: int, tg_chat_id: int, created_by_tg_id: int | None = None):
    return db.add_bridge(dc_chat_id, tg_chat_id, created_by_tg_id)

def remove_bridge(dc_chat_id: int) -> list[int]:
    return db.remove_bridge(dc_chat_id)

def remove_bridge_by_tg(tg_chat_id: int):
    return db.remove_bridge_by_tg(tg_chat_id)

def get_tg_chats(dc_chat_id: int) -> list[int]:
    return db.get_tg_chats(dc_chat_id)

def count_bridges_for_tg(tg_chat_id: int) -> int:
    return db.count_bridges_for_tg(tg_chat_id)

def get_dc_chats(tg_chat_id: int) -> list[int]:
    return db.get_dc_chats(tg_chat_id)

def save_message_map(dc_msg_id: int, dc_chat_id: int, tg_msg_id: int, tg_chat_id: int, content_hash: str | None = None):
    db.save_message_map(dc_msg_id, dc_chat_id, tg_msg_id, tg_chat_id, content_hash)

def get_dc_msgs_by_tg_msg_id(tg_msg_id: int, tg_chat_id: int) -> list[tuple[int, int]]:
    return db.get_dc_msgs_by_tg_msg_id(tg_msg_id, tg_chat_id)

def delete_message_map_entry_by_dc(dc_msg_id: int, dc_chat_id: int | None = None):
    db.delete_message_map_entry_by_dc(dc_msg_id, dc_chat_id)

def delete_message_map_entry_by_tg(tg_msg_id: int, tg_chat_id: int | None = None):
    db.delete_message_map_entry_by_tg(tg_msg_id, tg_chat_id)

def get_tg_msg_id(dc_msg_id: int, dc_chat_id: int, tg_chat_id: int) -> int | None:
    return db.get_tg_msg_id(dc_msg_id, dc_chat_id, tg_chat_id)

def get_tg_mappings_by_dc_msg_id(dc_msg_id: int) -> list[tuple[int, int, int]]:
    return db.get_tg_mappings_by_dc_msg_id(dc_msg_id)

def get_dc_msg_id(tg_msg_id: int, tg_chat_id: int, dc_chat_id: int) -> int | None:
    return db.get_dc_msg_id(tg_msg_id, tg_chat_id, dc_chat_id)

def get_message_content_hash(tg_msg_id: int, tg_chat_id: int, dc_chat_id: int) -> str | None:
    return db.get_message_content_hash(tg_msg_id, tg_chat_id, dc_chat_id)

def save_poll_context(poll_id: str, tg_chat_id: int, dc_chat_id: int):
    db.save_poll_context(poll_id, tg_chat_id, dc_chat_id)

def get_poll_context(poll_id: str) -> tuple[int, int] | None:
    return db.get_poll_context(poll_id)

def update_bridge_tg_chat_id(old_tg_id: int, new_tg_id: int):
    db.update_bridge_tg_chat_id(old_tg_id, new_tg_id)

def get_all_bridges() -> list[tuple]:
    return db.get_all_bridges()

def get_bridge_message_count(dc_chat_id: int, tg_chat_id: int) -> int:
    return db.get_bridge_message_count(dc_chat_id, tg_chat_id)

def cleanup_old_messages(limit=10000):
    db.cleanup_old_messages(limit)

def increment_bridge_reaction_count(dc_chat_id: int, tg_chat_id: int):
    db.increment_bridge_reaction_count(dc_chat_id, tg_chat_id)

def increment_channel_reaction_count(tg_channel_id: int):
    db.increment_channel_reaction_count(tg_channel_id)

def get_bridge_reaction_count(dc_chat_id: int, tg_chat_id: int) -> int:
    return db.get_bridge_reaction_count(dc_chat_id, tg_chat_id)

def get_channel_reaction_count(tg_channel_id: int) -> int:
    return db.get_channel_reaction_count(tg_channel_id)

def add_channel(tg_username: str, dc_chat_id: int, invite_link: str | None = None, created_by_tg_id: int | None = None) -> int | None:
    return db.add_channel(tg_username, dc_chat_id, invite_link, created_by_tg_id)

def add_channel_by_id(tg_channel_id: int, dc_chat_id: int, invite_link: str | None = None, username: str | None = None, created_by_tg_id: int | None = None) -> int | None:
    return db.add_channel_by_id(tg_channel_id, dc_chat_id, invite_link, username, created_by_tg_id)

def get_channel_by_id(channel_id: int) -> dict | None:
    return db.get_channel_by_id(channel_id)

def get_channel_by_tg_username(username: str) -> dict | None:
    return db.get_channel_by_tg_username(username)

def get_channel_by_tg_id(tg_channel_id: int) -> dict | None:
    return db.get_channel_by_tg_id(tg_channel_id)

def update_channel_info(channel_id: int, participants_count: int = None, username: str = None, title: str = None):
    db.update_channel_info(channel_id, participants_count, username, title)

def get_all_channels() -> list[dict]:
    return db.get_all_channels()

def remove_channel(channel_id: int) -> int | None:
    return db.remove_channel(channel_id)

def update_channel_tg_id(username: str, tg_channel_id: int):
    db.update_channel_tg_id(username, tg_channel_id)

def update_channel_invite_link(channel_id: int, invite_link: str):
    db.update_channel_invite_link(channel_id, invite_link)

def get_dc_channel_chat_id(tg_channel_id: int) -> int | None:
    return db.get_dc_channel_chat_id(tg_channel_id)

def get_channel_by_dc_chat_id(dc_chat_id: int) -> dict | None:
    return db.get_channel_by_dc_chat_id(dc_chat_id)

def add_dc_channel(dc_chat_id: int, dc_chat_name: str | None = None, tg_channel_id: int | None = None, tg_invite_link: str | None = None, created_by_tg_id: int | None = None) -> int | None:
    return db.add_dc_channel(dc_chat_id, dc_chat_name, tg_channel_id, tg_invite_link, created_by_tg_id)

def get_dc_channel_by_dc_chat_id(dc_chat_id: int) -> dict | None:
    return db.get_dc_channel_by_dc_chat_id(dc_chat_id)

def get_dc_channel_by_id(channel_id: int) -> dict | None:
    return db.get_dc_channel_by_id(channel_id)

def get_all_dc_channels() -> list[dict]:
    return db.get_all_dc_channels()

def remove_dc_channel(dc_chat_id: int) -> int | None:
    return db.remove_dc_channel(dc_chat_id)

def update_dc_channel_tg_info(dc_chat_id: int, tg_channel_id: int, tg_invite_link: str | None = None):
    db.update_dc_channel_tg_info(dc_chat_id, tg_channel_id, tg_invite_link)

def add_admin(tg_user_id: int) -> bool:
    return db.add_admin(tg_user_id)

def remove_admin(tg_user_id: int) -> bool:
    return db.remove_admin(tg_user_id)

def get_all_admins() -> list[int]:
    return db.get_all_admins()

def is_admin(tg_user_id: int) -> bool:
    return db.is_admin(tg_user_id)

def is_owner_or_admin(tg_user_id: int) -> bool:
    return db.is_owner_or_admin(tg_user_id)

def is_owner(tg_user_id: int) -> bool:
    return db.is_owner(tg_user_id)

def get_bridge_creator(dc_chat_id: int) -> int | None:
    return db.get_bridge_creator(dc_chat_id)

def get_bridge_creator_by_tg(tg_chat_id: int) -> int | None:
    return db.get_bridge_creator_by_tg(tg_chat_id)

def get_bridges_by_creator(tg_user_id: int) -> list[tuple[int, int]]:
    return db.get_bridges_by_creator(tg_user_id)

def get_channel_creator(channel_id: int) -> int | None:
    return db.get_channel_creator(channel_id)

def get_channels_by_creator(tg_user_id: int) -> list[dict]:
    return db.get_channels_by_creator(tg_user_id)

def increment_transport_sent(addr: str):
    db.increment_transport_sent(addr)

def increment_transport_received(addr: str):
    db.increment_transport_received(addr)

def get_all_transport_stats() -> list[dict]:
    return db.get_all_transport_stats()

init_db()
