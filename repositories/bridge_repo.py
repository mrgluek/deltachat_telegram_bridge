from typing import Optional

from .base import BaseRepository


class BridgeRepo(BaseRepository):
    def add_bridge(self, dc_chat_id: int, tg_chat_id: int, created_by_tg_id: int = None):
        return self._insert("bridges", {
            "dc_chat_id": dc_chat_id, "tg_chat_id": tg_chat_id,
            "created_by_tg_id": created_by_tg_id, "reactions_count": 0
        }) is not None

    def remove_bridge(self, dc_chat_id: int) -> list[int]:
        with self._db._conn() as c:
            c.execute("SELECT tg_chat_id FROM bridges WHERE dc_chat_id=?", (dc_chat_id,))
            tg_ids = [r[0] for r in c.fetchall()]
            c.execute("DELETE FROM bridges WHERE dc_chat_id=?", (dc_chat_id,))
            c.execute("DELETE FROM message_map WHERE dc_chat_id=?", (dc_chat_id,))
            return tg_ids

    def remove_bridge_by_tg(self, tg_chat_id: int):
        with self._db._conn() as c:
            c.execute("DELETE FROM bridges WHERE tg_chat_id=?", (tg_chat_id,))
            deleted = c.rowcount > 0
            c.execute("DELETE FROM message_map WHERE tg_chat_id=?", (tg_chat_id,))
            return deleted

    def get_tg_chats(self, dc_chat_id: int) -> list[int]:
        with self._db._conn(write=False) as c:
            c.execute("SELECT tg_chat_id FROM bridges WHERE dc_chat_id=?", (dc_chat_id,))
            return [r[0] for r in c.fetchall()]

    def count_bridges_for_tg(self, tg_chat_id: int) -> int:
        with self._db._conn(write=False) as c:
            c.execute("SELECT COUNT(*) FROM bridges WHERE tg_chat_id=?", (tg_chat_id,))
            total = c.fetchone()[0]
            for tbl in ('channels', 'dc_channels'):
                c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE tg_channel_id=?", (tg_chat_id,))
                total += c.fetchone()[0]
            return total

    def get_dc_chats(self, tg_chat_id: int) -> list[int]:
        with self._db._conn(write=False) as c:
            c.execute("SELECT dc_chat_id FROM bridges WHERE tg_chat_id=?", (tg_chat_id,))
            return [r[0] for r in c.fetchall()]

    def get_all_bridges(self) -> list[tuple]:
        return self._get_all("bridges", "dc_chat_id, tg_chat_id, reactions_count")

    def get_bridge_message_count(self, dc_chat_id: int, tg_chat_id: int) -> int:
        with self._db._conn(write=False) as c:
            c.execute("SELECT COUNT(*) FROM message_map WHERE dc_chat_id=? AND tg_chat_id=?",
                      (dc_chat_id, tg_chat_id))
            return c.fetchone()[0]

    def get_bridge_creator(self, dc_chat_id: int) -> Optional[int]:
        return self._get("bridges", "created_by_tg_id", "dc_chat_id=?", (dc_chat_id,))

    def get_bridge_creator_by_tg(self, tg_chat_id: int) -> Optional[int]:
        return self._get("bridges", "created_by_tg_id", "tg_chat_id=?", (tg_chat_id,))

    def get_bridges_by_creator(self, tg_user_id: int) -> list[tuple[int, int]]:
        return self._get_all("bridges", "dc_chat_id, tg_chat_id", "created_by_tg_id=?", (tg_user_id,))

    def increment_bridge_reaction_count(self, dc_chat_id: int, tg_chat_id: int):
        self._increment("bridges", "reactions_count",
                        "dc_chat_id=? AND tg_chat_id=?", (dc_chat_id, tg_chat_id))

    def get_bridge_reaction_count(self, dc_chat_id: int, tg_chat_id: int) -> int:
        return self._get("bridges", "reactions_count",
                         "dc_chat_id=? AND tg_chat_id=?", (dc_chat_id, tg_chat_id)) or 0

    def update_bridge_tg_chat_id(self, old_tg_id: int, new_tg_id: int):
        with self._db._conn() as c:
            try:
                c.execute("UPDATE bridges SET tg_chat_id=? WHERE tg_chat_id=?", (new_tg_id, old_tg_id))
                c.execute("UPDATE message_map SET tg_chat_id=? WHERE tg_chat_id=?", (new_tg_id, old_tg_id))
                c.execute("UPDATE polls SET tg_chat_id=? WHERE tg_chat_id=?", (new_tg_id, old_tg_id))
                print(f"Database updated for migration: {old_tg_id} -> {new_tg_id}")
            except Exception as e:
                c.connection.rollback()
                print(f"Error updating database for migration: {e}")

    def save_message_map(self, dc_msg_id: int, dc_chat_id: int, tg_msg_id: int, tg_chat_id: int,
                         content_hash: str = None):
        try:
            self._insert("message_map", {
                "dc_msg_id": dc_msg_id, "dc_chat_id": dc_chat_id,
                "tg_msg_id": tg_msg_id, "tg_chat_id": tg_chat_id,
                "content_hash": content_hash
            }, or_replace=True)
        except Exception:
            pass

    def get_dc_msgs_by_tg_msg_id(self, tg_msg_id: int, tg_chat_id: int) -> list[tuple[int, int]]:
        return self._get_all("message_map", "dc_msg_id, dc_chat_id",
                             "tg_msg_id=? AND tg_chat_id=?", (tg_msg_id, tg_chat_id))

    def delete_message_map_entry_by_dc(self, dc_msg_id: int, dc_chat_id: int = None):
        if dc_chat_id is not None:
            self._delete("message_map", "dc_msg_id=? AND dc_chat_id=?", (dc_msg_id, dc_chat_id))
        else:
            self._delete("message_map", "dc_msg_id=?", (dc_msg_id,))

    def delete_message_map_entry_by_tg(self, tg_msg_id: int, tg_chat_id: int = None):
        if tg_chat_id is not None:
            self._delete("message_map", "tg_msg_id=? AND tg_chat_id=?", (tg_msg_id, tg_chat_id))
        else:
            self._delete("message_map", "tg_msg_id=?", (tg_msg_id,))

    def get_tg_msg_id(self, dc_msg_id: int, dc_chat_id: int, tg_chat_id: int) -> Optional[int]:
        return self._get("message_map", "tg_msg_id",
                         "dc_msg_id=? AND dc_chat_id=? AND tg_chat_id=?",
                         (dc_msg_id, dc_chat_id, tg_chat_id))

    def get_tg_mappings_by_dc_msg_id(self, dc_msg_id: int) -> list[tuple[int, int, int]]:
        return self._get_all("message_map", "tg_msg_id, tg_chat_id, dc_chat_id",
                             "dc_msg_id=?", (dc_msg_id,))

    def get_dc_msg_id(self, tg_msg_id: int, tg_chat_id: int, dc_chat_id: int) -> Optional[int]:
        return self._get("message_map", "dc_msg_id",
                         "tg_msg_id=? AND tg_chat_id=? AND dc_chat_id=?",
                         (tg_msg_id, tg_chat_id, dc_chat_id))

    def get_message_content_hash(self, tg_msg_id: int, tg_chat_id: int, dc_chat_id: int) -> Optional[str]:
        return self._get("message_map", "content_hash",
                         "tg_msg_id=? AND tg_chat_id=? AND dc_chat_id=?",
                         (tg_msg_id, tg_chat_id, dc_chat_id))

    def cleanup_old_messages(self, limit=10000):
        try:
            with self._db._conn() as c:
                c.execute('''DELETE FROM message_map WHERE rowid NOT IN (
                    SELECT rowid FROM message_map ORDER BY rowid DESC LIMIT ?)''', (limit,))
        except Exception:
            pass

    def save_poll_context(self, poll_id: str, tg_chat_id: int, dc_chat_id: int):
        try:
            self._insert("polls", {"poll_id": poll_id, "tg_chat_id": tg_chat_id, "dc_chat_id": dc_chat_id},
                         or_replace=True)
        except Exception:
            pass

    def get_poll_context(self, poll_id: str) -> Optional[tuple[int, int]]:
        with self._db._conn(write=False) as c:
            c.execute("SELECT tg_chat_id, dc_chat_id FROM polls WHERE poll_id=?", (poll_id,))
            return c.fetchone()
