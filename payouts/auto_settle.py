#!/usr/bin/env python3
"""arena auto-settler wrapper — safety-railed live settlement.

Safety rails (checked BEFORE any money moves):
  1. Non-blocking lock: if another run is in flight, exit quietly.
  2. Dry-run first: parse its 'total outflow' line.
     - dry-run failure / missing total line  -> settle NOTHING, report.
     - total outflow > $25.00                -> settle NOTHING, report.
     - any 'game #5' in the plan             -> settle NOTHING, report.
  3. Only then run `settle.py --remote --live`.

settle.py itself excludes MANUAL_SETTLEMENTS (games 5, 8, 14) and the
admin API is per-stake idempotent, so a retried run can never double-pay.

Logs every run to hidden_files/auto_settle.log. The private key never
leaves settle.py and is never printed.
"""
import fcntl
import os
import re
import subprocess
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PY = os.path.join(REPO, ".venv", "bin", "python")
SETTLE = os.path.join(REPO, "payouts", "settle.py")
LOG = os.path.join(REPO, "hidden_files", "auto_settle.log")
LOCK_PATH = "/tmp/arena-auto-settler.lock"

OUTFLOW_CAP_USD = Decimal("25.00")
FORBIDDEN_GAMES = {5}


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[auto-settle] WARN: could not append to log: {e}",
              file=sys.stderr)


def run_settle(*args, timeout=1500):
    return subprocess.run([PY, SETTLE, *args], capture_output=True,
                          text=True, timeout=timeout, cwd=REPO)


def main():
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[auto-settle] another run in flight — skipping")
        return 0

    log("run start")
    try:
        dry = run_settle("--remote")
    except Exception as e:
        log(f"ABORT: dry-run crashed ({e}) — settled nothing")
        return 1
    sys.stdout.write(dry.stdout)
    sys.stderr.write(dry.stderr)
    if dry.returncode != 0:
        log(f"ABORT: dry-run exited {dry.returncode} — settled nothing")
        return 1
    m = re.search(r"total outflow: \$([\d,]+\.\d{2}) USDC", dry.stdout)
    if not m:
        log("ABORT: could not parse 'total outflow' from dry-run — "
            "settled nothing")
        return 1
    outflow = Decimal(m.group(1).replace(",", ""))
    games = sorted({int(g) for g in re.findall(r"^game #(\d+)",
                                               dry.stdout, re.M)})
    log(f"dry-run: games={games} outflow=${outflow}")
    if games and set(games) & FORBIDDEN_GAMES:
        log(f"ABORT: forbidden game in plan {games} — settled nothing")
        return 1
    if outflow > OUTFLOW_CAP_USD:
        log(f"ABORT: outflow ${outflow} exceeds ${OUTFLOW_CAP_USD} cap — "
            "settled nothing, needs human review")
        return 1
    if not games:
        log("nothing to do — $0.00 owed")
        return 0

    log("rails passed — running LIVE settlement")
    try:
        live = run_settle("--remote", "--live")
    except Exception as e:
        log(f"ERROR: live run crashed ({e}) — check state manually")
        return 1
    sys.stdout.write(live.stdout)
    sys.stderr.write(live.stderr)
    m2 = re.search(r"total outflow: \$([\d,]+\.\d{2}) USDC", live.stdout)
    paid = m2.group(1) if m2 else "?"
    log(f"live run exit={live.returncode} attempted=${paid}")
    return live.returncode


if __name__ == "__main__":
    sys.exit(main())
