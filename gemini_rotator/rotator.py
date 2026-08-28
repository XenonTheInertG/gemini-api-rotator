"""
gemini_rotator.rotator — core key rotation engine.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, List, Optional

from gemini_rotator.adapters.memory import MemoryAdapter
from gemini_rotator.middleware import RotatorMiddleware
from gemini_rotator.models import (
    ErrorType,
    KeyState,
    KeyStatus,
    RotatorConfig,
    classify_error,
    mask_key,
)

logger = logging.getLogger(__name__)


class GeminiAPIRotator(RotatorMiddleware):
    """
    Production-grade Gemini API key rotator.

    Parameters
    ----------
    keys : list[str]
        One or more Gemini API keys. Duplicates are removed.
    config : RotatorConfig | None
        Full configuration object. Pass None to use defaults.
    db : DBAdapter | None
        Persistence adapter. Defaults to in-memory (no persistence).
    on_key_suspended : callable | None
        Optional hook called with (key: str) whenever a key is suspended.
        Useful for Slack/email alerts.
    on_key_recovered : callable | None
        Optional hook called with (key: str) when a suspended key recovers.
    """

    def __init__(
        self,
        keys: List[str],
        *,
        config:            Optional[RotatorConfig]  = None,
        db                 = None,
        on_key_suspended:  Optional[Callable[[str], None]] = None,
        on_key_recovered:  Optional[Callable[[str], None]] = None,
    ) -> None:
        if not keys:
            raise ValueError("At least one API key is required.")

        self.config = config or RotatorConfig()
        self._db    = db if db is not None else MemoryAdapter()

        self._keys: List[str]  = list(dict.fromkeys(keys))   # deduplicate, preserve order
        self._lock             = threading.RLock()
        self._last_index: int  = -1

        # in-memory state (fast path, no DB round-trip on every call)
        self._cooling:             dict[str, float] = {}  # key → expiry timestamp
        self._suspended:           set[str]         = set()
        self._consecutive_limits:  dict[str, int]   = {}
        self._call_times:          dict[str, list]  = {}  # key → [timestamp, ...]
        self._inflight:            dict[str, int]   = {}  # key → current concurrent count

        # hooks
        self._on_suspended = on_key_suspended
        self._on_recovered = on_key_recovered

        # load persisted suspension state
        try:
            self._suspended = self._db.get_suspended_keys()
            if self._suspended:
                logger.info("[Rotator] %d suspended key(s) loaded from DB.", len(self._suspended))
        except Exception as exc:
            logger.warning("[Rotator] Could not load suspended keys: %s", exc)

        active = len([k for k in self._keys if k not in self._suspended])
        logger.info(
            "[Rotator] Ready — %d key(s) (%d active, %d suspended) | "
            "cooldown=%ds max=%ds | rpm_limit=%d | max_concurrent=%d",
            len(self._keys), active, len(self._suspended),
            self.config.cooldown_base_seconds, self.config.cooldown_max_seconds,
            self.config.rpm_per_key, self.config.max_concurrent_per_key,
        )

    # ── Key selection ─────────────────────────────────────────────────────────

    def get_next_working_key(self) -> Optional[str]:
        """
        Return the next ready API key (round-robin).

        Skips:  suspended keys, keys still in cooldown, keys at RPM ceiling,
                keys at max concurrency.

        Falls back gracefully when fully loaded:
          1. If some keys are only at their concurrency limit → return one anyway.
          2. If some keys are only at their RPM ceiling       → return one anyway.
          3. If every key is cooling / suspended              → return soonest-cooling key.
          4. All suspended                                    → return None.
        """
        with self._lock:
            return self._pick()

    def _pick(self) -> Optional[str]:
        now = time.time()
        n   = len(self._keys)
        if n == 0:
            return None

        cfg            = self.config
        at_concurrency: list[str] = []
        at_rpm:         list[str] = []

        for offset in range(1, n + 1):
            idx = (self._last_index + offset) % n
            key = self._keys[idx]

            if key in self._suspended:
                continue
            if now < self._cooling.get(key, 0):
                continue
            if self._inflight.get(key, 0) >= cfg.max_concurrent_per_key:
                at_concurrency.append(key)
                continue
            if self._calls_in_window(key, now) >= cfg.rpm_per_key:
                at_rpm.append(key)
                continue

            self._last_index = idx
            return key

        # Graduated fallback
        for candidates in (at_concurrency, at_rpm):
            if candidates:
                key = candidates[0]
                self._last_index = self._keys.index(key)
                return key

        available = [k for k in self._keys if k not in self._suspended]
        if not available:
            return None
        # All cooling — return soonest-recovering
        return min(available, key=lambda k: self._cooling.get(k, 0))

    # ── RPM tracking ──────────────────────────────────────────────────────────

    def _calls_in_window(self, key: str, now: float) -> int:
        window = self.config.rpm_window_seconds
        times  = [t for t in self._call_times.get(key, []) if now - t < window]
        self._call_times[key] = times
        return len(times)

    def _record_call_time(self, key: str) -> None:
        now    = time.time()
        window = self.config.rpm_window_seconds
        bucket = self._call_times.setdefault(key, [])
        bucket.append(now)
        self._call_times[key] = [t for t in bucket if now - t < window]

    # ── Concurrency tracking ──────────────────────────────────────────────────

    def acquire(self, key: str) -> None:
        """Mark one in-flight request for `key`. Call before making the API call."""
        with self._lock:
            self._inflight[key] = self._inflight.get(key, 0) + 1

    def release(self, key: str) -> None:
        """Mark one request completed for `key`. Call in a finally block."""
        with self._lock:
            cur = self._inflight.get(key, 0)
            self._inflight[key] = max(0, cur - 1)

    # ── Outcome reporting ─────────────────────────────────────────────────────

    def record_success(self, key: str, latency: Optional[float] = None, model: Optional[str] = None) -> None:
        """Call after a successful API request."""
        with self._lock:
            self._consecutive_limits[key] = 0
            self._record_call_time(key)
        self._db.record_request(key, "success", latency=latency, model=model)

    def mark_rate_limited(self, key: str, model: Optional[str] = None) -> None:
        """Call on 429 RESOURCE_EXHAUSTED."""
        with self._lock:
            hits     = self._consecutive_limits.get(key, 0) + 1
            self._consecutive_limits[key] = hits
            cooldown = min(
                self.config.cooldown_base_seconds * (2 ** (hits - 1)),
                self.config.cooldown_max_seconds,
            )
            self._cooling[key] = time.time() + cooldown
            logger.warning(
                "[Rotator] %s rate-limited (hit #%d) — cooling %ds.",
                mask_key(key), hits, cooldown,
            )
        self._db.record_request(key, "rate_limit", model=model)

    def mark_suspended(self, key: str, model: Optional[str] = None) -> None:
        """Call on 403 CONSUMER_SUSPENDED / PERMISSION_DENIED."""
        with self._lock:
            self._suspended.add(key)
            self._consecutive_limits.pop(key, None)
            remaining = len([k for k in self._keys if k not in self._suspended])
            logger.error(
                "[Rotator] %s SUSPENDED. %d key(s) remaining.",
                mask_key(key), remaining,
            )
        self._db.mark_key_suspended(key)
        self._db.record_request(key, "suspended", model=model)
        if self._on_suspended:
            try:
                self._on_suspended(key)
            except Exception:
                pass

    def record_error(self, key: str, latency: Optional[float] = None, model: Optional[str] = None) -> None:
        """Call on transient errors (500, 503, network timeouts)."""
        self._record_call_time(key)
        self._db.record_request(key, "transient", latency=latency, model=model)

    def record_outcome(
        self,
        key:     str,
        exc:     Optional[Exception] = None,
        latency: Optional[float]     = None,
        model:   Optional[str]       = None,
    ) -> None:
        """
        Convenience: classify `exc` and call the right record_* method.
        Pass exc=None to record a success.
        """
        if exc is None:
            self.record_success(key, latency=latency, model=model)
            return

        etype = classify_error(exc)
        if etype == ErrorType.RATE_LIMIT:
            self.mark_rate_limited(key, model=model)
        elif etype == ErrorType.SUSPENDED:
            self.mark_suspended(key, model=model)
        else:
            self.record_error(key, latency=latency, model=model)

    # ── Suspended-key auto-recheck ────────────────────────────────────────────

    async def revalidate_suspended_keys(self) -> int:
        """
        Probe every currently-suspended key in parallel.
        Returns the number of keys that recovered.
        """
        with self._lock:
            snapshot = list(self._suspended)

        if not snapshot:
            return 0

        logger.info("[Rotator] Auto-recheck: probing %d suspended key(s)…", len(snapshot))
        loop    = asyncio.get_running_loop()
        results = await asyncio.gather(
            *(loop.run_in_executor(None, self._probe_key, k) for k in snapshot),
            return_exceptions=True,
        )

        recovered = 0
        for key, ok in zip(snapshot, results):
            if ok is True:
                with self._lock:
                    self._suspended.discard(key)
                    self._cooling.pop(key, None)
                    self._consecutive_limits[key] = 0
                self._db.unmark_key_suspended(key)
                recovered += 1
                logger.info("[Rotator] %s RECOVERED — rejoining rotation.", mask_key(key))
                if self._on_recovered:
                    try:
                        self._on_recovered(key)
                    except Exception:
                        pass

        logger.info("[Rotator] Recheck complete: %d/%d recovered.", recovered, len(snapshot))
        return recovered

    def _probe_key(self, key: str) -> bool:
        try:
            import google.genai as genai
            from google.genai import types as genai_types

            cfg      = self.config
            client   = genai.Client(api_key=key)
            response = client.models.generate_content(
                model    = cfg.recheck_model,
                contents = [cfg.recheck_prompt],
                config   = genai_types.GenerateContentConfig(max_output_tokens=cfg.recheck_max_tokens),
            )
            return response is not None
        except Exception as exc:
            etype = classify_error(exc)
            if etype in (ErrorType.SUSPENDED, ErrorType.UNKNOWN):
                return False
            # transient error during probe — leave suspended, retry next cycle
            logger.debug("[Rotator] Probe for %s inconclusive: %s", mask_key(key), exc)
            return False

    # ── Observability ─────────────────────────────────────────────────────────

    def status(self) -> list[KeyStatus]:
        """Full per-key status snapshot."""
        now  = time.time()
        out  = []
        with self._lock:
            keys_snapshot = list(self._keys)

        for key in keys_snapshot:
            with self._lock:
                if key in self._suspended:
                    state = KeyState.SUSPENDED
                elif now < self._cooling.get(key, 0):
                    state = KeyState.COOLING
                else:
                    state = KeyState.ACTIVE
                cd_remaining = max(0.0, self._cooling.get(key, 0) - now)
                consec       = self._consecutive_limits.get(key, 0)
                rpm_used     = self._calls_in_window(key, now)

            try:
                stats = self._db.get_stats(key)
            except Exception:
                from gemini_rotator.models import KeyStats
                stats = KeyStats(key=key)

            out.append(KeyStatus(
                masked             = mask_key(key),
                state              = state,
                cooldown_remaining = cd_remaining,
                consecutive_limits = consec,
                rpm_used           = rpm_used,
                rpm_limit          = self.config.rpm_per_key,
                stats              = stats,
            ))
        return out

    def summary(self) -> str:
        now = time.time()
        with self._lock:
            total     = len(self._keys)
            suspended = len(self._suspended)
            cooling   = sum(
                1 for k in self._keys
                if k not in self._suspended and now < self._cooling.get(k, 0)
            )
        active = total - suspended - cooling
        return (
            f"Keys: {total} total | {active} active | "
            f"{cooling} cooling | {suspended} suspended"
        )

    def export_stats(self) -> list[dict]:
        """Return all key stats as a list of dicts (JSON-serialisable)."""
        return [s.to_dict() for s in self._db.get_all_stats()]

    # ── Key management ────────────────────────────────────────────────────────

    def add_key(self, key: str) -> bool:
        """Add a key at runtime. Returns False if it already exists."""
        with self._lock:
            if key in self._keys:
                return False
            self._keys.append(key)
        logger.info("[Rotator] Key added. Total: %d.", len(self._keys))
        return True

    def remove_key(self, key: str) -> bool:
        """Remove a key at runtime. Raises ValueError if it is the last key."""
        with self._lock:
            if key not in self._keys:
                return False
            if len(self._keys) == 1:
                raise ValueError("Cannot remove the last API key.")
            self._keys.remove(key)
            self._suspended.discard(key)
            self._cooling.pop(key, None)
            self._consecutive_limits.pop(key, None)
            self._call_times.pop(key, None)
            self._inflight.pop(key, None)
            if self._last_index >= len(self._keys):
                self._last_index = -1
        logger.info("[Rotator] Key removed. Total: %d.", len(self._keys))
        return True

    def reset_cooldowns(self) -> None:
        with self._lock:
            self._cooling.clear()
            self._consecutive_limits.clear()
        logger.info("[Rotator] All cooldowns cleared.")

    def get_active_count(self) -> int:
        now = time.time()
        with self._lock:
            return sum(
                1 for k in self._keys
                if k not in self._suspended and now >= self._cooling.get(k, 0)
            )

    def total_count(self) -> int:
        return len(self._keys)

    def __repr__(self) -> str:
        return (
            f"<GeminiAPIRotator total={len(self._keys)} "
            f"active={self.get_active_count()} "
            f"rpm_limit={self.config.rpm_per_key}>"
        )
