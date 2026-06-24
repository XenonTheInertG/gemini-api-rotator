"""
examples/retry_wrapper.py — decorator that auto-retries with key rotation.

Wraps any function that takes `api_key` as a kwarg. On a 429 / quota error
it marks the key, waits briefly, and retries with the next available key.
"""

import functools
import logging
import time

logger = logging.getLogger(__name__)


def with_key_rotation(rotator, max_retries: int = 3, retry_delay: float = 1.0):
    """
    Decorator factory.

    Usage
    -----
    @with_key_rotation(rotator, max_retries=3)
    def my_api_call(prompt, *, api_key):
        ...

    my_api_call("hello")          # api_key injected automatically
    my_api_call("hello", api_key="override")  # explicit override
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_retries + 1):
                key = kwargs.pop("api_key", None) or rotator.get_next_working_key()
                try:
                    return fn(*args, api_key=key, **kwargs)
                except Exception as e:
                    err = str(e)
                    is_rate_limit = "429" in err or "quota" in err.lower() or "rate" in err.lower()
                    if is_rate_limit:
                        rotator.mark_rate_limited(key)
                        logger.warning(
                            "Attempt %d/%d rate-limited. Available keys: %d.",
                            attempt, max_retries, rotator.available_count(),
                        )
                        if attempt < max_retries:
                            time.sleep(retry_delay)
                    else:
                        raise
                    last_exc = e
            raise last_exc
        return wrapper
    return decorator


# ── Demo ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from gemini_rotator import GeminiAPIRotator

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    rotator = GeminiAPIRotator(["key_a_fake", "key_b_fake"], cooldown_seconds=5)

    call_count = {"n": 0}

    @with_key_rotation(rotator, max_retries=4)
    def fake_api_call(prompt, *, api_key):
        call_count["n"] += 1
        # Simulate first two keys being rate-limited
        if call_count["n"] <= 2:
            raise Exception("429 RESOURCE_EXHAUSTED quota exceeded")
        return f"OK with key {api_key[:8]}..."

    try:
        result = fake_api_call("test prompt")
        print("Result:", result)
    except Exception as e:
        print("All retries exhausted:", e)
