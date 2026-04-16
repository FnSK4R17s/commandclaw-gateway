"""In-memory cache backend — for dev/testing without Redis dependency."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class MemoryCache:
    """Simple LRU in-memory cache with TTL."""

    def __init__(self, max_size: int = 10_000):
        self._store: OrderedDict[str, tuple[dict, float]] = OrderedDict()
        self._max_size = max_size

    async def get(self, key: str) -> dict[str, Any] | None:
        if key not in self._store:
            return None
        data, expires_at = self._store[key]
        if time.time() > expires_at:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return data

    async def set(self, key: str, value: dict[str, Any], ttl: int = 86400) -> None:
        if len(self._store) >= self._max_size:
            self._store.popitem(last=False)
        self._store[key] = (value, time.time() + ttl)

    async def delete(self, key: str) -> bool:
        if key in self._store:
            del self._store[key]
            return True
        return False

    async def flush(self) -> int:
        count = len(self._store)
        self._store.clear()
        return count

    def size(self) -> int:
        return len(self._store)


_memory_cache: MemoryCache | None = None


def get_memory_cache() -> MemoryCache:
    global _memory_cache
    if _memory_cache is None:
        _memory_cache = MemoryCache()
    return _memory_cache
