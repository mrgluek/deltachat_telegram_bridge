import sqlite3

from .base import BaseRepository


class TransportRepo(BaseRepository):
    def _upsert_transport(self, addr: str, sent_inc: int, recv_inc: int):
        with self._db._conn() as c:
            now_col = "last_sent_at" if sent_inc else "last_received_at"
            c.execute('''INSERT INTO transport_stats (addr, msgs_sent, msgs_received, {0})
                VALUES (?, ?, ?, CAST(strftime('%s','now') AS INTEGER))
                ON CONFLICT(addr) DO UPDATE SET
                msgs_sent = msgs_sent + ?, msgs_received = msgs_received + ?,
                {0} = CAST(strftime('%s','now') AS INTEGER)'''.format(now_col),
                      (addr, sent_inc, recv_inc, sent_inc, recv_inc))

    def increment_transport_sent(self, addr: str):
        self._upsert_transport(addr, 1, 0)

    def increment_transport_received(self, addr: str):
        self._upsert_transport(addr, 0, 1)

    def get_all_transport_stats(self) -> list[dict]:
        with self._db._conn(write=False) as c:
            c.row_factory = sqlite3.Row
            c.execute("SELECT * FROM transport_stats ORDER BY msgs_sent + msgs_received DESC")
            return [dict(r) for r in c.fetchall()]
