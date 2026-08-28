# gemini-rotator

Production-grade Gemini API key rotation with automatic rate-limit handling, exponential backoff, per-key RPM tracking, per-request latency stats, and optional persistence.

```bash
pip install gemini-rotator
```

---

## Why

Gemini's free tier enforces per-key rate limits and quota ceilings. With multiple keys you can spread load across them — but only if you handle the rotation, cooldowns, retries, and stats correctly. This library does all of that.

Key design goals:
- **Zero friction** — `await rotator.execute(prompt)` handles everything
- **Zero required dependencies** — core library is pure stdlib; `google-genai` only needed for actual API calls
- **Persistent stats** — per-request latency stored in SQLite (ships with Python)
- **Thread-safe** — works in async apps, threaded workers, Telegram bots, FastAPI, anything

---

## Install

```bash
# Core library only (no dependencies)
pip install gemini-rotator

# With google-genai so you can use execute()
pip install "gemini-rotator[genai]"

# With MongoDB adapter
pip install "gemini-rotator[mongo]"

# Everything
pip install "gemini-rotator[all]"
```

---

## Quick start

```python
import asyncio
from gemini_rotator import GeminiAPIRotator

rotator = GeminiAPIRotator([
    "AIza..key1..",
    "AIza..key2..",
    "AIza..key3..",
])

async def main():
    response = await rotator.execute("Explain async/await in one sentence.")
    print(response.text)

asyncio.run(main())
```

That's it. Key selection, retries, cooldowns, and stat recording happen automatically.

---

## With SQLite persistence

Suspension state and latency stats survive restarts.

```python
from gemini_rotator import GeminiAPIRotator
from gemini_rotator.adapters import SQLiteAdapter

db      = SQLiteAdapter("gemini_keys.db")
rotator = GeminiAPIRotator(keys, db=db)
```

---

## With custom config

```python
from gemini_rotator import GeminiAPIRotator, RotatorConfig

cfg = RotatorConfig(
    rpm_per_key            = 10,      # proactively skip a key after 10 calls/min
    cooldown_base_seconds  = 60,      # first rate-limit = 60s cooldown
    cooldown_max_seconds   = 1800,    # cap at 30 min after repeated hits
    max_concurrent_per_key = 3,       # max simultaneous requests per key
    max_retries            = 4,       # retries before raising
    retry_delay_seconds    = 1.0,
)

rotator = GeminiAPIRotator(keys, config=cfg)
```

---

## Batch requests

```python
prompts = ["Summarise X", "Translate Y", "Classify Z", ...]

responses = await rotator.execute_batch(
    prompts,
    model       = "gemini-2.5-flash",
    concurrency = 10,   # max simultaneous requests
)

for r in responses:
    if isinstance(r, Exception):
        print("failed:", r)
    else:
        print(r.text)
```

---

## Manual key management

If you prefer to manage keys yourself:

```python
key = rotator.get_next_working_key()
if key is None:
    raise RuntimeError("All keys suspended.")

rotator.acquire(key)   # track concurrency
try:
    response = client.models.generate_content(...)
    rotator.record_success(key, latency=elapsed, model="gemini-2.5-flash")
except Exception as e:
    rotator.record_outcome(key, exc=e)   # auto-classifies and routes
finally:
    rotator.release(key)
```

---

## Error classification

`record_outcome(key, exc=e)` classifies the exception automatically:

| Exception message contains | Action |
|---|---|
| `429`, `RESOURCE_EXHAUSTED`, `QUOTA` | `mark_rate_limited()` — exponential cooldown |
| `CONSUMER_SUSPENDED`, `PERMISSION_DENIED`, `API_KEY_INVALID` | `mark_suspended()` — blacklisted until recheck |
| `500`, `503`, `INTERNAL`, `UNAVAILABLE` | `record_error()` — transient, retry same key |
| anything else | `record_error()` — unknown |

---

## Alert hooks

```python
import requests

def alert_slack(key: str):
    requests.post(SLACK_WEBHOOK, json={"text": f"Gemini key {key[-6:]} suspended!"})

rotator = GeminiAPIRotator(
    keys,
    on_key_suspended = alert_slack,
    on_key_recovered = lambda k: print(f"Key {k[-6:]} recovered."),
)
```

---

## Automatic suspended-key recovery

Suspended keys are automatically probed on a schedule. Any key that starts responding again rejoins rotation immediately.

```python
async def recheck_loop():
    while True:
        await asyncio.sleep(6 * 60 * 60)   # every 6 hours
        await rotator.revalidate_suspended_keys()

asyncio.create_task(recheck_loop())
```

---

## Observability

### Summary line
```python
print(rotator.summary())
# Keys: 4 total | 2 active | 1 cooling | 1 suspended
```

### Per-key status
```python
for s in rotator.status():
    print(s.masked, s.state, s.cooldown_remaining, s.stats.avg_latency)
```

Each `KeyStatus` object:

| Field | Type | Description |
|---|---|---|
| `masked` | str | `AIza...abcd1234` |
| `state` | KeyState | `active` / `cooling` / `suspended` |
| `cooldown_remaining` | float | Seconds left in cooldown |
| `consecutive_limits` | int | Consecutive rate-limit hits |
| `rpm_used` | int | Calls in the last 60s |
| `stats.total_requests` | int | Lifetime requests |
| `stats.success_rate` | float | 0.0 – 1.0 |
| `stats.avg_latency` | float | Seconds (None if no data) |
| `stats.latency_min/max` | float | Seconds |

### Export as JSON
```python
import json
print(json.dumps(rotator.export_stats(), indent=2))
```

### Latency history (SQLite only)
```python
rows = db.get_latency_history(key="AIza...", limit=100)
# [{"key": ..., "latency_ms": 312.4, "model": "gemini-2.5-flash", "ts": ...}, ...]
```

---

## CLI

```bash
export GEMINI_API_KEYS="key1,key2,key3"
export GEMINI_ROTATOR_DB="gemini_keys.db"   # optional, default: ./gemini_rotator.db

gemini-rotator status           # per-key status table
gemini-rotator stats            # cumulative stats
gemini-rotator stats --json     # machine-readable
gemini-rotator latency          # recent per-request latency log
gemini-rotator latency --limit 100
gemini-rotator test             # probe every key against Gemini live
gemini-rotator add AIzaSy...    # add a key
gemini-rotator remove AIzaSy... # remove a key
gemini-rotator reset-cooldowns  # clear all cooldowns
gemini-rotator reset-stats      # reset all stats
gemini-rotator reset-stats AIzaSy...  # reset one key's stats
```

Example `gemini-rotator status` output:
```
  Keys: 4 total | 2 active | 1 cooling | 1 suspended

  KEY               STATE       COOLDOWN      RPM    REQUESTS    OK%   AVG ms
  ────────────────  ──────────  ─────────  ──────   ─────────  ─────  ───────
  AIza...abcd1234   ● active            —    3/15        1423   98%      312
  AIza...efgh5678   ● active            —    1/15         891   97%      289
  AIza...ijkl9012   ⏳ cooling        47s    0/15         234   85%      401
  AIza...mnop3456   ❌ suspended         —    0/15          89   71%      —
```

---

## Custom DB adapter

Implement the `DBAdapter` protocol to use any backend (PostgreSQL, Redis, DynamoDB, …):

```python
from gemini_rotator.adapters import DBAdapter
from gemini_rotator.models   import KeyStats
from typing import Optional, Set

class MyAdapter:
    def get_suspended_keys(self) -> Set[str]: ...
    def mark_key_suspended(self, key: str) -> None: ...
    def unmark_key_suspended(self, key: str) -> None: ...
    def record_request(self, key, event, latency=None, model=None) -> None: ...
    def get_stats(self, key: str) -> KeyStats: ...
    def get_all_stats(self) -> list[KeyStats]: ...
    def reset_stats(self, key: Optional[str] = None) -> None: ...

rotator = GeminiAPIRotator(keys, db=MyAdapter())
```

`event` is one of `"success"`, `"rate_limit"`, `"suspended"`, `"transient"`, `"error"`.

---

## Integrating with a Telegram bot

```python
from telegram.ext import ApplicationBuilder
from gemini_rotator import GeminiAPIRotator
from gemini_rotator.adapters import SQLiteAdapter

db      = SQLiteAdapter("bot_keys.db")
rotator = GeminiAPIRotator(
    keys,
    db               = db,
    on_key_suspended = lambda k: logger.error("Key suspended: %s", k[-6:]),
)

async def handle_message(update, context):
    response = await rotator.execute(
        update.message.text,
        model      = "gemini-2.5-flash",
        max_tokens = 512,
    )
    await update.message.reply_text(response.text)
```

---

## Running the tests

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

---

## License

MIT. See [LICENSE](LICENSE).
