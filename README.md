# gemini-key-rotator

Round-robin Gemini API key rotation with cooldown-based rate-limit handling.

When a key hits a rate limit, it's put in a timed cooldown instead of being
permanently dropped. The rotator moves to the next available key automatically.
Once the cooldown expires, the key is back in rotation.

No dependencies beyond the standard library.

---

## Why

Gemini's free tier enforces per-key rate limits. If you have multiple API keys,
this rotator lets you spread load across them and recover gracefully when one
gets throttled — without any manual intervention.

---

## Installation

Copy `gemini_rotator.py` into your project. That's it.

```
your-project/
├── gemini_rotator.py   ← drop this in
└── your_code.py
```

There are no third-party dependencies.

---

## Quick start

```python
import google.generativeai as genai
from gemini_rotator import GeminiAPIRotator

rotator = GeminiAPIRotator([
    "AIza..key1..",
    "AIza..key2..",
    "AIza..key3..",
])

def call_gemini(prompt: str) -> str:
    key = rotator.get_next_working_key()
    genai.configure(api_key=key)
    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        if "429" in str(e) or "quota" in str(e).lower():
            rotator.mark_rate_limited(key)
        raise
```

---

## API

### `GeminiAPIRotator(api_keys, cooldown_seconds=60)`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `api_keys` | `list[str]` | required | One or more Gemini API keys |
| `cooldown_seconds` | `int` | `60` | How long a rate-limited key is skipped |

---

### Getting keys

#### `get_next_working_key() → str`

Returns the next key not currently in cooldown, and advances the internal index.

If **every** key is in cooldown, returns the one with the shortest remaining
wait time rather than raising — you can decide whether to sleep or proceed.

```python
key = rotator.get_next_working_key()
```

#### `get_current_key() → str`

Returns the current key without advancing the index.

---

### Reporting failures

#### `mark_rate_limited(key)`

Call this when a request returns a 429 / quota error. The key is put in cooldown
for `cooldown_seconds` and the rotator moves to the next key.

```python
except RateLimitError:
    rotator.mark_rate_limited(key)
```

`mark_failed` is kept as an alias.

---

### Observability

#### `available_count() → int`

Number of keys not currently in cooldown.

#### `total_count() → int`

Total number of managed keys.

#### `status() → list[dict]`

Returns a list of dicts, one per key:

```python
[
    {
        "masked": "AIza...zXyZ",
        "available": True,
        "cooldown_remaining": 0.0,
        "requests": 42,
    },
    ...
]
```

#### `reset_cooldowns()`

Clears all cooldowns immediately. Useful in tests or after a manual recovery.

---

### Hot-swap at runtime

#### `add_key(key) → bool`

Adds a key. Returns `False` if it already exists.

#### `remove_key(key) → bool`

Removes a key. Returns `False` if not found. Raises `ValueError` if it's the
last key.

#### `sync_keys(new_keys)`

Reconciles the managed key list against `new_keys` — adds new ones, removes
missing ones, preserves cooldown state for keys that remain. Useful when keys
are stored in a database.

`reload_from_db` is kept as an alias.

---

## Loading keys from environment variables

```python
import os
from gemini_rotator import GeminiAPIRotator

# Option 1: comma-separated list in one env var
keys = os.environ["GEMINI_API_KEYS"].split(",")

# Option 2: individual numbered vars
keys = [v for k, v in os.environ.items() if k.startswith("GEMINI_KEY_")]

rotator = GeminiAPIRotator(keys)
```

---

## Logging

The rotator uses Python's standard `logging` module under the logger name
`gemini_rotator`. To see its output:

```python
import logging
logging.basicConfig(level=logging.INFO)
```

---

## Running the tests

```bash
python -m pytest tests/
```

---

## License

MIT. See [LICENSE](LICENSE).
