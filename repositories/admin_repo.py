from typing import Optional

from .base import BaseRepository


class AdminRepo(BaseRepository):
    def add_admin(self, tg_user_id: int) -> bool:
        return self._insert("admins", {"tg_user_id": tg_user_id}) is not None

    def remove_admin(self, tg_user_id: int) -> bool:
        with self._db._conn() as c:
            c.execute("DELETE FROM admins WHERE tg_user_id=?", (tg_user_id,))
            return c.rowcount > 0

    def get_all_admins(self) -> list[int]:
        with self._db._conn(write=False) as c:
            c.execute("SELECT tg_user_id FROM admins ORDER BY created_at")
            return [r[0] for r in c.fetchall()]

    def is_admin(self, tg_user_id: int) -> bool:
        with self._db._conn(write=False) as c:
            c.execute("SELECT 1 FROM admins WHERE tg_user_id=?", (tg_user_id,))
            return c.fetchone() is not None

    def is_owner_or_admin(self, tg_user_id: int) -> bool:
        admin_tg_id = self._db.get_config("admin_tg_id")
        if admin_tg_id and str(tg_user_id) == str(admin_tg_id):
            return True
        return self.is_admin(tg_user_id)

    def is_owner(self, tg_user_id: int) -> bool:
        admin_tg_id = self._db.get_config("admin_tg_id")
        return bool(admin_tg_id and str(tg_user_id) == str(admin_tg_id))
