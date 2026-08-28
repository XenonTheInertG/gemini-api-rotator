"""
gemini_rotator.adapters.sqlite — persistent SQLite adapter.

Uses Python's built-in sqlite3 — no extra dependencies.
Stores per-key stats including per-call latency rows for full
response-time analysis. Suspension state survives restarts.

Schema
------
suspended_keys  — one row per suspended key
key_stats       — one row per key, cumulative counters + latency aggregates
request_log     — one row per request (key, event, latency_ms, model, ts)
                  Kept for at most `log_max_rows` rows (default 100 000).
                  Set log_max_rows=0 to disable per-request logging.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, Set

from gemini_rotator.models import KeyStats


_CREATE_SUSPENDED = """
CREATE TABLE IF NOT EXISTS suspended_keys (
    key        TEXT PRIMARY KEY,
    suspended_at REAL NOT NULL
)
"""

_CREATE_KEY_STATS = """
CREATE TABLE IF NOT EXISTS key_stats (
    key             TEXT PRIMARY KEY,
    total_requests  INTEGER NOT NULL DEFAULT 0,
    total_success   INTEGER NOT NULL DEFAULT 0,
    total_rate_limit INTEGER NOT NULL DEFAULT 0,
    total_suspended INTEGER NOT NULL DEFAULT 0,
    total_transient INTEGER NOT NULL DEFAULT 0,
    total_error     INTEGER NOT NULL DEFAULT 0,
    latency_sum     REAL    NOT NULL DEFAULT 0.0,
    latency_min     REAL,
    latency_max     REAL,
    latency_count   INTEGER NOT NULL DEFAULT 0,
    last_used_at    REAL    NOT NULL DEFAULT 0.0
)
"""

_CREATE_REQUEST_LOG = """
CREATE TABLE IF NOT EXISTS request_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT    NOT NULL,
    event      TEXT    NOT NULL,
    latency_ms REAL,
    model      TEXT,
    ts         REAL    NOT NULL
)
"""

_CREATE_IDX_LOG_KEY = "CREATE INDEX IF NOT EXISTS idx_log_key ON request_log(key)"
_CREATE_IDX_LOG_TS  = "CREATE INDEX IF NOT EXISTS idx_log_ts  ON request_log(ts)"


class SQLiteAdapter:
    """
    Persistent SQLite adapter for GeminiAPIRotator.

    Parameters
    ----------
    path : str | Path
        Path to the SQLite database file.
        Pass ``":memory:"`` for an in-memory database (no persistence).
    log_max_rows : int
        Maximum rows kept in request_log. Older rows are pruned automatically
        after every 500 inserts. Default: 100 000. Set 0 to disable logging.
    """

    def __init__(self, path: str | Path = "gemini_rotator.db", log_max_rows: int = 100_000) -> None:
        self._path         = str(path)
        self._log_max_rows = log_max_rows
        self._lock         = threading.RLock()
        self._insert_count = 0
        self._PRUNE_EVERY  = 500

        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(_CREATE_SUSPENDED)
            self._conn.execute(_CREATE_KEY_STATS)
            if self._log_max_rows > 0:
                self._conn.execute(_CREATE_REQUEST_LOG)
                self._conn.execute(_CREATE_IDX_LOG_KEY)
                self._conn.execute(_CREATE_IDX_LOG_TS)

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── Suspension ────────────────────────────────────────────────────────

    def get_suspended_keys(self) -> Set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT key FROM suspended_keys").fetchall()
            return {r[0] for r in rows}

    def mark_key_suspended(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO suspended_keys(key, suspended_at) VALUES (?, ?)",
                (key, time.time()),
            )

    def unmark_key_suspended(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM suspended_keys WHERE key = ?", (key,))

    # ── Stats ─────────────────────────────────────────────────────────────

    def record_request(
        self,
        key:     str,
        event:   str,
        latency: Optional[float] = None,
        model:   Optional[str]   = None,
    ) -> None:
        now        = time.time()
        lat_ms     = latency * 1000 if latency is not None and latency >= 0 else None

        # column to increment
        col_map = {
            "success":    "total_success",
            "rate_limit": "total_rate_limit",
            "suspended":  "total_suspended",
            "transient":  "total_transient",
        }
        event_col = col_map.get(event, "total_error")

        with self._lock, self._conn:
            # Upsert stats row
            if latency is not None and latency >= 0:
                self._conn.execute(f"""
                    INSERT INTO key_stats(key, total_requests, {event_col},
                        latency_sum, latency_min, latency_max, latency_count, last_used_at)
                    VALUES (?, 1, 1, ?, ?, ?, 1, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        total_requests = total_requests + 1,
                        {event_col}    = {event_col} + 1,
                        latency_sum    = latency_sum + excluded.latency_sum,
                        latency_min    = MIN(COALESCE(latency_min, excluded.latency_min), excluded.latency_min),
                        latency_max    = MAX(COALESCE(latency_max, 0), excluded.latency_max),
                        latency_count  = latency_count + 1,
                        last_used_at   = excluded.last_used_at
                """, (key, latency, latency, latency, now))
            else:
                self._conn.execute(f"""
                    INSERT INTO key_stats(key, total_requests, {event_col}, last_used_at)
                    VALUES (?, 1, 1, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        total_requests = total_requests + 1,
                        {event_col}    = {event_col} + 1,
                        last_used_at   = excluded.last_used_at
                """, (key, now))

            # Per-request log
            if self._log_max_rows > 0:
                self._conn.execute(
                    "INSERT INTO request_log(key, event, latency_ms, model, ts) VALUES (?,?,?,?,?)",
                    (key, event, lat_ms, model, now),
                )
                self._insert_count += 1
                if self._insert_count % self._PRUNE_EVERY == 0:
                    self._prune_log()

    def _prune_log(self) -> None:
        """Keep only the most recent `log_max_rows` rows."""
        self._conn.execute("""
            DELETE FROM request_log WHERE id NOT IN (
                SELECT id FROM request_log ORDER BY id DESC LIMIT ?
            )
        """, (self._log_max_rows,))

    def get_stats(self, key: str) -> KeyStats:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM key_stats WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return KeyStats(key=key)
            return self._row_to_stats(row)

    def get_all_stats(self) -> list[KeyStats]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM key_stats").fetchall()
            return [self._row_to_stats(r) for r in rows]

    def reset_stats(self, key: Optional[str] = None) -> None:
        with self._lock, self._conn:
            if key:
                self._conn.execute("DELETE FROM key_stats WHERE key = ?", (key,))
                if self._log_max_rows > 0:
                    self._conn.execute("DELETE FROM request_log WHERE key = ?", (key,))
            else:
                self._conn.execute("DELETE FROM key_stats")
                if self._log_max_rows > 0:
                    self._conn.execute("DELETE FROM request_log")

    def get_latency_history(
        self,
        key:   Optional[str] = None,
        limit: int            = 500,
        model: Optional[str] = None,
    ) -> list[dict]:
        """
        Return recent per-request latency rows from request_log.

        Parameters
        ----------
        key   : filter to a specific key (None = all keys)
        limit : max rows to return (most recent first)
        model : filter by model name
        """
        if self._log_max_rows == 0:
            return []

        clauses = ["event = 'success'", "latency_ms IS NOT NULL"]
        params: list = []

        if key:
            clauses.append("key = ?")
            params.append(key)
        if model:
            clauses.append("model = ?")
            params.append(model)

        where = " AND ".join(clauses)
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(
                f"SELECT key, event, latency_ms, model, ts FROM request_log "
                f"WHERE {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()

        return [
            {"key": r[0], "event": r[1], "latency_ms": r[2], "model": r[3], "ts": r[4]}
            for r in rows
        ]

    @staticmethod
    def _row_to_stats(row) -> KeyStats:
        (key, total_req, total_ok, total_rl, total_sus,
         total_tr, total_err, lat_sum, lat_min, lat_max,
         lat_count, last_used) = row

        s = KeyStats(key=key)
        s.total_requests   = total_req
        s.total_success    = total_ok
        s.total_rate_limit = total_rl
        s.total_suspended  = total_sus
        s.total_transient  = total_tr
        s.total_error      = total_err
        s.latency_sum      = lat_sum   or 0.0
        s.latency_min      = lat_min   if lat_min is not None else float("inf")
        s.latency_max      = lat_max   or 0.0
        s.latency_count    = lat_count or 0
        s.last_used_at     = last_used or 0.0
        return s
