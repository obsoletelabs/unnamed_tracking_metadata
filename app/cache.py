from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass


@dataclass(slots=True)
class _MemoryValue:
    expires_at: float
    value: str


class MetadataCache:
    """Redis-backed cache with a bounded in-process fallback for standalone mode."""

    def __init__(self) -> None:
        self._memory: dict[str, _MemoryValue] = {}
        self._lock = threading.Lock()
        self._redis = None
        url = os.getenv("REDIS_URL", "").strip()
        if url:
            try:
                import redis

                self._redis = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
                self._redis.ping()
            except Exception:
                self._redis = None

    @property
    def distributed(self) -> bool:
        return self._redis is not None

    def get(self, key: str) -> str | None:
        if self._redis is not None:
            try:
                return self._redis.get(key)
            except Exception:
                pass
        now = time.monotonic()
        with self._lock:
            item = self._memory.get(key)
            if not item:
                return None
            if item.expires_at <= now:
                self._memory.pop(key, None)
                return None
            return item.value

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        if ttl_seconds < 1 or ttl_seconds > 86400:
            raise ValueError("ttl_seconds must be between 1 and 86400")
        if self._redis is not None:
            try:
                self._redis.setex(key, ttl_seconds, value)
                return
            except Exception:
                pass
        now = time.monotonic()
        with self._lock:
            self._memory[key] = _MemoryValue(now + ttl_seconds, value)
            if len(self._memory) > 10000:
                for old_key in list(self._memory):
                    if self._memory[old_key].expires_at <= now:
                        self._memory.pop(old_key, None)

    def delete(self, key: str) -> None:
        if self._redis is not None:
            try:
                self._redis.delete(key)
            except Exception:
                pass
        with self._lock:
            self._memory.pop(key, None)

    def get_json(self, key: str):
        value = self.get(key)
        return json.loads(value) if value is not None else None

    def set_json(self, key: str, value, ttl_seconds: int) -> None:
        self.set(key, json.dumps(value, separators=(",", ":"), sort_keys=True), ttl_seconds)


cache = MetadataCache()
