"""Saved hosts and preferences, plus hosts from ~/.ssh/config."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QSettings

DEFAULT_CACHE_MB = 512


def ssh_config_hosts(path: Optional[str] = None) -> list[str]:
    path = path or os.path.expanduser("~/.ssh/config")
    hosts: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as config:
            for line in config:
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower() == "host":
                    hosts += [h for h in parts[1:]
                              if not any(c in h for c in "*?!") and h not in hosts]
    except OSError:
        pass
    return hosts


class Settings:
    def __init__(self):
        self._store = QSettings("RemoteViewer", "RemoteViewer")

    @property
    def recent_hosts(self) -> list[str]:
        value = self._store.value("hosts/recent", [])
        return [value] if isinstance(value, str) else list(value or [])

    def remember_host(self, host: str) -> None:
        self._store.setValue("hosts/recent",
                             ([host] + [h for h in self.recent_hosts if h != host])[:20])

    @property
    def cache_mb(self) -> int:
        return int(self._store.value("cache/size_mb", DEFAULT_CACHE_MB))

    @cache_mb.setter
    def cache_mb(self, value: int) -> None:
        self._store.setValue("cache/size_mb", value)

    @property
    def theme(self) -> str:
        return str(self._store.value("appearance/theme", "auto"))

    @theme.setter
    def theme(self, value: str) -> None:
        self._store.setValue("appearance/theme", value)

    @property
    def watcher(self) -> str:
        return str(self._store.value("watcher/mode", "auto"))

    @watcher.setter
    def watcher(self, value: str) -> None:
        self._store.setValue("watcher/mode", value)
