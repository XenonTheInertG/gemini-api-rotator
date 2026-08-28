"""
gemini_rotator.cli — command-line interface.

    gemini-rotator status
    gemini-rotator add   AIzaSy...
    gemini-rotator remove AIzaSy...
    gemini-rotator test
    gemini-rotator stats [--json]
    gemini-rotator reset-cooldowns
    gemini-rotator reset-stats [KEY]
    gemini-rotator latency [--key KEY] [--limit N]

Keys are read from GEMINI_API_KEYS (comma-separated).
DB path defaults to ./gemini_rotator.db (set GEMINI_ROTATOR_DB to override).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
DIM    = "\033[2m"


def _load_rotator():
    from gemini_rotator import GeminiAPIRotator
    from gemini_rotator.adapters.sqlite import SQLiteAdapter

    raw  = os.getenv("GEMINI_API_KEYS", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        print(f"{RED}Error: GEMINI_API_KEYS environment variable is not set.{RESET}")
        sys.exit(1)

    db_path = os.getenv("GEMINI_ROTATOR_DB", "gemini_rotator.db")
    db      = SQLiteAdapter(db_path)
    return GeminiAPIRotator(keys, db=db), db


def cmd_status(args):
    rotator, _ = _load_rotator()
    statuses   = rotator.status()

    print(f"\n{BOLD}  {rotator.summary()}{RESET}\n")
    print(f"  {'KEY':<16}  {'STATE':<10}  {'COOLDOWN':>9}  {'RPM':>7}  {'REQUESTS':>9}  {'OK%':>5}  {'AVG ms':>7}")
    print(f"  {'─'*16}  {'─'*10}  {'─'*9}  {'─'*7}  {'─'*9}  {'─'*5}  {'─'*7}")

    for s in statuses:
        if s.state.value == "active":
            icon = f"{GREEN}● active{RESET}   "
        elif s.state.value == "cooling":
            icon = f"{YELLOW}⏳ cooling{RESET} "
        else:
            icon = f"{RED}❌ suspended{RESET}"

        ok_pct  = f"{s.stats.success_rate*100:.0f}%"
        avg_lat = f"{s.stats.avg_latency*1000:.0f}" if s.stats.avg_latency else "—"
        cd      = f"{s.cooldown_remaining:.0f}s" if s.cooldown_remaining > 0 else "—"
        rpm     = f"{s.rpm_used}/{s.rpm_limit}"

        print(
            f"  {s.masked:<16}  {icon}  {cd:>9}  {rpm:>7}  "
            f"{s.stats.total_requests:>9}  {ok_pct:>5}  {avg_lat:>7}"
        )
    print()


def cmd_stats(args):
    rotator, _ = _load_rotator()
    data       = rotator.export_stats()

    if args.json:
        print(json.dumps(data, indent=2))
        return

    if not data:
        print("No stats recorded yet.")
        return

    for d in data:
        print(f"\n  {BOLD}{d['masked']}{RESET}")
        print(f"    requests  : {d['total_requests']}")
        print(f"    success   : {d['total_success']}  ({d['success_rate']*100:.1f}%)")
        print(f"    rate-limit: {d['total_rate_limit']}")
        print(f"    suspended : {d['total_suspended']}")
        print(f"    transient : {d['total_transient']}")
        print(f"    avg latency: {d['avg_latency_ms']} ms")
        print(f"    min latency: {d['min_latency_ms']} ms")
        print(f"    max latency: {d['max_latency_ms']} ms")
        if d['last_used_at']:
            last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d['last_used_at']))
            print(f"    last used  : {last}")
    print()


def cmd_latency(args):
    rotator, db = _load_rotator()

    if not hasattr(db, "get_latency_history"):
        print("Latency history is only available with the SQLite adapter.")
        return

    rows = db.get_latency_history(key=args.key or None, limit=args.limit)
    if not rows:
        print("No latency data recorded yet.")
        return

    print(f"\n  {'KEY':<16}  {'MODEL':<24}  {'LATENCY ms':>10}  {'TIMESTAMP'}")
    print(f"  {'─'*16}  {'─'*24}  {'─'*10}  {'─'*19}")
    for r in rows:
        masked = f"...{r['key'][-8:]}" if len(r['key']) > 8 else r['key']
        ts     = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r['ts']))
        model  = (r['model'] or '—')[:24]
        lat    = f"{r['latency_ms']:.1f}" if r['latency_ms'] is not None else '—'
        print(f"  {masked:<16}  {model:<24}  {lat:>10}  {ts}")
    print()


def cmd_add(args):
    rotator, _ = _load_rotator()
    if rotator.add_key(args.key):
        print(f"{GREEN}✓ Key added. Total: {rotator.total_count()}{RESET}")
    else:
        print(f"{YELLOW}Key already present.{RESET}")


def cmd_remove(args):
    rotator, _ = _load_rotator()
    try:
        if rotator.remove_key(args.key):
            print(f"{GREEN}✓ Key removed. Total: {rotator.total_count()}{RESET}")
        else:
            print(f"{YELLOW}Key not found.{RESET}")
    except ValueError as e:
        print(f"{RED}Error: {e}{RESET}")


def cmd_test(args):
    print("Probing all keys against Gemini…\n")
    rotator, _ = _load_rotator()

    async def _run():
        return await rotator.revalidate_suspended_keys()

    # Test all keys (including active) by direct probe
    loop = asyncio.new_event_loop()

    keys = rotator._keys
    results = {}
    for key in keys:
        ok = rotator._probe_key(key)
        results[key] = ok

    for key, ok in results.items():
        icon = f"{GREEN}✓ working{RESET}" if ok else f"{RED}✗ failed{RESET}"
        print(f"  ...{key[-8:]}  {icon}")
    print()


def cmd_reset_cooldowns(args):
    rotator, _ = _load_rotator()
    rotator.reset_cooldowns()
    print(f"{GREEN}✓ All cooldowns cleared.{RESET}")


def cmd_reset_stats(args):
    rotator, db = _load_rotator()
    key = args.key or None
    db.reset_stats(key)
    if key:
        print(f"{GREEN}✓ Stats reset for ...{key[-8:]}{RESET}")
    else:
        print(f"{GREEN}✓ All stats reset.{RESET}")


def main():
    parser = argparse.ArgumentParser(
        prog="gemini-rotator",
        description="Gemini API key rotator — CLI management tool.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status",           help="Show per-key status table.")
    sub.add_parser("test",             help="Probe every key against Gemini.")
    sub.add_parser("reset-cooldowns",  help="Clear all cooldowns immediately.")

    p_add = sub.add_parser("add",    help="Add a key.")
    p_add.add_argument("key")

    p_rm = sub.add_parser("remove",  help="Remove a key.")
    p_rm.add_argument("key")

    p_stats = sub.add_parser("stats", help="Show cumulative per-key stats.")
    p_stats.add_argument("--json", action="store_true", help="Output raw JSON.")

    p_lat = sub.add_parser("latency", help="Show recent per-request latency log.")
    p_lat.add_argument("--key",   default=None, help="Filter by key suffix.")
    p_lat.add_argument("--limit", type=int, default=50, help="Max rows. Default: 50.")

    p_rst = sub.add_parser("reset-stats", help="Reset stats (all keys or one).")
    p_rst.add_argument("key", nargs="?", default=None, help="Key to reset (omit for all).")

    args    = parser.parse_args()
    fn_map  = {
        "status":          cmd_status,
        "stats":           cmd_stats,
        "latency":         cmd_latency,
        "add":             cmd_add,
        "remove":          cmd_remove,
        "test":            cmd_test,
        "reset-cooldowns": cmd_reset_cooldowns,
        "reset-stats":     cmd_reset_stats,
    }
    fn_map[args.command](args)


if __name__ == "__main__":
    main()
