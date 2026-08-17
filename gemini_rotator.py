"""
gemini_rotator.py — Gemini API key rotation with persistent stats.

Key states
----------
active     — available for use
cooling    — rate-limited; skipped for a growing cooldown window
             (in-memory, resets on restart)
suspended  — permanently disabled; stored in the optional DB adapter
             and survives restarts. Auto-retested on a schedule.

Error handling
--------------
403 CONSUMER_SUSPENDED / PERMISSION_DENIED  → mark_suspended()
429 RESOURCE_EXHAUSTED                      → mark_rate_limited()  (exponential backoff)
500 / 503                                   → transient; retry same key

Rate-limit avoidance
--------------------
- Round-robin selection spreads load evenly.
- Per-key RPM tracking: a key that has already hit its call ceiling
  within the rolling 60-second window is proactively skipped, so we
  pre-empt 429s instead of reacting to them.
- Repeated rate-limits grow the cooldown exponentially
  (60s → 120s → 240s … capped at 30 min).
"""

import asyncio
import logging
import time
from typing import List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

COOLDOWN_BASE_SECONDS = 60
COOLDOWN_MAX_SECONDS  = 30 * 60
RPM_WINDOW_SECONDS    = 60
DEFAULT_RPM_PER_KEY   = 15


# ── Optional DB adapter ───────────────────────────────────────────────────────
#
# Supply any object that satisfies DBAdapter when constructing GeminiAPIRotator.
# If you pass nothing, all state is in-memory only (suspended keys reset on
# restart, no persistent stats).

@runtime_checkable
class DBAdapter(Protocol):
    def get_suspended_keys(self) -> set: ...
    def mark_key_suspended(self, key: str) -> None: ...
    def unmark_key_suspended(self, key: str) -> None: ...
    def update_key_stat(self, key: str, event: str) -> None: ...
    def get_all_key_stats(self) -> list: ...


class _NoopDB:
    """Fallback used when no real DB adapter is provided."""
    def get_suspended_keys(self)               -> set:  return set()
    def mark_key_suspended(self, key: str)     -> None: pass
    def unmark_key_suspended(self, key: str)   -> None: pass
    def update_key_stat(self, key, event)      -> None: pass
    def get_all_key_stats(self)                -> list: return []


# ── Rotator ───────────────────────────────────────────────────────────────────

class GeminiAPIRotator:
    """
    Round-robin Gemini API key rotator with per-key cooldown, exponential
    backoff, RPM-ceiling pre-emption, and optional persistent suspension.

    Parameters
    ----------
    keys : list[str]
        One or more Gemini API keys.
    rpm_per_key : int
        Maximum calls per key per 60-second rolling window before it is
        proactively skipped. Default: 15.
    db : DBAdapter | None
        Optional database adapter for persistent suspension state and stats.
        Pass None (default) to use in-memory state only.
    """

    def __init__(
        self,
        keys: List[str],
        *,
        rpm_per_key: int = DEFAULT_RPM_PER_KEY,
        db: Optional[DBAdapter] = None,
    ) -> None:
        if not keys:
            raise ValueError("At least one API key is required.")

        self._keys: List[str]       = list(dict.fromkeys(keys))  # deduplicate
        self._rpm_per_key: int      = rpm_per_key
        self._db: DBAdapter         = db if isinstance(db, DBAdapter) else _NoopDB()
        self._last_index: int       = -1

        # in-memory state
        self._cooling: dict         = {}  # key → expiry timestamp
        self._suspended: set        = set()
        self._consecutive_limits    = {}  # key → int
        self._call_times: dict      = {}  # key → [timestamp, ...]

        # Load persisted suspensions
        try:
            self._suspended = self._db.get_suspended_keys()
            if self._suspended:
                logger.info("[APIRotator] %d suspended key(s) loaded from DB.", len(self._suspended))
        except Exception as exc:
            logger.warning("[APIRotator] Could not load suspended keys: %s", exc)

        active = len([k for k in self._keys if k not in self._suspended])
        logger.info(
            "[APIRotator] %d key(s) loaded (%d active, %d suspended), "
            "%ds base cooldown, %d RPM/key ceiling.",
            len(self._keys), active, len(self._suspended),
            COOLDOWN_BASE_SECONDS, self._rpm_per_key,
        )

    # ── Key selection ─────────────────────────────────────────────────────────

    def get_next_working_key(self) -> Optional[str]:
        """
        Return the next ready key using round-robin selection.

        Skips suspended keys, keys still in cooldown, and keys that have
        already hit their RPM ceiling within the rolling window.

        If no key is fully free, falls back to a key that is merely at its
        RPM ceiling (still better than a cooling key). If every key is
        cooling or suspended, returns the one whose cooldown expires soonest
        so the caller can wait it out. Returns None only when every key is
        suspended.
        """
        now = time.time()
        n   = len(self._keys)
        if n == 0:
            return None

        at_rpm_limit: List[str] = []

        for offset in range(1, n + 1):
            idx = (self._last_index + offset) % n
            key = self._keys[idx]

            if key in self._suspended:
                continue
            if now < self._cooling.get(key, 0):
                continue
            if self._calls_in_window(key, now) >= self._rpm_per_key:
                at_rpm_limit.append(key)
                continue

            self._last_index = idx
            return key

        # Fall back to a key that is only RPM-limited
        if at_rpm_limit:
            key = at_rpm_limit[0]
            self._last_index = self._keys.index(key)
            return key

        # Everything is cooling or suspended — return soonest-recovering key
        available = [k for k in self._keys if k not in self._suspended]
        if not available:
            return None
        return min(available, key=lambda k: self._cooling.get(k, 0))

    # ── Per-key RPM tracking ──────────────────────────────────────────────────

    def _calls_in_window(self, key: str, now: float) -> int:
        cutoff = now - RPM_WINDOW_SECONDS
        times  = [t for t in self._call_times.get(key, []) if t >= cutoff]
        self._call_times[key] = times
        return len(times)

    def _record_call(self, key: str) -> None:
        now    = time.time()
        cutoff = now - RPM_WINDOW_SECONDS
        bucket = self._call_times.setdefault(key, [])
        bucket.append(now)
        self._call_times[key] = [t for t in bucket if t >= cutoff]

    # ── State updates ─────────────────────────────────────────────────────────

    def mark_rate_limited(self, key: str) -> None:
        """
        Call when a request returns 429 RESOURCE_EXHAUSTED.

        Cooldown grows exponentially with consecutive hits:
        60s → 120s → 240s → … capped at 30 minutes.
        """
        hits    = self._consecutive_limits.get(key, 0) + 1
        self._consecutive_limits[key] = hits
        cooldown = min(COOLDOWN_BASE_SECONDS * (2 ** (hits - 1)), COOLDOWN_MAX_SECONDS)
        self._cooling[key] = time.time() + cooldown
        self._db.update_key_stat(key, "rate_limit")
        logger.warning(
            "[APIRotator] Key ...%s rate-limited (hit #%d) — cooling %ds.",
            key[-6:], hits, cooldown,
        )

    def mark_suspended(self, key: str) -> None:
        """
        Call when a request returns 403 CONSUMER_SUSPENDED / PERMISSION_DENIED.

        The key is blacklisted in memory and persisted to the DB.
        revalidate_suspended_keys() will probe it again on schedule.
        """
        self._suspended.add(key)
        self._consecutive_limits.pop(key, None)
        self._db.mark_key_suspended(key)
        remaining = len([k for k in self._keys if k not in self._suspended])
        logger.error(
            "[APIRotator] Key ...%s SUSPENDED. %d key(s) remaining. "
            "Will be auto-rechecked periodically.",
            key[-6:], remaining,
        )

    def record_success(self, key: str) -> None:
        """
        Call after every successful request.

        Resets the consecutive-rate-limit counter, records the call for RPM
        tracking, and increments persistent success stats.
        """
        self._consecutive_limits[key] = 0
        self._record_call(key)
        self._db.update_key_stat(key, "success")

    def record_error(self, key: str) -> None:
        """
        Call on non-rate-limit, non-suspension errors (e.g. 500, 503).

        Records the call for RPM tracking and increments persistent error stats.
        """
        self._record_call(key)
        self._db.update_key_stat(key, "error")

    # ── Suspended-key auto-recheck ────────────────────────────────────────────

    async def revalidate_suspended_keys(self) -> None:
        """
        Fire a cheap, parallel probe against every currently-suspended key.

        Any key that responds successfully is unsuspended and rejoins rotation
        immediately. Designed to be called on a schedule (e.g. every 6 hours)
        — no user-facing command needed.
        """
        snapshot = list(self._suspended)
        if not snapshot:
            return

        logger.info("[APIRotator] Auto-recheck: testing %d suspended key(s)…", len(snapshot))

        loop    = asyncio.get_running_loop()
        results = await asyncio.gather(
            *(loop.run_in_executor(None, self._probe_key, k) for k in snapshot),
            return_exceptions=True,
        )

        recovered = 0
        for key, ok in zip(snapshot, results):
            if ok is True:
                self._suspended.discard(key)
                self._cooling.pop(key, None)
                self._consecutive_limits[key] = 0
                self._db.unmark_key_suspended(key)
                recovered += 1
                logger.info("[APIRotator] Key ...%s RECOVERED — rejoining rotation.", key[-6:])

        logger.info(
            "[APIRotator] Auto-recheck complete: %d/%d key(s) recovered.",
            recovered, len(snapshot),
        )

    @staticmethod
    def _probe_key(key: str) -> bool:
        """
        Minimal Gemini call used only to check whether a suspended key works again.
        Returns True if the key is usable, False otherwise.
        """
        try:
            import google.genai as genai
            from google.genai import types as genai_types

            client   = genai.Client(api_key=key)
            config   = genai_types.GenerateContentConfig(max_output_tokens=8)
            response = client.models.generate_content(
                model    = "gemini-2.5-flash",
                contents = ["ping"],
                config   = config,
            )
            return response is not None
        except Exception as exc:
            msg = str(exc)
            if any(m in msg for m in (
                "CONSUMER_SUSPENDED", "PERMISSION_DENIED",
                "API_KEY_INVALID", "suspended", "disabled",
            )):
                return False
            logger.debug("[APIRotator] Recheck for ...%s inconclusive: %s", key[-6:], exc)
            return False

    # ── Observability ─────────────────────────────────────────────────────────

    def status(self) -> List[dict]:
        """
        Return a list of dicts describing each key's current state.

        Each dict contains:
            masked            — last 8 chars, prefixed with "..."
            state             — "active" | "cooling" | "suspended"
            cooldown_remaining — seconds left in cooldown (0 if not cooling)
            rpm_used          — calls made in the current 60s window
            rpm_limit         — per-key RPM ceiling
            stats             — dict with success/rate_limit/error counts from DB
        """
        now       = time.time()
        stats_map = {}
        try:
            stats_map = {d["key"]: d for d in self._db.get_all_key_stats()}
        except Exception:
            pass

        out = []
        for key in self._keys:
            if key in self._suspended:
                state = "suspended"
            elif now < self._cooling.get(key, 0):
                state = "cooling"
            else:
                state = "active"

            s = stats_map.get(key, {})
            out.append({
                "masked":             f"...{key[-8:]}",
                "state":              state,
                "cooldown_remaining": max(0.0, self._cooling.get(key, 0) - now),
                "rpm_used":           self._calls_in_window(key, now),
                "rpm_limit":          self._rpm_per_key,
                "stats": {
                    "success":    s.get("total_success", 0),
                    "rate_limit": s.get("total_rate_limit", 0),
                    "error":      s.get("total_error", 0),
                },
            })
        return out

    def summary(self) -> str:
        """One-line summary — useful for health checks and log lines."""
        now       = time.time()
        suspended = len(self._suspended)
        cooling   = sum(
            1 for k in self._keys
            if k not in self._suspended and now < self._cooling.get(k, 0)
        )
        active = len(self._keys) - suspended - cooling
        return (
            f"Keys: {len(self._keys)} total | "
            f"{active} active | {cooling} cooling | {suspended} suspended"
        )

    def masked_list(self) -> List[str]:
        """
        Human-readable per-key status lines suitable for a /stats command or
        admin panel.
        """
        now    = time.time()
        lines  = []
        stats_map = {}
        try:
            stats_map = {d["key"]: d for d in self._db.get_all_key_stats()}
        except Exception:
            pass

        for key in self._keys:
            s      = stats_map.get(key, {})
            ok     = s.get("total_success", 0)
            rl     = s.get("total_rate_limit", 0)
            errs   = s.get("total_error", 0)
            masked = f"...{key[-8:]}"

            if key in self._suspended:
                lines.append(f"❌ {masked}  SUSPENDED  ✓{ok}  ⚠{rl}  err:{errs}")
            elif now < self._cooling.get(key, 0):
                secs = int(self._cooling[key] - now)
                lines.append(f"⏳ {masked}  cooling {secs}s  ✓{ok}  ⚠{rl}  err:{errs}")
            else:
                rpm = self._calls_in_window(key, now)
                lines.append(
                    f"✅ {masked}  active  ✓{ok}  ⚠{rl}  err:{errs}  "
                    f"rpm:{rpm}/{self._rpm_per_key}"
                )
        return lines

    # ── Key management ────────────────────────────────────────────────────────

    def add_key(self, key: str) -> bool:
        """Add a key at runtime. Returns False if it already exists."""
        if key in self._keys:
            return False
        self._keys.append(key)
        logger.info("[APIRotator] Key added. Total: %d.", len(self._keys))
        return True

    def remove_key(self, key: str) -> bool:
        """
        Remove a key at runtime. Returns False if not found.
        Raises ValueError if it is the last key.
        """
        if key not in self._keys:
            return False
        if len(self._keys) == 1:
            raise ValueError("Cannot remove the last API key.")
        self._keys.remove(key)
        self._suspended.discard(key)
        self._cooling.pop(key, None)
        self._consecutive_limits.pop(key, None)
        self._call_times.pop(key, None)
        if self._last_index >= len(self._keys):
            self._last_index = -1
        logger.info("[APIRotator] Key removed. Total: %d.", len(self._keys))
        return True

    def get_active_count(self) -> int:
        """Number of keys that are neither suspended nor cooling."""
        now = time.time()
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
            f"rpm_limit={self._rpm_per_key}>"
        )
