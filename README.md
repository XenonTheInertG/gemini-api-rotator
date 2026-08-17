# gemini-key-rotator

Gemini API key rotation with proactive rate-limit avoidance, exponential backoff, and optional persistent suspension tracking.

---

## How it works

Keys cycle round-robin. When a key gets throttled:

- **429 RESOURCE_EXHAUSTED** → key goes into a timed cooldown (60s, then 120s, 240s… capped at 30 min on repeat hits). Automatically returns to rotation when the window expires.
- **403 CONSUMER_SUSPENDED / PERMISSION_DENIED** → key is blacklisted. Persisted to DB so it survives restarts. Probed again automatically on schedule.
- **500 / 503** → transient; same key is retried.

On top of that, each key tracks its own call timestamps in a 60-second rolling window. A key that has already hit its per-minute ceiling is **skipped proactively** — no waiting for a 429 to tell you it's exhausted.

---

## Installation

Drop `gemini_rotator.py` into your project. No third-party dependencies.

```
your-project/
└── gemini_rotator.py
```

Optional persistent stats and suspension tracking requires a DB adapter (see [examples/mongodb_adapter.py](examples/mongodb_adapter.py)).

---

## Quick start

```python
import google.genai as genai
from google.genai import types as genai_types
from gemini_rotator import GeminiAPIRotator

rotator = GeminiAPIRotator(
    ["AIza..key1..", "AIza..key2..", "AIza..key3.."],
    rpm_per_key=15,   # proactively skip a key after 15 calls/min
)

def call_gemini(prompt: str) -> str:
    key = rotator.get_next_working_key()
    if key is None:
        raise RuntimeError("All keys suspended.")

    client = genai.Client(api_key=key)
    try:
        response = client.models.generate_content(
            model    = "gemini-2.5-flash",
            contents = [prompt],
            config   = genai_types.GenerateContentConfig(max_output_tokens=256),
        )
        rotator.record_success(key)
        return response.text
    except Exception as e:
        err = str(e)
        if "CONSUMER_SUSPENDED" in err or "PERMISSION_DENIED" in err:
            rotator.mark_suspended(key)
        elif "429" in err or "RESOURCE_EXHAUSTED" in err:
            rotator.mark_rate_limited(key)
        else:
            rotator.record_error(key)
        raise
```

---

## Loading keys from environment

```python
import os
keys = [k.strip() for k in os.environ["GEMINI_API_KEYS"].split(",")]
rotator = GeminiAPIRotator(keys)
```

---

## API

### Constructor

```python
GeminiAPIRotator(keys, *, rpm_per_key=15, db=None)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `keys` | `list[str]` | required | One or more Gemini API keys. Duplicates removed. |
| `rpm_per_key` | `int` | `15` | Calls per key per 60s before it is proactively skipped. |
| `db` | `DBAdapter \| None` | `None` | Optional adapter for persistent suspension and stats. |

---

### Key selection

#### `get_next_working_key() → str | None`

Returns the next available key. Skips suspended keys, cooling keys, and keys at their RPM ceiling. Falls back gracefully when everything is limited. Returns `None` only if every key is suspended.

---

### Reporting outcomes

#### `record_success(key)`
Call after every successful request. Resets the backoff counter and records the call for RPM tracking.

#### `mark_rate_limited(key)`
Call on **429 RESOURCE_EXHAUSTED**. Cooldown grows exponentially on repeat hits for the same key.

#### `mark_suspended(key)`
Call on **403 CONSUMER_SUSPENDED / PERMISSION_DENIED**. Key is blacklisted and persisted to DB (if configured).

#### `record_error(key)`
Call on other errors (500, 503, network timeouts, etc.).

---

### Automatic recovery

#### `async revalidate_suspended_keys()`

Fires a cheap parallel probe against every suspended key. Keys that respond successfully are immediately unsuspended and rejoin rotation. Run this on a schedule:

```python
async def recheck_loop(rotator):
    while True:
        await asyncio.sleep(6 * 60 * 60)   # every 6 hours
        await rotator.revalidate_suspended_keys()
```

---

### Observability

#### `status() → list[dict]`

Structured per-key status:

```python
[
    {
        "masked": "...zXyZ1234",
        "state": "active",          # "active" | "cooling" | "suspended"
        "cooldown_remaining": 0.0,
        "rpm_used": 3,
        "rpm_limit": 15,
        "stats": {"success": 142, "rate_limit": 2, "error": 0},
    },
    ...
]
```

#### `masked_list() → list[str]`

Human-readable lines for a `/stats` command or admin panel:

```
✅ ...abcd1234  active  ✓142  ⚠2  err:0  rpm:3/15
⏳ ...efgh5678  cooling 47s  ✓98  ⚠5  err:0
❌ ...ijkl9012  SUSPENDED  ✓201  ⚠12  err:3
```

#### `summary() → str`

One-liner for health checks:

```
Keys: 4 total | 3 active | 1 cooling | 0 suspended
```

#### `get_active_count() → int`, `total_count() → int`

---

### Key management

#### `add_key(key) → bool`
Add a key at runtime. Returns `False` if it already exists.

#### `remove_key(key) → bool`
Remove a key at runtime. Returns `False` if not found. Raises `ValueError` if it is the last key.

---

## Persistent DB adapter

By default all state is in-memory. Pass a `db` adapter to persist suspension state and stats across restarts.

A ready-to-use MongoDB adapter is in [examples/mongodb_adapter.py](examples/mongodb_adapter.py). Implementing your own is straightforward — just satisfy this protocol:

```python
class DBAdapter(Protocol):
    def get_suspended_keys(self) -> set: ...
    def mark_key_suspended(self, key: str) -> None: ...
    def unmark_key_suspended(self, key: str) -> None: ...
    def update_key_stat(self, key: str, event: str) -> None: ...
    def get_all_key_stats(self) -> list: ...
```

`event` is one of `"success"`, `"rate_limit"`, or `"error"`.

---

## Logging

Uses Python's standard `logging` module under the logger name `gemini_rotator`.

```python
import logging
logging.basicConfig(level=logging.INFO)
```

---

## Running the tests

```bash
pip install pytest
pytest tests/
```

---

## License

MIT. See [LICENSE](LICENSE).
