#!/usr/bin/env python3
"""DEV-ONLY calibration: tuned bot vs L3 (strong, zero-mistake) opponent.
Usage: python3 calibrate_tmp.py <game> <rate> <n> [seed]
Prints W/L/D from the tuned bot's perspective. DELETE before shipping."""
import random
import sys

import bots
import benchmark as bm

game, rate, n = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
seed = int(sys.argv[4]) if len(sys.argv) > 4 else 777

# NOTE: default arg values are bound at def time, so pass rate explicitly.
tuned = {
    "checkers": lambda s, side, chain=None: bots.checkers_move(
        s["board"], side, chain=chain, mistake_rate=rate),
    "connect4": lambda s, side, chain=None: bots.connect4_move(
        s, side, mistake_rate=rate),
    "tictactoe": lambda s, side, chain=None: bots.tictactoe_move(
        s, side, mistake_rate=rate),
    "poker": lambda hole, comm, street, legal, ctx: bots.poker_move(
        hole, comm, street, legal, ctx, mistake_rate=rate),
}[game]
opp = bm.OPPONENTS["L3"][game]

random.seed(seed)
b = bm.Bench(seed)
if game == "poker":
    res = b.play_poker(tuned, opp, n)
    avg = res["bot_chips"] / max(1, res["n"])
    print("[poker] rate=%.2f tuned vs L3: W %d / L %d (n=%d matches, %d hands)"
          "  avg %+.1f chips/match  L3 win%%=%.1f"
          % (rate, res["wins"], res["losses"], res["n"], res["hands"], avg,
             100.0 * res["losses"] / max(1, res["n"])), flush=True)
else:
    res = b.play_board(game, tuned, opp, n)
    print("[%s] rate=%.2f tuned vs L3: W %d / L %d / D %d (n=%d)  L3 win%%=%.1f"
          % (game, rate, res["wins"], res["losses"], res["draws"], res["n"],
             100.0 * res["losses"] / max(1, res["n"])), flush=True)
