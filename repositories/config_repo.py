from typing import Optional

from .base import BaseRepository


class ConfigRepo(BaseRepository):
    def set_config(self, key: str, value: str):
        self._insert("config", {"key": key, "value": value}, or_replace=True)

    def get_config(self, key: str) -> Optional[str]:
        return self._get("config", "value", "key=?", (key,))
