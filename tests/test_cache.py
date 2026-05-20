"""Tests for the TTL cache."""

from __future__ import annotations

import threading
import time

from nbu_exporter.cache import TTLCache


def test_caches_within_ttl() -> None:
    calls = {"n": 0}

    def fetch() -> int:
        calls["n"] += 1
        return calls["n"]

    cache: TTLCache[int] = TTLCache(ttl_seconds=60.0, fetcher=fetch)
    assert cache.get() == 1
    assert cache.get() == 1
    assert calls["n"] == 1


def test_refetches_after_ttl() -> None:
    calls = {"n": 0}

    def fetch() -> int:
        calls["n"] += 1
        return calls["n"]

    cache: TTLCache[int] = TTLCache(ttl_seconds=0.05, fetcher=fetch)
    assert cache.get() == 1
    time.sleep(0.1)
    assert cache.get() == 2


def test_invalidate() -> None:
    calls = {"n": 0}

    def fetch() -> int:
        calls["n"] += 1
        return calls["n"]

    cache: TTLCache[int] = TTLCache(ttl_seconds=60.0, fetcher=fetch)
    cache.get()
    cache.invalidate()
    cache.get()
    assert calls["n"] == 2


def test_concurrent_callers_single_fetch() -> None:
    calls = {"n": 0}
    started = threading.Event()

    def fetch() -> int:
        started.wait()
        calls["n"] += 1
        return calls["n"]

    cache: TTLCache[int] = TTLCache(ttl_seconds=60.0, fetcher=fetch)
    results: list[int] = []

    def caller() -> None:
        results.append(cache.get())

    threads = [threading.Thread(target=caller) for _ in range(10)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    started.set()
    for t in threads:
        t.join()

    assert calls["n"] == 1
    assert all(r == 1 for r in results)
