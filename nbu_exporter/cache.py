"""Thread-safe TTL cache with single-flight semantics."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """In-memory TTL cache that serializes fetches under a lock."""

    def __init__(self, ttl_seconds: float, fetcher: Callable[[], T]) -> None:
        self._ttl = ttl_seconds
        self._fetcher = fetcher
        self._value: T | None = None
        self._fetched_at: float = 0.0
        self._lock = threading.Lock()

    def get(self) -> T:
        """Return the cached value, refreshing it if older than TTL."""
        with self._lock:
            now = time.monotonic()
            if self._value is None or (now - self._fetched_at) >= self._ttl:
                self._value = self._fetcher()
                self._fetched_at = now
            return self._value

    def invalidate(self) -> None:
        """Drop any cached value so the next get() refetches."""
        with self._lock:
            self._value = None
            self._fetched_at = 0.0
