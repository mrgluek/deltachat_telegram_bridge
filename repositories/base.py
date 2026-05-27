import sqlite3


class BaseRepository:
    def __init__(self, db):
        self._db = db

    def _get(self, table, columns, where_clause, params, row_factory=False):
        with self._db._conn(write=False) as c:
            if row_factory:
                c.row_factory = sqlite3.Row
            c.execute(f"SELECT {columns} FROM {table} WHERE {where_clause}", params)
            row = c.fetchone()
            return dict(row) if (row_factory and row) else (row[0] if row else None)

    def _get_all(self, table, columns="*", where_clause="", params=(), order_by="", row_factory=False):
        with self._db._conn(write=False) as c:
            if row_factory:
                c.row_factory = sqlite3.Row
            query = f"SELECT {columns} FROM {table}"
            if where_clause:
                query += f" WHERE {where_clause}"
            if order_by:
                query += f" ORDER BY {order_by}"
            rows = c.execute(query, params).fetchall()
            return [dict(r) for r in rows] if row_factory else rows

    def _insert(self, table, data: dict, or_replace=False):
        with self._db._conn() as c:
            cols = ", ".join(data.keys())
            placeholders = ", ".join("?" for _ in data)
            cmd = "INSERT OR REPLACE INTO" if or_replace else "INSERT INTO"
            try:
                c.execute(f"{cmd} {table} ({cols}) VALUES ({placeholders})", tuple(data.values()))
                return c.lastrowid
            except sqlite3.IntegrityError:
                return None

    def _update(self, table, set_clause, params):
        with self._db._conn() as c:
            c.execute(f"UPDATE {table} SET {set_clause}", params)

    def _delete(self, table, where_clause, params):
        with self._db._conn() as c:
            c.execute(f"DELETE FROM {table} WHERE {where_clause}", params)

    def _increment(self, table, column, where_clause, params):
        with self._db._conn() as c:
            c.execute(f"UPDATE {table} SET {column} = {column} + 1 WHERE {where_clause}", params)
