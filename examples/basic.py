"""
examples/basic.py — minimal async usage.

    pip install "gemini-rotator[genai]"
    GEMINI_API_KEYS="key1,key2" python examples/basic.py
"""

import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gemini_rotator import GeminiAPIRotator
from gemini_rotator.adapters import SQLiteAdapter

keys = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()]
if not keys:
    print("Set GEMINI_API_KEYS=key1,key2 and re-run.")
    sys.exit(1)

db      = SQLiteAdapter("example.db")
rotator = GeminiAPIRotator(keys, db=db)

async def main():
    # Single call
    response = await rotator.execute("Reply with exactly three words.")
    print("Response:", response.text)

    # Batch
    responses = await rotator.execute_batch(
        ["What is 2+2?", "What is the capital of France?", "Name a colour."],
        concurrency=3,
    )
    for r in responses:
        print(" -", r.text if not isinstance(r, Exception) else f"ERROR: {r}")

    # Status
    print()
    print(rotator.summary())
    for s in rotator.status():
        print(f"  {s.masked}  {s.state.value:<10}  "
              f"✓{s.stats.total_success}  "
              f"avg={round(s.stats.avg_latency*1000) if s.stats.avg_latency else '—'}ms")

asyncio.run(main())
