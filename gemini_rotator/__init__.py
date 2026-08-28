"""
gemini-rotator — production-grade Gemini API key rotation.

Quick start
-----------
>>> from gemini_rotator import GeminiAPIRotator
>>> rotator = GeminiAPIRotator(["AIza..key1..", "AIza..key2.."])
>>> response = await rotator.execute("Hello!")
>>> print(response.text)

With SQLite persistence
-----------------------
>>> from gemini_rotator import GeminiAPIRotator
>>> from gemini_rotator.adapters import SQLiteAdapter
>>> db      = SQLiteAdapter("keys.db")
>>> rotator = GeminiAPIRotator(keys, db=db)
"""

from gemini_rotator.rotator    import GeminiAPIRotator
from gemini_rotator.models     import (
    ErrorType,
    KeyState,
    KeyStats,
    KeyStatus,
    RotatorConfig,
    classify_error,
    mask_key,
)
from gemini_rotator.adapters   import DBAdapter, MemoryAdapter, SQLiteAdapter

__all__ = [
    # Main class
    "GeminiAPIRotator",
    # Config
    "RotatorConfig",
    # Models
    "ErrorType",
    "KeyState",
    "KeyStats",
    "KeyStatus",
    # Adapters
    "DBAdapter",
    "MemoryAdapter",
    "SQLiteAdapter",
    # Helpers
    "classify_error",
    "mask_key",
]

__version__ = "2.0.0"
