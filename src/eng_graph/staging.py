"""In-memory staging. Abandoned preparations never touch SQLite.

Entries live only in this process (default TTL 30 minutes) behind an ``RLock``.
A restart drops them. That is the privacy property: a declined or abandoned
proposal leaves no durable trace.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Generic, TypeVar

from eng_graph.models import DEFAULT_TTL_SECONDS

T = TypeVar("T")


@dataclass
class Preparation:
    session_id: str
    candidates: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    placements: list[dict] = field(default_factory=list)


@dataclass
class ProviderApproval:
    """Server-chosen fact ids for one recall. Callers cannot widen this list."""

    session_id: str
    query: str
    fact_ids: list[str]


class TtlStaged(Generic[T]):
    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self.ttl = ttl_seconds
        self._lock = RLock()
        self._items: dict[str, tuple[float, T, str]] = {}

    def put(self, prefix: str, value: T, session_id: str) -> str:
        with self._lock:
            self._purge(time.monotonic())
            key = f"{prefix}_{secrets.token_hex(8)}"
            self._items[key] = (time.monotonic() + self.ttl, value, session_id)
            return key

    def get(self, key: str) -> T | None:
        with self._lock:
            now = time.monotonic()
            self._purge(now)
            item = self._items.get(key)
            if item is None:
                return None
            return item[1]

    def pop(self, key: str) -> T | None:
        with self._lock:
            now = time.monotonic()
            self._purge(now)
            item = self._items.pop(key, None)
            if item is None:
                return None
            return item[1]

    def drop_session(self, session_id: str) -> int:
        with self._lock:
            keys = [key for key, item in self._items.items() if item[2] == session_id]
            for key in keys:
                self._items.pop(key, None)
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            self._purge(time.monotonic())
            return len(self._items)

    def _purge(self, now: float) -> None:
        dead = [key for key, (expiry, _value, _sid) in self._items.items() if expiry <= now]
        for key in dead:
            self._items.pop(key, None)
