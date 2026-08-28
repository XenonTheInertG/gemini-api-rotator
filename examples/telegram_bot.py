"""
examples/telegram_bot.py — drop-in Telegram bot integration.

    pip install "gemini-rotator[genai]" python-telegram-bot
    GEMINI_API_KEYS="k1,k2" BOT_TOKEN="..." python examples/telegram_bot.py
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gemini_rotator import GeminiAPIRotator
from gemini_rotator.adapters import SQLiteAdapter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Setup ─────────────────────────────────────────────────────────────────────

keys = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()]
if not keys:
    print("Set GEMINI_API_KEYS=key1,key2 and re-run.")
    sys.exit(1)

db      = SQLiteAdapter("bot_keys.db")
rotator = GeminiAPIRotator(
    keys,
    db               = db,
    on_key_suspended = lambda k: logger.error("⚠ Key suspended: ...%s", k[-6:]),
    on_key_recovered = lambda k: logger.info("✓ Key recovered: ...%s", k[-6:]),
)

# ── Recheck loop ──────────────────────────────────────────────────────────────

async def recheck_loop():
    while True:
        await asyncio.sleep(6 * 60 * 60)
        n = await rotator.revalidate_suspended_keys()
        logger.info("Recheck complete: %d key(s) recovered.", n)

# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update, context):
    await update.message.reply_text("Ready. Send me anything.")

async def handle_message(update, context):
    user_text = update.message.text
    try:
        response = await rotator.execute(
            user_text,
            model      = "gemini-2.5-flash",
            max_tokens = 512,
        )
        await update.message.reply_text(response.text)
    except RuntimeError as e:
        await update.message.reply_text("⚠️ All API keys are currently unavailable. Try again shortly.")
        logger.error("All keys unavailable: %s", e)

async def cmd_status(update, context):
    lines = [rotator.summary(), ""]
    for s in rotator.status():
        icon = {"active": "✅", "cooling": "⏳", "suspended": "❌"}.get(s.state.value, "?")
        avg  = f"{round(s.stats.avg_latency * 1000)}ms" if s.stats.avg_latency else "—"
        lines.append(f"{icon} {s.masked}  ✓{s.stats.total_success}  avg:{avg}")
    await update.message.reply_text("\n".join(lines))

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    try:
        from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
    except ImportError:
        print("Install python-telegram-bot: pip install python-telegram-bot")
        sys.exit(1)

    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("Set BOT_TOKEN env var.")
        sys.exit(1)

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    loop = asyncio.get_event_loop()
    loop.create_task(recheck_loop())
    app.run_polling()

if __name__ == "__main__":
    main()
