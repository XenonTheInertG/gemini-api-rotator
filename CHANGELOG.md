# Changelog

## [2.0.0] — 2026-08-28

Complete rewrite. Now a proper installable library.

### Added
- `pip install gemini-rotator`
- `await rotator.execute(prompt)` — one-line API call with auto key selection, retry, and stat recording
- `await rotator.execute_batch(prompts, concurrency=10)` — concurrent batch execution
- Per-request latency stored in SQLite (`request_log` table, 100k row cap)
- `SQLiteAdapter` — zero-dependency persistence (stdlib sqlite3)
- `MongoDBAdapter` — MongoDB persistence with capped request log
- `MemoryAdapter` — default in-memory adapter
- `DBAdapter` protocol — implement your own backend
- `RotatorConfig` dataclass — all settings in one place
- `on_key_suspended` / `on_key_recovered` hooks for alerting
- `acquire()` / `release()` — per-key concurrency tracking
- `record_outcome(key, exc)` — auto-classifies errors and routes to the right handler
- `export_stats()` — JSON-serialisable stats for all keys
- `gemini-rotator` CLI — status, stats, latency, add, remove, test, reset-cooldowns, reset-stats
- Thread-safe throughout (RLock on all shared state)
- `KeyState`, `KeyStats`, `KeyStatus` — typed models for all observability data

## [1.0.0] — 2026-06-24

Initial release — standalone single-file rotator.
