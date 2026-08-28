"""
gemini_rotator.adapters.base — DBAdapter protocol.

Any object satisfying this protocol can be passed as `db=` to GeminiAPIRotator.
"""

from __future__ import annotations

from typing import Optional, Protocol, Set, runtime_checkable

from gemini_rotator.models import KeyStats


@runtime_checkable
class DBAdapter(Protocol):
    """
    Persistence interface for GeminiAPIRotator.

    Implement this to plug in any storage backend (MongoDB, PostgreSQL,
    Redis, DynamoDB, …). A SQLite and an in-memory implementation are
    included out of the box.
    """

    def get_suspended_keys(self) -> Set[str]:
        """Return the set of currently suspended key strings."""
        ...

    def mark_key_suspended(self, key: str) -> None:
        """Persist that `key` is suspended."""
        ...

    def unmark_key_suspended(self, key: str) -> None:
        """Remove `key` from the suspended set."""
        ...

    def record_request(
        self,
        key:       str,
        event:     str,            # "success" | "rate_limit" | "suspended" | "transient" | "error"
        latency:   Optional[float] = None,   # seconds; None for error paths where latency is unknown
        model:     Optional[str]   = None,
    ) -> None:
        """
        Record one completed (or failed) request.

        Called after every API call — success or failure. Implementations
        should update counters and latency stats atomically where possible.
        """
        ...

    def get_stats(self, key: str) -> KeyStats:
        """Return cumulative stats for a single key."""
        ...

    def get_all_stats(self) -> list[KeyStats]:
        """Return stats for every known key."""
        ...

    def reset_stats(self, key: Optional[str] = None) -> None:
        """
        Reset stats. If `key` is given, reset only that key.
        If `key` is None, reset all keys.
        """
        ...
