#!/usr/bin/env python3
"""Dedicated house-bot correctness tests (bots.py).

Covers: ttt takes wins / blocks losses; c4 takes wins / blocks losses;
checkers always returns a legal move incl. chains; poker sanitizer never
emits impossible amounts; blackjack known basic-strategy cases.
"""
import random

import app
import bots

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# --- tic-tac-toe: takes immediate win, blocks immediate loss (no mistakes)
s = {"board": [1, 1, 0, 2, 2, 0, 0, 0, 0]}  # X to move, wins at 2
check("ttt takes immediate win", bots.tictactoe_move(s, 0, mistake_rate=0) == {"cell": 2})
s = {"board": [2, 2, 0, 1, 0, 0, 0, 0, 1]}  # O threatens at 2, X must block
check("ttt blocks immediate loss", bots.tictactoe_move(s, 0, mistake_rate=0) == {"cell": 2})
# perfect vs perfect is always a draw
def ttt_play():
    board = [0] * 9
    turn = 0
    while True:
        w = app.ttt_winner({"board": board})
        if w is not None:
            return w
        if all(board):
            return None
        m = bots.tictactoe_move({"board": board}, turn, mistake_rate=0)
        board[m["cell"]] = turn + 1
        turn = 1 - turn
results = {ttt_play() for _ in range(20)}
check("ttt perfect vs perfect always draws", results == {None})

# --- connect four: takes wins, blocks losses (no mistakes)
def c4_state(col_lists, side):
    return {"cols": [list(c) for c in col_lists]}
st = c4_state([[1, 1, 1], [], [], [], [], [], []], 0)  # X wins playing col 0
check("c4 takes immediate win",
      bots.connect4_move(st, 0, mistake_rate=0) == {"column": 0})
st = c4_state([[2, 2, 2], [], [], [], [], [], []], 0)  # O threatens col 0
check("c4 blocks immediate loss",
      bots.connect4_move(st, 0, mistake_rate=0) == {"column": 0})
# mistake move is still a legal column
random.seed(7)
for _ in range(30):
    cols = [[random.choice([1, 2]) for _ in range(random.randint(0, 5))]
            for _ in range(7)]
    cols = [c[:6] for c in cols]
    legal = [c for c in range(7) if len(cols[c]) < 6]
    if not legal:
        continue
    m = bots.connect4_move({"cols": cols}, 0, mistake_rate=1.0)
    if not (0 <= m["column"] <= 6 and len(cols[m["column"]]) < 6):
        check("c4 mistake move always legal", False)
        break
else:
    check("c4 mistake move always legal", True)

# --- checkers: always a legal move, incl. chains (engine uses "b"/"w"/None)
random.seed(11)
ok = True
for _ in range(60):
    st = app.chk_new()
    b = st["board"]
    # scramble into a random midgame-ish position
    for _ in range(random.randint(0, 10)):
        r, c = random.randrange(8), random.randrange(8)
        if (r + c) % 2 == 1:
            b[r][c] = random.choice(["b", "w", "B", "W", None])
    side = random.randrange(2)
    legal = app.chk_legal_moves(b, side)
    m = bots.checkers_move(b, side, mistake_rate=1.0)
    if legal and m not in legal:
        ok = False
        break
    if not legal and m is not None:
        ok = False
        break
    if legal and m is None:
        ok = False
        break
check("checkers always returns legal move (mistakes on)", ok and True)
# forced capture is respected: side0 "b" at (4,3), side1 "w" at (3,4) ->
# "b" moves UP (decreasing row), captures to (2,5)
b = [[None] * 8 for _ in range(8)]
b[4][3] = "b"
b[3][4] = "w"
moves = app.chk_legal_moves(b, 0)
m = bots.checkers_move(b, 0, mistake_rate=0)
check("checkers takes forced capture",
      len(moves) > 0 and m is not None and
      abs(m["to"][0] - m["from"][0]) == 2)

# --- poker sanitizer: never impossible amounts
legal = [{"action": "check"},
         {"action": "bet", "min_amount": 10, "max_amount": 5}]  # engine quirk
m = bots.sanitize_poker_move(legal, {"action": "bet", "amount": 99})
check("poker sanitizer: impossible bet -> check",
      m == {"action": "check"})
legal = [{"action": "bet", "min_amount": 4, "max_amount": 50}]
m = bots.sanitize_poker_move(legal, {"action": "bet", "amount": 999})
check("poker sanitizer: clamps bet to max", m == {"action": "bet", "amount": 50})
m = bots.sanitize_poker_move(legal, {"action": "bet", "amount": 1})
check("poker sanitizer: clamps bet to min", m == {"action": "bet", "amount": 4})
m = bots.sanitize_poker_move([{"action": "fold"}], {"action": "raise", "amount": 10})
check("poker sanitizer: illegal action -> fold", m == {"action": "fold"})
# poker bot itself returns a legal action
legal = [{"action": "check"}, {"action": "bet", "min_amount": 2, "max_amount": 60}]
ctx = {"pot": 10, "to_call": 0, "stack": 100, "my_bet": 0, "is_button": True, "bb": 2}
for _ in range(50):
    m = bots.sanitize_poker_move(
        legal, bots.poker_move(["As", "Kd"], ["2c", "7h", "Qd"], "flop",
                               legal, ctx))
    if m["action"] not in ("check", "bet"):
        check("poker bot+sanitizer always legal", False)
        break
else:
    check("poker bot+sanitizer always legal", True)

# --- blackjack: basic strategy spot checks (S17, no split, double allowed)
def bj(hand, up):
    return bots.blackjack_move(hand, up, [{"action": "hit"}, {"action": "stand"},
                                          {"action": "double"}])["action"]
check("bj 11 vs 10 doubles", bj(["6h", "5d"], "Th") == "double")
check("bj hard 16 vs 10 hits", bj(["9h", "7d"], "Th") == "hit")
check("bj hard 17 vs A stands", bj(["9h", "8d"], "Ah") == "stand")
check("bj soft 18 (A7) vs 6 doubles", bj(["Ah", "7d"], "6h") == "double")
check("bj soft 18 (A7) vs 9 hits", bj(["Ah", "7d"], "9h") == "hit")
check("bj pair-less 12 vs 4 stands", bj(["9h", "3d"], "4h") == "stand")
check("bj 9 vs 3 doubles", bj(["6h", "3d"], "3h") == "double")

print()
if fails:
    print("FAILURES:", fails)
    raise SystemExit(1)
print("ALL BOT TESTS PASSED")
