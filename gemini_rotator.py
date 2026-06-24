"""
gemini_rotator.py — Gemini API key rotation with cooldown-based rate-limit handling.

Strategy:
  - A key that hits rate limits is marked with a timestamp and skipped for
    COOLDOWN_SECONDS. It is NOT permanently blacklisted.
  - get_next_working_key() always returns a key not currently in cooldown.
  - Per-key request counters are available for debugging/observability.

Keys come from two sources (merged, deduplicated at runtime):
  1. Constructor argument  →  GeminiAPIRotator(["key1", "key2"])
  2. Hot-swap methods      →  add_key(), remove_key(), reload_from_db()
"""

import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 60


class AllKeysExhausted(Exception):
    """Raised when every key is in cooldown and no fallback is possible."""


class GeminiAPIRotator:
    """
    Round-robin Gemini API key rotator with per-key cooldown on rate limits.

    Parameters
    ----------
    api_keys : list[str]
        One or more Gemini API keys.
    cooldown_seconds : int
        How long (in seconds) a failed key sits out before being retried.
        Default: 60.
    """

    def __init__(
        self,
        api_keys: List[str],
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        if not api_keys:
            raise ValueError("At least one Gemini API key is required.")

        self.cooldown_seconds: int = cooldown_seconds
        self.keys: List[str] = list(dict.fromkeys(api_keys))  # deduplicate, preserve order
        self._index: int = 0
        # unix timestamp until which a key is in cooldown; 0.0 = available
        self._cooldown_until: Dict[str, float] = {k: 0.0 for k in self.keys}
        self._request_count: Dict[str, int] = {k: 0 for k in self.keys}

        logger.info(
            "GeminiAPIRotator ready — %d key(s), %ds cooldown on rate-limit.",
            len(self.keys),
            self.cooldown_seconds,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Core rotation
    # ──────────────────────────────────────────────────────────────────────

    def get_current_key(self) -> str:
        """Return the key at the current index without advancing."""
        return self.keys[self._index]

    def get_next_working_key(self) -> str:
        """
        Return the next available (not in cooldown) key and advance the index.

        If every key is currently in cooldown, returns the one with the
        shortest remaining wait time instead of raising — callers can choose
        to wait or proceed at their own risk.

        Returns
        -------
        str
            An API key to use for the next request.
        """
        for _ in range(len(self.keys)):
            key = self.keys[self._index]
            if self._is_available(key):
                self._request_count[key] += 1
                self._rotate()
                return key
            self._rotate()

        # All keys in cooldown — hand back the soonest-recovering one
        soonest = min(self.keys, key=lambda k: self._cooldown_until.get(k, 0.0))
        wait = max(0.0, self._cooldown_until[soonest] - time.time())
        logger.warning(
            "All %d key(s) in cooldown. Soonest recovers in %.0fs.",
            len(self.keys),
            wait,
        )
        self._request_count[soonest] += 1
        return soonest

    def mark_rate_limited(self, key: str) -> None:
        """
        Call this when a request with `key` returns a rate-limit / quota error.
        The key will be skipped for `cooldown_seconds` seconds.
        """
        if key not in self._cooldown_until:
            return
        self._cooldown_until[key] = time.time() + self.cooldown_seconds
        logger.warning(
            "Key %s rate-limited — cooling down for %ds.",
            _mask(key),
            self.cooldown_seconds,
        )
        self._rotate()

    # Alias kept for compatibility with older internal usage
    mark_failed = mark_rate_limited

    def reset_cooldowns(self) -> None:
        """Clear all cooldowns immediately (useful in tests or manual recovery)."""
        for k in self.keys:
            self._cooldown_until[k] = 0.0
        logger.info("All cooldowns cleared.")

    # ──────────────────────────────────────────────────────────────────────
    # Observability
    # ──────────────────────────────────────────────────────────────────────

    def available_count(self) -> int:
        """Number of keys not currently in cooldown."""
        return sum(1 for k in self.keys if self._is_available(k))

    def total_count(self) -> int:
        """Total number of managed keys."""
        return len(self.keys)

    def status(self) -> List[dict]:
        """
        Return a list of dicts describing each key's current state.

        Each dict contains:
            masked   — first 4 + last 4 chars, rest replaced by "..."
            available — bool
            cooldown_remaining — seconds left (0 if available)
            requests — total requests served by this key
        """
        now = time.time()
        return [
            {
                "masked": _mask(k),
                "available": self._is_available(k),
                "cooldown_remaining": max(0.0, self._cooldown_until.get(k, 0.0) - now),
                "requests": self._request_count.get(k, 0),
            }
            for k in self.keys
        ]

    def __repr__(self) -> str:
        return (
            f"<GeminiAPIRotator keys={len(self.keys)} "
            f"available={self.available_count()} "
            f"cooldown={self.cooldown_seconds}s>"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Hot-swap
    # ──────────────────────────────────────────────────────────────────────

    def add_key(self, key: str) -> bool:
        """
        Add a new key at runtime.

        Returns True if added, False if the key already exists.
        """
        if key in self.keys:
            return False
        self.keys.append(key)
        self._cooldown_until[key] = 0.0
        self._request_count[key] = 0
        logger.info("Key added. Total: %d.", len(self.keys))
        return True

    def remove_key(self, key: str) -> bool:
        """
        Remove a key at runtime.

        Returns True if removed, False if not found.
        Raises ValueError if this is the last key.
        """
        if key not in self.keys:
            return False
        if len(self.keys) == 1:
            raise ValueError("Cannot remove the last API key.")

        idx = self.keys.index(key)
        self.keys.remove(key)
        self._cooldown_until.pop(key, None)
        self._request_count.pop(key, None)

        # Keep _index valid
        if self._index >= len(self.keys):
            self._index = 0
        elif self._index > idx:
            self._index -= 1

        logger.info("Key removed. Total: %d.", len(self.keys))
        return True

    def sync_keys(self, new_keys: List[str]) -> None:
        """
        Sync the key list to `new_keys` (e.g. after re-reading from a database).

        Keys in `new_keys` but not currently managed are added.
        Keys currently managed but absent from `new_keys` are removed.
        Existing keys retain their cooldown and request-count state.
        """
        new_set = set(new_keys)
        current_set = set(self.keys)

        for k in new_set - current_set:
            self.keys.append(k)
            self._cooldown_until[k] = 0.0
            self._request_count[k] = 0

        removed = current_set - new_set
        self.keys = [k for k in self.keys if k not in removed]
        for k in removed:
            self._cooldown_until.pop(k, None)
            self._request_count.pop(k, None)

        if not self.keys:
            raise ValueError("No API keys remaining after sync.")

        self._index = min(self._index, len(self.keys) - 1)
        logger.info("Keys synced. Total: %d.", len(self.keys))

    # Alias for users coming from the old internal API
    reload_from_db = sync_keys

    # ──────────────────────────────────────────────────────────────────────
    # Internals
    # ──────────────────────────────────────────────────────────────────────

    def _rotate(self) -> None:
        self._index = (self._index + 1) % len(self.keys)

    def _is_available(self, key: str) -> bool:
        return time.time() >= self._cooldown_until.get(key, 0.0)


def _mask(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-4:]}"
