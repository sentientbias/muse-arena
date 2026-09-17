#!/usr/bin/env python3
"""Bot benchmark harness for Muse Arena.

Drives the REAL Arena engines in-process (temp sqlite DB, like the test
suite) with the bot on one side and a scripted opponent on the other.

  python3 benchmark.py --mode before            # v1 baselines
  python3 benchmark.py --mode after             # toughened bots (bots.py)
  python3 benchmark.py --mode after --games c4,ttt --n-c4 50

Modes:
  before — checkers: the shipped app.chk_bot_move (negamax d4, 2s budget).
           c4/ttt/poker/blackjack: NO shipped bot existed, so v1 is a
           minimal greedy bot written here (labeled "v1 baseline").
  after  — the toughened bots in bots.py.

Opponents (fixed across both modes):
  L0 random  — uniform random legal move.
  L1 greedy  — 1-ply heuristic (board games); calling station (poker);
               hit<=16/stand>=17 (blackjack).
  L2 decent  — stronger 1-ply + tactics (board games); tight-aggressive-lite
               with pot odds (poker); full basic strategy (blackjack).

Only real, executed games are reported. No invented numbers.
"""
import argparse
import itertools
import json
import os
import random
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)  # Arena reads questions.json relative to cwd

import app
import bots
from app import Arena, ApiError

# ---------------------------------------------------------------------------
# card helpers shared by baselines/opponents
# ---------------------------------------------------------------------------

def _best_n(cards):
    """Best-5 evalu for 5/6/7 card lists. Returns (rank, tiebreak)."""
    if len(cards) == 5:
        r, t, _n = app.poker_eval5([app.parse_card(c) for c in cards])
        return r, t
    best = None
    for combo in itertools.combinations(cards, 5):
        r, t, _n = app.poker_eval5([app.parse_card(c) for c in combo])
        if best is None or (r, t) > best:
            best = (r, t)
    return best


def _has_flush_draw(cards):
    suits = {}
    for c in cards:
        suits[c[1]] = suits.get(c[1], 0) + 1
    return max(suits.values()) >= 4


def _int(x, dflt):
    try:
        return int(x)
    except (TypeError, ValueError):
        return dflt


sanitize_poker = bots.sanitize_poker_move


def poker_ctx(state, side):
    return {
        "pot": state["pot"],
        "to_call": state["current_bet"] - state["bets"][side],
        "stack": state["stacks"][side],
        "my_bet": state["bets"][side],
        "is_button": state["button"] == side,
        "bb": state["bb"],
        "hand_no": state["hand_no"],
    }


# ---------------------------------------------------------------------------
# v1 baselines ("before") — minimal bots; none shipped for c4/ttt/poker/bj
# ---------------------------------------------------------------------------

def v1_checkers(state, side, chain=None):
    return app.chk_bot_move(state["board"], side, chain=chain)


def v1_c4(state, side):
    cols = [list(c) for c in state["cols"]]
    best, bests = -10 ** 9, None
    for c in range(7):
        if len(cols[c]) >= 6:
            continue
        cols[c].append(side + 1)
        won = bots._c4_won_at(cols, c)
        cols[c].pop()
        s = (100000 if won else 0) + bots._c4_eval(
            [list(x) for x in state["cols"][:c]] +
            [state["cols"][c] + [side + 1]] +
            [list(x) for x in state["cols"][c + 1:]], side)
        if s > best:
            best, bests = s, c
    return {"column": bests}


def v1_ttt(state, side):
    board = state["board"]
    me = side + 1
    # win > block > random
    for i, v in enumerate(board):
        if v:
            continue
        b = list(board)
        b[i] = me
        if app.ttt_winner({"board": b}) == side:
            return {"cell": i}
    for i, v in enumerate(board):
        if v:
            continue
        b = list(board)
        b[i] = 3 - me
        if app.ttt_winner({"board": b}) == 1 - side:
            return {"cell": i}
    return {"cell": random.choice([i for i, v in enumerate(board) if not v])}


def v1_poker(hole, community, street, legal, ctx):
    by = {m["action"]: m for m in legal}
    to_call, pot, stack = ctx["to_call"], ctx["pot"], ctx["stack"]
    chen = bots._chen(hole)
    if street == "preflop":
        if to_call == 0:
            if chen >= 9 and "bet" in by:
                m = by["bet"]
                return {"action": "bet",
                        "amount": max(m["min_amount"],
                                      min(m["max_amount"], 3 * ctx["bb"]))}
            return {"action": "check"}
        if chen >= 9:
            return {"action": "call", "amount": min(stack, to_call)}
        return {"action": "fold"}
    rank, _t = _best_n(hole + community)
    if to_call == 0:
        if rank >= 2 and "bet" in by:
            m = by["bet"]
            return {"action": "bet",
                    "amount": max(m["min_amount"],
                                  min(m["max_amount"], pot // 2 or m["min_amount"]))}
        return {"action": "check"}
    if rank >= 2:
        return {"action": "call", "amount": min(stack, to_call)}
    if rank == 1 and to_call <= pot // 4:
        return {"action": "call", "amount": min(stack, to_call)}
    return {"action": "fold"}


def v1_bj(hand, dealer_up, legal):
    total, _soft = app.bj_total(hand)
    return {"action": "hit" if total <= 16 else "stand"}


# ---------------------------------------------------------------------------
# opponents
# ---------------------------------------------------------------------------

def opp_random_board(kind):
    def f(state, side, chain=None):
        if kind == "checkers":
            legal = app.chk_legal_moves(state["board"], side, chain)
        elif kind == "connect4":
            legal = app.c4_legal(state)
        else:
            legal = app.ttt_legal(state)
        return random.choice(legal)
    return f


def opp_random_poker(hole, community, street, legal, ctx):
    m = random.choice(legal)
    a = m["action"]
    if a == "bet":
        return {"action": "bet", "amount": m["min_amount"]}
    if a == "raise":
        return {"action": "raise", "amount": m["min_total"]}
    if a == "call":
        return {"action": "call", "amount": m["amount"]}
    return {"action": a}


def opp_random_bj(hand, dealer_up, legal):
    return {"action": random.choice(legal)["action"]}


def opp_greedy_board(kind):
    def f(state, side, chain=None):
        if kind == "checkers":
            legal = app.chk_legal_moves(state["board"], side, chain)
            return max(legal, key=lambda m: app._chk_eval(
                app.chk_apply(state["board"], side, m)[0], side))
        if kind == "connect4":
            return v1_c4(state, side)
        return v1_ttt(state, side)
    return f


def opp_greedy_poker(hole, community, street, legal, ctx):
    # calling station: never bets, never folds
    by = {m["action"]: m for m in legal}
    if ctx["to_call"] == 0:
        return {"action": "check"}
    return {"action": "call", "amount": by["call"]["amount"]}


def opp_greedy_bj(hand, dealer_up, legal):
    return v1_bj(hand, dealer_up, legal)


def opp_decent_board(kind):
    def f(state, side, chain=None):
        if kind == "checkers":
            legal = app.chk_legal_moves(state["board"], side, chain)
            return max(legal, key=lambda m: bots._chk_eval2(
                app.chk_apply(state["board"], side, m)[0], side))
        if kind == "connect4":
            cols = [list(c) for c in state["cols"]]
            # win now
            for c in range(7):
                if len(cols[c]) >= 6:
                    continue
                cols[c].append(side + 1)
                won = bots._c4_won_at(cols, c)
                cols[c].pop()
                if won:
                    return {"column": c}
            # block immediate loss
            for c in range(7):
                if len(cols[c]) >= 6:
                    continue
                cols[c].append(2 - side)
                won = bots._c4_won_at(cols, c)
                cols[c].pop()
                if won:
                    return {"column": c}
            return v1_c4(state, side)
        # tictactoe: win > block > center > corner > side (near-optimal)
        board = state["board"]
        me = side + 1
        for i, v in enumerate(board):
            if v:
                continue
            b = list(board)
            b[i] = me
            if app.ttt_winner({"board": b}) == side:
                return {"cell": i}
        for i, v in enumerate(board):
            if v:
                continue
            b = list(board)
            b[i] = 3 - me
            if app.ttt_winner({"board": b}) == 1 - side:
                return {"cell": i}
        for i in (4, 0, 2, 6, 8, 1, 3, 5, 7):
            if not board[i]:
                return {"cell": i}
        raise AssertionError("no ttt move")
    return f


def opp_decent_poker(hole, community, street, legal, ctx):
    by = {m["action"]: m for m in legal}
    to_call, pot, stack = ctx["to_call"], ctx["pot"], ctx["stack"]
    chen = bots._chen(hole)

    def open_bet(frac):
        m = by.get("bet")
        if not m:
            return {"action": "check"}
        amt = max(m["min_amount"], min(m["max_amount"], int(pot * frac)
                                       or m["min_amount"]))
        return {"action": "bet", "amount": amt}

    if street == "preflop":
        if to_call == 0:
            if chen >= 8.5 and "bet" in by:
                m = by["bet"]
                return {"action": "bet",
                        "amount": max(m["min_amount"],
                                      min(m["max_amount"], 3 * ctx["bb"]))}
            return {"action": "check"}
        if chen >= 8.5:
            return {"action": "call", "amount": min(stack, to_call)}
        if chen >= 6.5 and to_call <= 3 * ctx["bb"]:
            return {"action": "call", "amount": min(stack, to_call)}
        return {"action": "fold"}
    rank, _t = _best_n(hole + community)
    draw = _has_flush_draw(hole + community)
    if to_call == 0:
        if rank >= 2:
            return open_bet(0.5)
        return {"action": "check"}
    odds = to_call / (pot + to_call)
    if rank >= 2:
        return {"action": "call", "amount": min(stack, to_call)}
    if rank == 1 and odds <= 0.25:
        return {"action": "call", "amount": min(stack, to_call)}
    if draw and odds <= 0.20:
        return {"action": "call", "amount": min(stack, to_call)}
    return {"action": "fold"}


def opp_decent_bj(hand, dealer_up, legal):
    return bots.blackjack_move(hand, dealer_up, legal)  # basic strategy


# L3: strong simulated HUMAN (not superhuman). The same engine as the bot
# but with a HIGHER human-like slip rate — the "strong human" who sees most
# tactics but doesn't always find the best move. The shipped bot's lower
# slip rate is its edge; target: L3 scrapes 5-15% wins ("I almost had it").
#   checkers/connect4: full-depth search, mistake_rate above the bot's
#   tictactoe: perfect (the game is trivial; perfect = strong human)
#   poker/blackjack: disciplined mirror of the bot itself
L3_CHECKERS_MISTAKE = 0.15
L3_CONNECT4_MISTAKE = 0.12


def _l3_checkers(s, side, chain=None):
    return bots.checkers_move(s["board"], side, chain=chain,
                              mistake_rate=L3_CHECKERS_MISTAKE)


def _l3_connect4(s, side, chain=None):
    return bots.connect4_move(s, side, mistake_rate=L3_CONNECT4_MISTAKE)


def _l3_tictactoe(s, side, chain=None):
    return bots.tictactoe_move(s, side, mistake_rate=0.0)


def _l3_poker(hole, comm, street, legal, ctx):
    return bots.poker_move(hole, comm, street, legal, ctx, mistake_rate=0.0)


def _l3_bj(hand, dup, legal):
    return bots.blackjack_move(hand, dup, legal)  # basic strategy mirror


OPPONENTS = {
    "L0": {"checkers": opp_random_board("checkers"),
           "connect4": opp_random_board("connect4"),
           "tictactoe": opp_random_board("tictactoe"),
           "poker": opp_random_poker, "blackjack": opp_random_bj},
    "L1": {"checkers": opp_greedy_board("checkers"),
           "connect4": opp_greedy_board("connect4"),
           "tictactoe": opp_greedy_board("tictactoe"),
           "poker": opp_greedy_poker, "blackjack": opp_greedy_bj},
    "L2": {"checkers": opp_decent_board("checkers"),
           "connect4": opp_decent_board("connect4"),
           "tictactoe": opp_decent_board("tictactoe"),
           "poker": opp_decent_poker, "blackjack": opp_decent_bj},
    "L3": {"checkers": _l3_checkers, "connect4": _l3_connect4,
           "tictactoe": _l3_tictactoe, "poker": _l3_poker,
           "blackjack": _l3_bj},
}

BEFORE_BOTS = {"checkers": v1_checkers, "connect4": v1_c4, "tictactoe": v1_ttt,
               "poker": v1_poker, "blackjack": v1_bj}
AFTER_BOTS = {"checkers": lambda s, side, chain=None: bots.checkers_move(
                  s["board"], side, chain=chain),
              "connect4": lambda s, side, chain=None: bots.connect4_move(s, side),
              "tictactoe": lambda s, side, chain=None: bots.tictactoe_move(s, side),
              "poker": lambda hole, comm, street, legal, ctx: bots.poker_move(
                  hole, comm, street, legal, ctx),
              "blackjack": lambda hand, dup, legal: bots.blackjack_move(
                  hand, dup, legal)}


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

class Bench:
    def __init__(self, seed):
        self.db = tempfile.mktemp(suffix=".db")
        self.a = Arena(self.db)
        self.bot = self._player("BenchBot")
        self.opp = self._player("BenchOpp")
        room = self.a.create_room(self.bot, "bench-%d" % seed)
        self.room = room["id"]
        self.a.join_room(self.opp, self.room)

    def _player(self, name):
        reg = self.a.register(name)
        return self.a.auth(reg["token"])

    def _pdict(self, p):
        return {"id": p["id"], "name": p["name"], "is_human": 0}

    def _new_game(self, kind, bot_first):
        creator, other = ((self.bot, self.opp) if bot_first
                          else (self.opp, self.bot))
        g = self.a.new_board_game(creator, self.room, kind, other["name"])
        gid = g["id"]
        players = json.loads(self.a._board_row(gid)["players_json"])
        bot_side = players.index(self.bot["id"])
        pd = {0: self._pdict(self.bot if bot_side == 0 else self.opp),
              1: self._pdict(self.bot if bot_side == 1 else self.opp)}
        return gid, players, bot_side, pd

    def play_board(self, kind, bot_fn, opp_fn, n):
        res = {"wins": 0, "losses": 0, "draws": 0, "errors": 0,
               "bot_ms": 0.0, "bot_moves": 0, "n": n}
        for i in range(n):
            gid, players, bot_side, pd = self._new_game(kind, i % 2 == 0)
            try:
                for _ply in range(600):
                    row = self.a._board_row(gid)
                    if row["status"] != "open":
                        break
                    state = json.loads(row["state_json"])
                    side = players.index(row["turn_pid"])
                    is_bot = side == bot_side
                    fn = bot_fn if is_bot else opp_fn
                    t0 = time.time()
                    if kind == "checkers":
                        chain = (tuple(state["chain"])
                                 if state.get("chain") else None)
                        legal = app.chk_legal_moves(state["board"], side, chain)
                        mv = fn(state, side, chain)
                    elif kind == "connect4":
                        legal = app.c4_legal(state)
                        mv = fn(state, side)
                    else:
                        legal = app.ttt_legal(state)
                        mv = fn(state, side)
                    if is_bot:
                        res["bot_ms"] += (time.time() - t0) * 1000
                        res["bot_moves"] += 1
                    if mv not in legal:  # never crash the game on a bad bot
                        res["errors"] += 1
                        mv = random.choice(legal)
                    self.a.make_move(pd[side], gid, mv)
                row = self.a._board_row(gid)
                wid = row["winner_id"]
                if wid == self.bot["id"]:
                    res["wins"] += 1
                elif wid is None:
                    res["draws"] += 1
                else:
                    res["losses"] += 1
            except ApiError as e:
                res["errors"] += 1
        return res

    def play_poker(self, bot_fn, opp_fn, n_matches):
        res = {"wins": 0, "losses": 0, "draws": 0, "errors": 0,
               "bot_ms": 0.0, "bot_moves": 0, "hands": 0, "n": n_matches,
               "bot_chips": 0}
        for i in range(n_matches):
            gid, players, bot_side, pd = self._new_game("poker", i % 2 == 0)
            try:
                for _d in range(800):
                    row = self.a._board_row(gid)
                    if row["status"] != "open":
                        break
                    state = json.loads(row["state_json"])
                    side = players.index(row["turn_pid"])
                    pid = players[side]
                    hole = self.a._secret_get(gid, state["hand_no"], pid)
                    legal = self.a._poker_legal(state, side)
                    ctx = poker_ctx(state, side)
                    fn = bot_fn if side == bot_side else opp_fn
                    t0 = time.time()
                    mv = sanitize_poker(
                        legal, fn(hole, state["community"],
                                 state["street"], legal, ctx))
                    if side == bot_side:
                        res["bot_ms"] += (time.time() - t0) * 1000
                        res["bot_moves"] += 1
                    self.a.make_move(pd[side], gid, mv)
                row = self.a._board_row(gid)
                state = json.loads(row["state_json"])
                res["hands"] += state.get("hand_no", 0)
                res["bot_chips"] += state["stacks"][bot_side] - 100
                wid = row["winner_id"]
                if wid == self.bot["id"]:
                    res["wins"] += 1
                elif wid is None:
                    res["draws"] += 1
                else:
                    res["losses"] += 1
            except ApiError:
                res["errors"] += 1
        return res

    def play_blackjack(self, bot_fn, opp_fn, n_matches):
        res = {"wins": 0, "losses": 0, "draws": 0, "errors": 0,
               "bot_ms": 0.0, "bot_moves": 0, "hands": 0, "n": n_matches,
               "bot_chips": 0, "opp_chips": 0}
        for i in range(n_matches):
            gid, players, bot_side, pd = self._new_game("blackjack",
                                                        i % 2 == 0)
            try:
                for _d in range(400):
                    row = self.a._board_row(gid)
                    if row["status"] != "open":
                        break
                    state = json.loads(row["state_json"])
                    side = players.index(row["turn_pid"])
                    legal = self.a._bj_legal(state, side)
                    legal_actions = {m["action"] for m in legal}
                    fn = bot_fn if side == bot_side else opp_fn
                    t0 = time.time()
                    mv = fn(state["hands"][side], state["dealer_up"], legal)
                    if side == bot_side:
                        res["bot_ms"] += (time.time() - t0) * 1000
                        res["bot_moves"] += 1
                    if mv.get("action") not in legal_actions:
                        res["errors"] += 1
                        mv = {"action": "stand"}
                    self.a.make_move(pd[side], gid, mv)
                row = self.a._board_row(gid)
                state = json.loads(row["state_json"])
                res["hands"] += state.get("hand_no", 0)
                bd = state["stacks"][bot_side] - 100
                od = state["stacks"][1 - bot_side] - 100
                res["bot_chips"] += bd
                res["opp_chips"] += od
                if bd > od:
                    res["wins"] += 1
                elif bd == od:
                    res["draws"] += 1
                else:
                    res["losses"] += 1
            except ApiError:
                res["errors"] += 1
        return res


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def fmt_board(res):
    n = res["n"]
    avg_ms = res["bot_ms"] / max(1, res["bot_moves"])
    return ("W %d / L %d / D %d  (n=%d, err=%d)  bot %.1f ms/move"
            % (res["wins"], res["losses"], res["draws"], n,
               res["errors"], avg_ms))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["before", "after", "final"], required=True)
    ap.add_argument("--games", default="checkers,connect4,tictactoe,poker,blackjack")
    ap.add_argument("--levels", default="L0,L1,L2")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--n-checkers", type=int, default=24)
    ap.add_argument("--n-connect4", type=int, default=150)
    ap.add_argument("--n-tictactoe", type=int, default=300)
    ap.add_argument("--n-poker", type=int, default=30)
    ap.add_argument("--n-blackjack", type=int, default=100)
    args = ap.parse_args()

    random.seed(args.seed)

    botset = BEFORE_BOTS if args.mode == "before" else AFTER_BOTS
    games = [g.strip() for g in args.games.split(",")]
    levels = [lv.strip() for lv in args.levels.split(",")]
    counts = {"checkers": args.n_checkers, "connect4": args.n_connect4,
              "tictactoe": args.n_tictactoe, "poker": args.n_poker,
              "blackjack": args.n_blackjack}

    print("mode=%s seed=%d" % (args.mode, args.seed), flush=True)
    t_start = time.time()
    for gi, game in enumerate(games):
        for li, lv in enumerate(levels):
            # deterministic per-block stream (same for before/after runs)
            random.seed(args.seed * 1000 + gi * 10 + li)
            b = Bench(args.seed)
            n = counts[game]
            opp = OPPONENTS[lv][game]
            t0 = time.time()
            if game in ("checkers", "connect4", "tictactoe"):
                res = b.play_board(game, botset[game], opp, n)
                print("[%s] bot vs %s: %s  (%.0fs)"
                      % (game, lv, fmt_board(res), time.time() - t0),
                      flush=True)
            elif game == "poker":
                res = b.play_poker(botset[game], opp, n)
                avg = res["bot_chips"] / max(1, res["n"])
                print("[poker] bot vs %s: W %d / L %d / D %d (n=%d matches,"
                      " %d hands, err=%d)  avg %+.1f chips/match  bot %.1f ms/move"
                      " (%.0fs)"
                      % (lv, res["wins"], res["losses"], res["draws"], res["n"],
                         res["hands"], res["errors"], avg,
                         res["bot_ms"] / max(1, res["bot_moves"]),
                         time.time() - t0), flush=True)
            else:
                res = b.play_blackjack(botset[game], opp, n)
                avg = res["bot_chips"] / max(1, res["n"])
                print("[blackjack] bot vs %s: ahead %d / behind %d / tied %d"
                      " (n=%d matches, %d hands, err=%d)  avg %+.1f chips/match"
                      "  bot %.1f ms/move (%.0fs)"
                      % (lv, res["wins"], res["losses"], res["draws"], res["n"],
                         res["hands"], res["errors"], avg,
                         res["bot_ms"] / max(1, res["bot_moves"]),
                         time.time() - t0), flush=True)
    print("total %.0fs" % (time.time() - t_start), flush=True)


if __name__ == "__main__":
    main()
