"""
examples/demo.py — terminal demo for gemini-key-rotator.

Shows rotation, rate-limit cooldown, suspension, and status display
without making real API calls.

    python examples/demo.py
"""

import sys, os, time, logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from gemini_rotator import GeminiAPIRotator

logging.basicConfig(level=logging.WARNING)

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
DIM    = "\033[2m"

def header(text):
    print(f"\n{BOLD}{CYAN}{'─'*52}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{'─'*52}{RESET}")

def ok(text):    print(f"  {GREEN}✓{RESET} {text}")
def warn(text):  print(f"  {YELLOW}⚠{RESET} {text}")
def err(text):   print(f"  {RED}✗{RESET} {text}")
def info(text):  print(f"  {DIM}{text}{RESET}")

# ─────────────────────────────────────────────────────────────────────────────

keys = [
    "AIzaSyA1b2c3d4e5f6g7h8i9j0kLmNoPqRsT",
    "AIzaSyB9z8y7x6w5v4u3t2s1rQpOnMlKjIhG",
    "AIzaSyC3f4g5h6i7j8k9l0mNoPqRsTuVwXyZ",
    "AIzaSyD7e6d5c4b3a2z1y0xWvUtSrQpOnMlK",
]

header("gemini-key-rotator  v2.0")
rotator = GeminiAPIRotator(keys, rpm_per_key=15)
print(f"\n  {repr(rotator)}\n")

# ── Round-robin rotation ──────────────────────────────────────────────────────

header("Round-robin across 4 keys")
for i in range(8):
    k = rotator.get_next_working_key()
    rotator.record_success(k)
    ok(f"request {i+1:>2d}  →  ...{k[-8:]}")

# ── Rate limit + exponential backoff ─────────────────────────────────────────

header("429 RESOURCE_EXHAUSTED → exponential backoff")

k0 = keys[0]
for hit in range(1, 4):
    rotator.mark_rate_limited(k0)
    cd = rotator._cooling[k0] - time.time()
    warn(f"hit #{hit}  →  ...{k0[-8:]}  cooling {cd:.0f}s")

info(f"next {4} requests automatically skip the cooling key:")
for i in range(4):
    k = rotator.get_next_working_key()
    rotator.record_success(k)
    ok(f"request {i+1}  →  ...{k[-8:]}")

# ── Suspension ────────────────────────────────────────────────────────────────

header("403 CONSUMER_SUSPENDED → key blacklisted")
k1 = keys[1]
rotator.mark_suspended(k1)
err(f"...{k1[-8:]}  SUSPENDED — will be auto-rechecked on schedule")

# ── Status display ────────────────────────────────────────────────────────────

header("rotator.summary()")
print(f"\n  {rotator.summary()}\n")

header("rotator.masked_list()")
print()
for line in rotator.masked_list():
    print(f"  {line}")

header("rotator.status()")
print()
icons = {"active": f"{GREEN}🟢{RESET}", "cooling": f"{YELLOW}🟡{RESET}", "suspended": f"{RED}🔴{RESET}"}
for s in rotator.status():
    icon = icons.get(s["state"], "?")
    print(
        f"  {icon}  {BOLD}{s['masked']}{RESET}"
        f"  state={s['state']:<10}"
        f"  rpm={s['rpm_used']}/{s['rpm_limit']}"
        f"  cooldown={s['cooldown_remaining']:.0f}s"
        f"  ✓{s['stats']['success']} ⚠{s['stats']['rate_limit']} err:{s['stats']['error']}"
    )

# ── Hot-swap ──────────────────────────────────────────────────────────────────

header("Hot-swap keys at runtime")
new_key = "AIzaSyE1f2g3h4i5j6k7l8m9nOpQrStUvWx"
rotator.add_key(new_key)
ok(f"add_key(...)  →  total={rotator.total_count()}")
rotator.remove_key(new_key)
ok(f"remove_key(...)  →  total={rotator.total_count()}")

print(f"\n{DIM}{'─'*52}{RESET}\n")
