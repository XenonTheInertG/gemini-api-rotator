"""
gemini_rotator.adapters.memory — thread-safe in-memory adapter (default).

No persistence — state resets on restart. Use the SQLite adapter if you
need suspension state and stats to survive restarts.
"""

from __future__ import annotations

import threading
import time
from typing import Optional, Set

from gemini_rotator.models import KeyStats


class MemoryAdapter:
    """
    Thread-safe in-memory implementation of DBAdapter.

    All state lives in dicts protected by a single RLock.
    """

    def __init__(self) -> None:
        self._lock      = threading.RLock()
        self._suspended: Set[str]          = set()
        self._stats:     dict[str, KeyStats] = {}

    # ── Suspension ────────────────────────────────────────────────────────

    def get_suspended_keys(self) -> Set[str]:
        with self._lock:
            return set(self._suspended)

    def mark_key_suspended(self, key: str) -> None:
        with self._lock:
            self._suspended.add(key)

    def unmark_key_suspended(self, key: str) -> None:
        with self._lock:
            self._suspended.discard(key)

    # ── Stats ─────────────────────────────────────────────────────────────

    def _ensure(self, key: str) -> KeyStats:
        if key not in self._stats:
            self._stats[key] = KeyStats(key=key)
        return self._stats[key]

    def record_request(
        self,
        key:     str,
        event:   str,
        latency: Optional[float] = None,
        model:   Optional[str]   = None,
    ) -> None:
        with self._lock:
            s = self._ensure(key)
            s.total_requests += 1
            s.last_used_at    = time.time()

            if event == "success":
                s.total_success += 1
            elif event == "rate_limit":
                s.total_rate_limit += 1
            elif event == "suspended":
                s.total_suspended += 1
            elif event == "transient":
                s.total_transient += 1
            else:
                s.total_error += 1

            if latency is not None and latency >= 0:
                s.latency_sum   += latency
                s.latency_count += 1
                s.latency_min    = min(s.latency_min, latency)
                s.latency_max    = max(s.latency_max, latency)

    def get_stats(self, key: str) -> KeyStats:
        with self._lock:
            if key not in self._stats:
                return KeyStats(key=key)
            s = self._stats[key]
            # return a copy so caller can't mutate internal state
            return KeyStats(**s.__dict__)

    def get_all_stats(self) -> list[KeyStats]:
        with self._lock:
            return [KeyStats(**s.__dict__) for s in self._stats.values()]

    def reset_stats(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key:
                self._stats.pop(key, None)
            else:
                self._stats.clear()
