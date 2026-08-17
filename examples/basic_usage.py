"""
examples/basic_usage.py

Minimal working example — no database, no async, just keys from env.

    GEMINI_API_KEYS="key1,key2,key3" python examples/basic_usage.py
"""

import os
import sys
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from gemini_rotator import GeminiAPIRotator

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

# ── Load keys ─────────────────────────────────────────────────────────────────

raw = os.getenv("GEMINI_API_KEYS", "")
if not raw:
    print("Set GEMINI_API_KEYS=key1,key2,key3 and re-run.")
    sys.exit(1)

keys    = [k.strip() for k in raw.split(",") if k.strip()]
rotator = GeminiAPIRotator(keys, rpm_per_key=15)
print(rotator)

# ── Use with google-genai ─────────────────────────────────────────────────────

try:
    import google.genai as genai
    from google.genai import types as genai_types

    def call_gemini(prompt: str) -> str:
        key = rotator.get_next_working_key()
        if key is None:
            raise RuntimeError("No API keys available.")

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

    print(call_gemini("Reply with exactly three words."))

except ImportError:
    print("google-genai not installed — skipping live call demo.")
    print("Install: pip install google-genai")

# ── Inspect status ────────────────────────────────────────────────────────────

print()
print(rotator.summary())
print()
for line in rotator.masked_list():
    print(" ", line)
