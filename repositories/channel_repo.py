from typing import Optional

import sqlite3
from .base import BaseRepository


class ChannelRepo(BaseRepository):
    def add_channel(self, tg_username: str, dc_chat_id: int, invite_link: str = None,
                    created_by_tg_id: int = None) -> Optional[int]:
        return self._insert("channels", {
            "tg_channel_username": tg_username.lower(), "dc_chat_id": dc_chat_id,
            "invite_link": invite_link, "created_by_tg_id": created_by_tg_id, "reactions_count": 0
        })

    def add_channel_by_id(self, tg_channel_id: int, dc_chat_id: int, invite_link: str = None,
                          username: str = None, created_by_tg_id: int = None) -> Optional[int]:
        return self._insert("channels", {
            "tg_channel_username": username.lower() if username else None,
            "tg_channel_id": tg_channel_id, "dc_chat_id": dc_chat_id,
            "invite_link": invite_link, "created_by_tg_id": created_by_tg_id, "reactions_count": 0
        })

    def _get_channel_row(self, where, params):
        with self._db._conn(write=False) as c:
            c.row_factory = sqlite3.Row
            c.execute(f"SELECT * FROM channels WHERE {where}", params)
            row = c.fetchone()
            return dict(row) if row else None

    def get_channel_by_id(self, channel_id: int) -> Optional[dict]:
        return self._get_channel_row("id=?", (channel_id,))

    def get_channel_by_tg_username(self, username: str) -> Optional[dict]:
        return self._get_channel_row("tg_channel_username=?", (username.lower(),))

    def get_channel_by_tg_id(self, tg_channel_id: int) -> Optional[dict]:
        return self._get_channel_row("tg_channel_id=?", (tg_channel_id,))

    def get_channel_by_dc_chat_id(self, dc_chat_id: int) -> Optional[dict]:
        return self._get_channel_row("dc_chat_id=?", (dc_chat_id,))

    def get_all_channels(self) -> list[dict]:
        with self._db._conn(write=False) as c:
            c.row_factory = sqlite3.Row
            c.execute("SELECT * FROM channels ORDER BY id")
            return [dict(r) for r in c.fetchall()]

    def get_channels_by_creator(self, tg_user_id: int) -> list[dict]:
        with self._db._conn(write=False) as c:
            c.row_factory = sqlite3.Row
            c.execute("SELECT * FROM channels WHERE created_by_tg_id=? ORDER BY id", (tg_user_id,))
            return [dict(r) for r in c.fetchall()]

    def get_channel_creator(self, channel_id: int) -> Optional[int]:
        return self._get("channels", "created_by_tg_id", "id=?", (channel_id,))

    def update_channel_info(self, channel_id: int, participants_count: int = None,
                            username: str = None, title: str = None):
        with self._db._conn() as c:
            if participants_count is not None:
                c.execute("UPDATE channels SET tg_participants_count=? WHERE id=?",
                          (participants_count, channel_id))
            if username is not None:
                c.execute("UPDATE channels SET tg_channel_username=? WHERE id=?",
                          (username.lower(), channel_id))

    def update_channel_tg_id(self, username: str, tg_channel_id: int):
        self._update("channels", "tg_channel_id=?", (tg_channel_id, username.lower()))

    def update_channel_invite_link(self, channel_id: int, invite_link: str):
        self._update("channels", "invite_link=?", (invite_link, channel_id))

    def remove_channel(self, channel_id: int) -> Optional[int]:
        with self._db._conn() as c:
            c.execute("SELECT dc_chat_id, tg_channel_id FROM channels WHERE id=?", (channel_id,))
            row = c.fetchone()
            tg_channel_id = None
            if row:
                dc_chat_id, tg_channel_id = row
                c.execute("DELETE FROM message_map WHERE dc_chat_id=?", (dc_chat_id,))
            c.execute("DELETE FROM channels WHERE id=?", (channel_id,))
            return tg_channel_id

    def get_dc_channel_chat_id(self, tg_channel_id: int) -> Optional[int]:
        return self._get("channels", "dc_chat_id", "tg_channel_id=?", (tg_channel_id,))

    def increment_channel_reaction_count(self, tg_channel_id: int):
        self._increment("channels", "reactions_count", "tg_channel_id=?", (tg_channel_id,))

    def get_channel_reaction_count(self, tg_channel_id: int) -> int:
        return self._get("channels", "reactions_count", "tg_channel_id=?", (tg_channel_id,)) or 0
