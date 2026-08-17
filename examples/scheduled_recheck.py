"""
examples/scheduled_recheck.py

How to run revalidate_suspended_keys() on a schedule inside an async app
(e.g. an aiohttp server, a Telegram bot using python-telegram-bot, etc.).
"""

import asyncio
import os
import sys
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from gemini_rotator import GeminiAPIRotator

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

RECHECK_INTERVAL_SECONDS = 6 * 60 * 60  # 6 hours


async def recheck_loop(rotator: GeminiAPIRotator) -> None:
    """Background task — runs forever, rechecks suspended keys every interval."""
    while True:
        await asyncio.sleep(RECHECK_INTERVAL_SECONDS)
        await rotator.revalidate_suspended_keys()


async def main() -> None:
    keys = [k.strip() for k in os.getenv("GEMINI_API_KEYS", "key1,key2").split(",")]
    rotator = GeminiAPIRotator(keys)

    # Start recheck loop as a background task
    asyncio.create_task(recheck_loop(rotator))

    # Your main application logic here
    print(rotator.summary())
    print("App running — recheck loop started.")

    # Keep running (replace with your real app logic)
    await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
