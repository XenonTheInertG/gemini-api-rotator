"""
gemini_rotator.models — shared dataclasses, enums, and config.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class KeyState(str, Enum):
    ACTIVE    = "active"
    COOLING   = "cooling"
    SUSPENDED = "suspended"


class ErrorType(str, Enum):
    RATE_LIMIT = "rate_limit"   # 429 RESOURCE_EXHAUSTED
    SUSPENDED  = "suspended"    # 403 CONSUMER_SUSPENDED / PERMISSION_DENIED
    TRANSIENT  = "transient"    # 500 / 503 / network
    UNKNOWN    = "unknown"


@dataclass
class KeyStats:
    """Cumulative stats for a single API key."""
    key:               str
    total_requests:    int   = 0
    total_success:     int   = 0
    total_rate_limit:  int   = 0
    total_suspended:   int   = 0
    total_transient:   int   = 0
    total_error:       int   = 0
    # latency in seconds
    latency_sum:       float = 0.0
    latency_min:       float = float("inf")
    latency_max:       float = 0.0
    latency_count:     int   = 0
    last_used_at:      float = 0.0   # unix timestamp

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_success / self.total_requests

    @property
    def avg_latency(self) -> Optional[float]:
        if self.latency_count == 0:
            return None
        return self.latency_sum / self.latency_count

    def to_dict(self) -> dict:
        return {
            "key":             self.key,
            "total_requests":  self.total_requests,
            "total_success":   self.total_success,
            "total_rate_limit":self.total_rate_limit,
            "total_suspended": self.total_suspended,
            "total_transient": self.total_transient,
            "total_error":     self.total_error,
            "success_rate":    round(self.success_rate, 4),
            "avg_latency_ms":  round(self.avg_latency * 1000, 2) if self.avg_latency else None,
            "min_latency_ms":  round(self.latency_min * 1000, 2) if self.latency_count else None,
            "max_latency_ms":  round(self.latency_max * 1000, 2) if self.latency_count else None,
            "last_used_at":    self.last_used_at,
        }


@dataclass
class KeyStatus:
    """Point-in-time snapshot of a single key — returned by rotator.status()."""
    masked:             str
    state:              KeyState
    cooldown_remaining: float        = 0.0
    consecutive_limits: int          = 0
    rpm_used:           int          = 0
    rpm_limit:          int          = 15
    stats:              KeyStats     = field(default_factory=lambda: KeyStats(key=""))

    def to_dict(self) -> dict:
        return {
            "masked":             self.masked,
            "state":              self.state.value,
            "cooldown_remaining": round(self.cooldown_remaining, 1),
            "consecutive_limits": self.consecutive_limits,
            "rpm_used":           self.rpm_used,
            "rpm_limit":          self.rpm_limit,
            **self.stats.to_dict(),
        }


@dataclass
class RotatorConfig:
    """
    Full configuration for GeminiAPIRotator.

    All fields have sensible defaults — only pass what you want to override.
    """
    # Rate-limit handling
    cooldown_base_seconds: int   = 60
    cooldown_max_seconds:  int   = 30 * 60   # 30 min cap on backoff
    rpm_per_key:           int   = 15        # proactive skip threshold

    # Concurrency
    max_concurrent_per_key: int  = 5         # simultaneous in-flight requests per key

    # Retry
    max_retries:           int   = 3
    retry_delay_seconds:   float = 0.5

    # Suspended-key recheck
    recheck_interval_seconds: int = 6 * 60 * 60   # 6 hours
    recheck_model:         str   = "gemini-2.5-flash"
    recheck_prompt:        str   = "ping"
    recheck_max_tokens:    int   = 8

    # Rolling RPM window
    rpm_window_seconds:    int   = 60


def classify_error(exc: Exception) -> ErrorType:
    """
    Map a google-genai (or requests) exception to an ErrorType.
    Works by inspecting the string representation — avoids a hard
    dependency on google-genai's exception hierarchy.
    """
    msg = str(exc).upper()
    if any(m in msg for m in ("CONSUMER_SUSPENDED", "PERMISSION_DENIED", "API_KEY_INVALID", "DISABLED")):
        return ErrorType.SUSPENDED
    if any(m in msg for m in ("429", "RESOURCE_EXHAUSTED", "QUOTA")):
        return ErrorType.RATE_LIMIT
    if any(m in msg for m in ("500", "503", "UNAVAILABLE", "INTERNAL")):
        return ErrorType.TRANSIENT
    return ErrorType.UNKNOWN


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-8:]}"
