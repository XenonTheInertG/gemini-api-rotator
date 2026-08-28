"""
gemini_rotator.adapters.mongodb — MongoDB adapter.

Requirements: pip install pymongo

Collections
-----------
gemini_suspended_keys  — one doc per suspended key
gemini_key_stats       — one doc per key, cumulative counters + latency aggregates
gemini_request_log     — one doc per request; capped collection (optional)
"""

from __future__ import annotations

import time
from typing import Optional, Set

from gemini_rotator.models import KeyStats


class MongoDBAdapter:
    """
    MongoDB-backed adapter for GeminiAPIRotator.

    Parameters
    ----------
    uri : str
        MongoDB connection URI.
    db_name : str
        Database name.
    log_max_docs : int
        Cap for the request_log capped collection. 0 disables logging.
        Default: 100 000 docs (~50 MB).
    """

    def __init__(
        self,
        uri:          str,
        db_name:      str,
        log_max_docs: int = 100_000,
    ) -> None:
        try:
            from pymongo import MongoClient, ASCENDING
        except ImportError:
            raise ImportError(
                "pymongo is required for MongoDBAdapter. "
                "Install it: pip install pymongo"
            )

        client         = MongoClient(uri)
        db             = client[db_name]
        self._sus_col  = db["gemini_suspended_keys"]
        self._stat_col = db["gemini_key_stats"]

        self._sus_col.create_index("key",  unique=True)
        self._stat_col.create_index("key", unique=True)

        # Optional capped request log
        self._log_col = None
        if log_max_docs > 0:
            existing = db.list_collection_names()
            if "gemini_request_log" not in existing:
                db.create_collection(
                    "gemini_request_log",
                    capped=True,
                    max=log_max_docs,
                    size=log_max_docs * 512,
                )
            self._log_col = db["gemini_request_log"]
            self._log_col.create_index([("key", ASCENDING)])
            self._log_col.create_index([("ts",  ASCENDING)])

    # ── Suspension ────────────────────────────────────────────────────────

    def get_suspended_keys(self) -> Set[str]:
        return {doc["key"] for doc in self._sus_col.find({}, {"key": 1})}

    def mark_key_suspended(self, key: str) -> None:
        self._sus_col.update_one(
            {"key": key},
            {"$set": {"key": key, "suspended_at": time.time()}},
            upsert=True,
        )

    def unmark_key_suspended(self, key: str) -> None:
        self._sus_col.delete_one({"key": key})

    # ── Stats ─────────────────────────────────────────────────────────────

    def record_request(
        self,
        key:     str,
        event:   str,
        latency: Optional[float] = None,
        model:   Optional[str]   = None,
    ) -> None:
        now      = time.time()
        inc: dict = {"total_requests": 1}

        event_map = {
            "success":    "total_success",
            "rate_limit": "total_rate_limit",
            "suspended":  "total_suspended",
            "transient":  "total_transient",
        }
        inc[event_map.get(event, "total_error")] = 1

        update: dict = {"$inc": inc, "$set": {"last_used_at": now}, "$setOnInsert": {"key": key}}

        if latency is not None and latency >= 0:
            lat_ms = latency * 1000
            update["$inc"].update({"latency_sum": latency, "latency_count": 1})
            update["$min"] = {"latency_min": latency}
            update["$max"] = {"latency_max": latency}

        self._stat_col.update_one({"key": key}, update, upsert=True)

        if self._log_col is not None:
            self._log_col.insert_one({
                "key":        key,
                "event":      event,
                "latency_ms": latency * 1000 if latency is not None else None,
                "model":      model,
                "ts":         now,
            })

    def get_stats(self, key: str) -> KeyStats:
        doc = self._stat_col.find_one({"key": key})
        if doc is None:
            return KeyStats(key=key)
        return self._doc_to_stats(doc)

    def get_all_stats(self) -> list[KeyStats]:
        return [self._doc_to_stats(d) for d in self._stat_col.find()]

    def reset_stats(self, key: Optional[str] = None) -> None:
        query = {"key": key} if key else {}
        self._stat_col.delete_many(query)
        if self._log_col is not None:
            self._log_col.delete_many(query)

    def get_latency_history(
        self,
        key:   Optional[str] = None,
        limit: int            = 500,
        model: Optional[str] = None,
    ) -> list[dict]:
        if self._log_col is None:
            return []
        q: dict = {"event": "success", "latency_ms": {"$ne": None}}
        if key:   q["key"]   = key
        if model: q["model"] = model
        cursor = self._log_col.find(q, {"_id": 0}).sort("ts", -1).limit(limit)
        return list(cursor)

    @staticmethod
    def _doc_to_stats(doc: dict) -> KeyStats:
        s = KeyStats(key=doc["key"])
        s.total_requests   = doc.get("total_requests",   0)
        s.total_success    = doc.get("total_success",    0)
        s.total_rate_limit = doc.get("total_rate_limit", 0)
        s.total_suspended  = doc.get("total_suspended",  0)
        s.total_transient  = doc.get("total_transient",  0)
        s.total_error      = doc.get("total_error",      0)
        s.latency_sum      = doc.get("latency_sum",      0.0)
        s.latency_min      = doc.get("latency_min",      float("inf"))
        s.latency_max      = doc.get("latency_max",      0.0)
        s.latency_count    = doc.get("latency_count",    0)
        s.last_used_at     = doc.get("last_used_at",     0.0)
        return s
