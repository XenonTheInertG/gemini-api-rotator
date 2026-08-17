"""
examples/mongodb_adapter.py

A ready-to-use MongoDB adapter for persistent suspension state and per-key
stats. Wire it up when constructing GeminiAPIRotator.

Requirements:
    pip install pymongo

Usage:
    from examples.mongodb_adapter import MongoDBAdapter
    from gemini_rotator import GeminiAPIRotator

    db      = MongoDBAdapter("mongodb://localhost:27017", "mydb")
    rotator = GeminiAPIRotator(keys, db=db)
"""

import time
from typing import List


class MongoDBAdapter:
    """
    Persistent adapter for GeminiAPIRotator.

    Collections used:
        gemini_suspended_keys  — one doc per suspended key
        gemini_key_stats       — one doc per key, cumulative counters
    """

    def __init__(self, mongo_uri: str, db_name: str) -> None:
        from pymongo import MongoClient
        client = MongoClient(mongo_uri)
        self._db = client[db_name]
        self._suspended_col = self._db["gemini_suspended_keys"]
        self._stats_col     = self._db["gemini_key_stats"]

        # Ensure indexes
        self._suspended_col.create_index("key", unique=True)
        self._stats_col.create_index("key", unique=True)

    # ── Suspension ────────────────────────────────────────────────────────────

    def get_suspended_keys(self) -> set:
        return {doc["key"] for doc in self._suspended_col.find({}, {"key": 1})}

    def mark_key_suspended(self, key: str) -> None:
        self._suspended_col.update_one(
            {"key": key},
            {"$set": {"key": key, "suspended_at": time.time()}},
            upsert=True,
        )

    def unmark_key_suspended(self, key: str) -> None:
        self._suspended_col.delete_one({"key": key})

    # ── Stats ─────────────────────────────────────────────────────────────────

    def update_key_stat(self, key: str, event: str) -> None:
        """
        event: "success" | "rate_limit" | "error"
        """
        field_map = {
            "success":    "total_success",
            "rate_limit": "total_rate_limit",
            "error":      "total_error",
        }
        field = field_map.get(event)
        if not field:
            return
        self._stats_col.update_one(
            {"key": key},
            {
                "$inc": {field: 1, "total_calls": 1},
                "$set": {"last_used": time.time()},
                "$setOnInsert": {"key": key},
            },
            upsert=True,
        )

    def get_all_key_stats(self) -> List[dict]:
        return list(self._stats_col.find({}, {"_id": 0}))
