"""
examples/basic_usage.py — minimal working example.

Run:
    GEMINI_API_KEYS="key1,key2,key3" python examples/basic_usage.py
"""

import os
import sys
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gemini_rotator import GeminiAPIRotator

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

# ── 1. Load keys ─────────────────────────────────────────────────────────

raw = os.getenv("GEMINI_API_KEYS", "")
if not raw:
    print("Set GEMINI_API_KEYS=key1,key2,key3 and re-run.")
    sys.exit(1)

keys = [k.strip() for k in raw.split(",") if k.strip()]
rotator = GeminiAPIRotator(keys, cooldown_seconds=60)
print(rotator)

# ── 2. Use with google-generativeai ──────────────────────────────────────
#
# pip install google-generativeai
#
try:
    import google.generativeai as genai

    def call_gemini(prompt: str) -> str:
        key = rotator.get_next_working_key()
        genai.configure(api_key=key)
        try:
            model = genai.GenerativeModel("gemini-2.5-flash")
            return model.generate_content(prompt).text
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower() or "rate" in err.lower():
                rotator.mark_rate_limited(key)
            raise

    print(call_gemini("Reply with exactly three words."))

except ImportError:
    print("google-generativeai not installed — skipping live call demo.")

# ── 3. Inspect status ────────────────────────────────────────────────────

print("\nKey status:")
for entry in rotator.status():
    flag = "✅" if entry["available"] else "🔴"
    print(f"  {flag} {entry['masked']}  requests={entry['requests']}")
