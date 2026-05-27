from typing import Optional

import sqlite3
from .base import BaseRepository


class DcChannelRepo(BaseRepository):
    def add_dc_channel(self, dc_chat_id: int, dc_chat_name: str = None, tg_channel_id: int = None,
                       tg_invite_link: str = None, created_by_tg_id: int = None) -> Optional[int]:
        return self._insert("dc_channels", {
            "dc_chat_id": dc_chat_id, "dc_chat_name": dc_chat_name,
            "tg_channel_id": tg_channel_id, "tg_invite_link": tg_invite_link,
            "created_by_tg_id": created_by_tg_id
        })

    def _get_dc_channel_row(self, where, params):
        with self._db._conn(write=False) as c:
            c.row_factory = sqlite3.Row
            c.execute(f"SELECT * FROM dc_channels WHERE {where}", params)
            row = c.fetchone()
            return dict(row) if row else None

    def get_dc_channel_by_dc_chat_id(self, dc_chat_id: int) -> Optional[dict]:
        return self._get_dc_channel_row("dc_chat_id=?", (dc_chat_id,))

    def get_dc_channel_by_id(self, channel_id: int) -> Optional[dict]:
        return self._get_dc_channel_row("id=?", (channel_id,))

    def get_all_dc_channels(self) -> list[dict]:
        with self._db._conn(write=False) as c:
            c.row_factory = sqlite3.Row
            c.execute("SELECT * FROM dc_channels ORDER BY id")
            return [dict(r) for r in c.fetchall()]

    def remove_dc_channel(self, dc_chat_id: int) -> Optional[int]:
        with self._db._conn() as c:
            c.execute("SELECT tg_channel_id FROM dc_channels WHERE dc_chat_id=?", (dc_chat_id,))
            row = c.fetchone()
            tg_channel_id = row[0] if row else None
            c.execute("DELETE FROM dc_channels WHERE dc_chat_id=?", (dc_chat_id,))
            c.execute("DELETE FROM message_map WHERE dc_chat_id=?", (dc_chat_id,))
            return tg_channel_id

    def update_dc_channel_tg_info(self, dc_chat_id: int, tg_channel_id: int, tg_invite_link: str = None):
        with self._db._conn() as c:
            c.execute(
                "UPDATE dc_channels SET tg_channel_id=?, tg_invite_link=COALESCE(?, tg_invite_link) WHERE dc_chat_id=?",
                (tg_channel_id, tg_invite_link, dc_chat_id))
