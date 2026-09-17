#!/usr/bin/env python3
"""Muse Arena — house-bot move logic for all five games.

Pure functions (no DB, no network) so they can be benchmarked in-process
and reused by the server. Each `*_move` takes the public game state plus
whatever private info the bot legitimately knows (hole cards, etc.) and
returns a move dict in exactly the shape `Arena.make_move` expects.

Response budget: every bot is designed to answer well under a second —
iterative deepening with a hard wall-clock deadline for the search games,
table lookup / bounded Monte Carlo for the card games.
"""
import itertools
import math
import random
import time

import app
from app import (bj_total, c4_apply, c4_legal, chk_apply, chk_legal_moves,
                 parse_card, poker_best7)

# ---------------------------------------------------------------------------
# shared: timeout
# ---------------------------------------------------------------------------

class _Timeout(Exception):
    pass


# ---------------------------------------------------------------------------
# checkers (english draughts) — negamax, alpha-beta, iterative deepening
# ---------------------------------------------------------------------------
# Toughness upgrades vs the original app.chk_bot_move:
#   * deeper: depth 6 (was 4) inside a tighter 0.8s budget (was 2.0s)
#   * richer eval: back-rank guard bonus, edge-file penalty for men,
#     stronger king value, all cheap piece-square terms
#   * capture-extension at leaf nodes (no standing pat mid-tactic)
#   * forced multi-jump continuations don't consume depth
#   * move ordering: captures, then promotions, then static-eval order
#   * sane fallback: best 1-ply eval move instead of moves[0]

_CHK_MAN = 100
_CHK_KING = 172


def _chk_eval2(board, side):
    """Centipawn score from `side`'s perspective."""
    s = 0
    for r in range(8):
        for c in range(8):
            p = board[r][c]
            if not p:
                continue
            v = (_CHK_MAN if p in ("b", "w") else _CHK_KING)
            v += (3 - abs(3.5 - c)) * 2          # central control
            if p in ("b", "B"):                   # side 0 wants row 0
                if p == "b":
                    v += (7 - r) * 7              # advancement
                    if r == 7:
                        v += 12                   # back-rank guard
                    if c in (0, 7):
                        v -= 4                    # edge men are passive
                s += v if side == 0 else -v
            else:                                 # side 1 wants row 7
                if p == "w":
                    v += r * 7
                    if r == 0:
                        v += 12
                    if c in (0, 7):
                        v -= 4
                s += v if side == 1 else -v
    return s


def _chk_order(board, side, moves):
    """Captures first, then promotions, then static-eval order (best for
    alpha-beta pruning)."""
    scored = []
    for m in moves:
        nb, _cap, prom, _ch = chk_apply(board, side, m)
        key = (0 if abs(m["to"][0] - m["from"][0]) == 2 else 1,
               0 if prom else 1,
               -_chk_eval2(nb, side))
        scored.append((key, m))
    scored.sort(key=lambda t: t[0])
    return [m for _, m in scored]


def _chk_negamax2(board, side, depth, alpha, beta, deadline, chain=None):
    if time.time() > deadline:
        raise _Timeout()
    moves = chk_legal_moves(board, side, chain)
    if not moves:
        return -50000 - depth, None
    if depth <= 0:
        # capture extension: never stand pat in the middle of a tactic
        if any(abs(m["to"][0] - m["from"][0]) == 2 for m in moves):
            depth = 2
        else:
            return _chk_eval2(board, side), None
    best, bestm = -10 ** 9, None
    for m in _chk_order(board, side, moves):
        nb, _cap, _prom, chain2 = chk_apply(board, side, m)
        if chain2:  # forced multi-jump: same side, depth not consumed
            val, _ = _chk_negamax2(nb, side, depth, alpha, beta, deadline,
                                   chain=tuple(chain2))
        else:
            val, _ = _chk_negamax2(nb, 1 - side, depth - 1,
                                   -beta, -alpha, deadline)
            val = -val
        if val > best:
            best, bestm = val, m
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break
    return best, bestm


def checkers_move(board, side, chain=None, time_budget=0.8, max_depth=6):
    """House-bot checkers move. Returns a legal move dict."""
    moves = chk_legal_moves(board, side, chain)
    if not moves:
        return None
    if len(moves) == 1:
        return moves[0]
    # sane fallback: best 1-ply move by eval (never an arbitrary moves[0])
    best = max(moves,
               key=lambda m: _chk_eval2(chk_apply(board, side, m)[0], side))
    deadline = time.time() + time_budget
    try:
        for depth in range(1, max_depth + 1):
            _val, m = _chk_negamax2(board, side, depth,
                                    -10 ** 9, 10 ** 9, deadline)
            if m:
                best = m
    except _Timeout:
        pass
    return best


# ---------------------------------------------------------------------------
# tic-tac-toe — perfect minimax (never loses; wins vs any mistake)
# ---------------------------------------------------------------------------

_TTT_ORDER = (4, 0, 2, 6, 8, 1, 3, 5, 7)  # center, corners, sides


def _ttt_minimax(board, side, alpha, beta):
    """board: list of 9 (0 empty, 1 = side0, 2 = side1). Returns (score, move)
    with score from `side`'s perspective: 1 win, 0 draw, -1 loss."""
    from app import ttt_winner
    w = ttt_winner({"board": board})
    if w is not None:
        return (1 if w == side else -1), None
    if all(board):
        return 0, None
    # side 0 (X) always moves first: equal stone counts -> side 0 to move
    n0 = sum(1 for v in board if v == 1)
    n1 = sum(1 for v in board if v == 2)
    turn = 0 if n0 == n1 else 1
    best, bestm = (-2 if turn == side else 2), None
    for i in _TTT_ORDER:
        if board[i]:
            continue
        board[i] = turn + 1
        val, _ = _ttt_minimax(board, side, alpha, beta)
        board[i] = 0
        if turn == side:  # maximizing
            if val > best:
                best, bestm = val, i
            alpha = max(alpha, best)
        else:  # minimizing
            if val < best:
                best, bestm = val, i
            beta = min(beta, best)
        if beta <= alpha:
            break
    return best, bestm


def tictactoe_move(state, side):
    """Perfect tic-tac-toe: never loses, punishes every mistake."""
    board = list(state["board"])
    legal = [i for i, v in enumerate(board) if v == 0]
    if not legal:
        return None
    if len(legal) == 1:
        return {"cell": legal[0]}
    _score, cell = _ttt_minimax(board, side, -2, 2)
    return {"cell": cell if cell is not None else legal[0]}


# ---------------------------------------------------------------------------
# connect four — negamax + alpha-beta + transposition table, iterative
# deepening inside a wall-clock budget
# ---------------------------------------------------------------------------

_C4_WINDOWS = []
for _r in range(6):
    for _c in range(7):
        if _c + 3 < 7:
            _C4_WINDOWS.append([(_r, _c + i) for i in range(4)])
        if _r + 3 < 6:
            _C4_WINDOWS.append([(_r + i, _c) for i in range(4)])
        if _r + 3 < 6 and _c + 3 < 7:
            _C4_WINDOWS.append([(_r + i, _c + i) for i in range(4)])
        if _r + 3 < 6 and _c - 3 >= 0:
            _C4_WINDOWS.append([(_r + i, _c - i) for i in range(4)])

_C4_ORDER = (3, 2, 4, 1, 5, 0, 6)  # center-out


def _c4_grid(cols):
    g = [[0] * 7 for _r in range(6)]
    for _c in range(7):
        for _r, v in enumerate(cols[_c]):
            g[_r][_c] = v
    return g


def _c4_won_at(cols, col):
    """Did the token just dropped in `col` complete four?"""
    r = len(cols[col]) - 1
    v = cols[col][r]
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        n = 1
        for sgn in (1, -1):
            rr, cc = r + dr * sgn, col + dc * sgn
            while 0 <= rr < 6 and 0 <= cc < 7 and rr < len(cols[cc]) \
                    and cols[cc][rr] == v:
                n += 1
                rr += dr * sgn
                cc += dc * sgn
        if n >= 4:
            return True
    return False


def _c4_eval(cols, side):
    """Window-count eval from `side`'s perspective."""
    me, op = side + 1, 2 - side
    g = _c4_grid(cols)
    s = 0
    for w in _C4_WINDOWS:
        m = o = 0
        for r, c in w:
            v = g[r][c]
            if v == me:
                m += 1
            elif v == op:
                o += 1
        if m and o:
            continue
        if m == 4:
            s += 1000000
        elif m == 3:
            s += 120
        elif m == 2:
            s += 12
        elif m == 1:
            s += 1
        elif o == 4:
            s -= 1000000
        elif o == 3:
            s -= 140  # losing threats hurt slightly more than ours help
        elif o == 2:
            s -= 12
        elif o == 1:
            s -= 1
    s += sum(6 for v in cols[3] if v == me)
    s -= sum(6 for v in cols[3] if v == op)
    return s


def _c4_negamax(cols, side, depth, alpha, beta, deadline, tt):
    if time.time() > deadline:
        raise _Timeout()
    pos = tuple(tuple(c) for c in cols)
    moves = [c for c in _C4_ORDER if len(cols[c]) < 6]
    if not moves:
        return 0, None
    # immediate win shortcut
    for c in moves:
        cols[c].append(side + 1)
        won = _c4_won_at(cols, c)
        cols[c].pop()
        if won:
            return 1000000 + depth, {"column": c}
    if depth <= 0:
        return _c4_eval(cols, side), None
    best, bestm = -10 ** 9, None
    tt_move = tt.get((pos, side))  # best move seen at this position: try first
    ordered = sorted(moves, key=lambda c: (c != tt_move, abs(3 - c)))
    for c in ordered:
        cols[c].append(side + 1)
        val, _ = _c4_negamax(cols, 1 - side, depth - 1, -beta, -alpha,
                             deadline, tt)
        cols[c].pop()
        val = -val
        if val > best:
            best, bestm = val, {"column": c}
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break
    if bestm:
        tt[(pos, side)] = bestm["column"]
    return best, bestm


def connect4_move(state, side, time_budget=0.8, max_depth=8):
    """House-bot connect-four move. Returns {"column": c}."""
    cols = [list(c) for c in state["cols"]]
    moves = [c for c in range(7) if len(cols[c]) < 6]
    if not moves:
        return None
    if len(moves) == 1:
        return {"column": moves[0]}
    # take an immediate win without thinking
    for c in moves:
        cols[c].append(side + 1)
        won = _c4_won_at(cols, c)
        cols[c].pop()
        if won:
            return {"column": c}
    # block an immediate loss without thinking
    for c in moves:
        cols[c].append(2 - side)
        won = _c4_won_at(cols, c)
        cols[c].pop()
        if won:
            return {"column": c}
    best = {"column": 3 if 3 in moves else moves[0]}
    deadline = time.time() + time_budget
    tt = {}
    try:
        for depth in range(1, max_depth + 1):
            _val, m = _c4_negamax(cols, side, depth,
                                  -10 ** 9, 10 ** 9, deadline, tt)
            if m:
                best = m
    except _Timeout:
        pass
    return best


# ---------------------------------------------------------------------------
# poker (heads-up hold'em) — Chen preflop tiers + Monte Carlo equity postflop
# ---------------------------------------------------------------------------

def _chen(hole):
    """Chen formula hand strength for two hole cards (strings)."""
    (r1, s1), (r2, s2) = parse_card(hole[0]), parse_card(hole[1])
    hi, lo = max(r1, r2), min(r1, r2)
    base = {14: 10, 13: 8, 12: 7, 11: 6}.get(hi, hi / 2.0)
    if r1 == r2:
        score = max(5.0, base * 2)
    else:
        score = base
        if s1 == s2:
            score += 2
        gap = hi - lo - 1
        if gap == 0:
            score += 0
        elif gap == 1:
            score -= 1
            if hi < 12:
                score += 1
        elif gap == 2:
            score -= 2
        elif gap == 3:
            score -= 4
        else:
            score -= 5
    return math.ceil(score)


def _poker_equity(hole, community, samples=350):
    """Monte Carlo equity of hole+community vs a random hand."""
    known = set(hole) | set(community)
    deck = [r + s for s in "shdc" for r in "23456789TJQKA"
            if r + s not in known]
    need_board = 5 - len(community)
    wins = ties = 0.0
    my7 = list(hole) + list(community)
    for _ in range(samples):
        samp = random.sample(deck, 2 + need_board)
        opp7 = samp[:2] + list(community) + samp[2:]
        mk = poker_best7(my7 + samp[2:])[:2]
        ok = poker_best7(opp7)[:2]
        if mk > ok:
            wins += 1
        elif mk == ok:
            ties += 1
    return (wins + 0.5 * ties) / samples


def poker_move(hole, community, street, legal, ctx):
    """Heads-up poker decision.

    hole: [c1, c2]; community: [...]; street: preflop/flop/turn/river.
    legal: list of move dicts from the engine (with min/max amounts).
    ctx: dict(pot, to_call, stack, my_bet, is_button, bb).
    Returns a move dict for Arena.make_move.
    """
    pot = ctx["pot"]
    to_call = ctx["to_call"]
    stack = ctx["stack"]
    my_bet = ctx.get("my_bet", 0)
    bb = ctx.get("bb", 2)
    actions = {m["action"]: m for m in legal}

    def bet_size(frac=0.65):
        m = actions.get("bet")
        if not m:
            return None
        amt = int(round(frac * pot))
        amt = max(m["min_amount"], min(m.get("max_amount", stack), amt, stack))
        return {"action": "bet", "amount": amt}

    def raise_to(frac=0.65):
        m = actions.get("raise")
        if not m:
            return None
        target = my_bet + to_call + int(round(frac * (pot + to_call)))
        target = max(m["min_total"], target)
        extra = target - my_bet
        if extra > stack:
            return {"action": "allin"}
        return {"action": "raise", "amount": target}

    def call_or_fold(equity_like, need):
        if to_call <= 0:
            return {"action": "check"}
        if to_call >= stack:  # calling = all-in: demand real equity
            return ({"action": "call", "amount": stack}
                    if equity_like > to_call / (pot + to_call) else
                    {"action": "fold"})
        if equity_like >= need:
            return {"action": "call", "amount": min(stack, to_call)}
        return {"action": "fold"}

    if street == "preflop":
        chen = _chen(hole)
        if not ctx.get("is_button"):
            chen -= 0  # heads-up: small blind already in; keep simple
        if to_call <= 0:
            if chen >= 8:
                return bet_size() or {"action": "check"}
            return {"action": "check"}
        # facing a bet/raise
        if chen >= 11:
            return raise_to() or call_or_fold(1.0, 0)
        if chen >= 8:
            return call_or_fold(0.5, 0.28)
        if chen >= 6 and to_call <= 2 * bb:
            return call_or_fold(0.35, 0.22)
        return {"action": "fold"}

    # postflop: Monte Carlo equity drives everything
    equity = _poker_equity(hole, community)
    if to_call <= 0:
        if equity >= 0.58:
            return bet_size() or {"action": "check"}
        return {"action": "check"}
    pot_odds = to_call / (pot + to_call)
    if equity >= 0.72 and "raise" in actions:
        return raise_to()
    return call_or_fold(equity, pot_odds + 0.07)


# ---------------------------------------------------------------------------
# blackjack — full basic strategy (S17, double on first two cards, no split)
# ---------------------------------------------------------------------------

def sanitize_poker_move(legal, move):
    """Clamp a bot's poker move into the engine's legal ranges (strategic
    choice preserved, amounts fixed). Also works around an engine quirk:
    a short stack can be offered bet with min_amount > max_amount — an
    unmakable bet, in which case we check (or shove) instead."""
    by = {m["action"]: m for m in legal}
    a = (move or {}).get("action")
    if a not in by:
        return {"action": "fold" if "fold" in by else "check"}
    m = by[a]
    if a == "bet":
        if m["max_amount"] < m["min_amount"]:
            return ({"action": "check"} if "check" in by
                    else {"action": "allin"})
        try:
            amt = int(move.get("amount"))
        except (TypeError, ValueError):
            amt = m["min_amount"]
        return {"action": "bet",
                "amount": max(m["min_amount"], min(m["max_amount"], amt))}
    if a == "raise":
        try:
            amt = int(move.get("amount"))
        except (TypeError, ValueError):
            amt = m["min_total"]
        return {"action": "raise", "amount": max(m["min_total"], amt)}
    if a == "call":
        return {"action": "call", "amount": m["amount"]}
    return {"action": a}


def _bj_basic(hand, dealer_up, can_double):
    total, soft = bj_total(hand)
    r = dealer_up[0]
    if r == "A":
        dv = 11
    elif r in "TJQK":
        dv = 10
    else:
        dv = int(r)

    def dbl_else(act):
        return "double" if can_double else act

    if soft:
        if total <= 14:      # A,2 – A,3
            return dbl_else("hit") if 4 <= dv <= 6 else "hit"
        if total <= 16:      # A,4 – A,5
            return dbl_else("hit") if 4 <= dv <= 6 else "hit"
        if total == 17:      # A,6
            return dbl_else("hit") if 3 <= dv <= 6 else "hit"
        if total == 18:      # A,7
            if 3 <= dv <= 6:
                return dbl_else("stand")
            return "stand" if dv in (2, 7, 8) else "hit"
        return "stand"       # A,8+
    else:
        if total <= 8:
            return "hit"
        if total == 9:
            return dbl_else("hit") if 3 <= dv <= 6 else "hit"
        if total == 10:
            return dbl_else("hit") if dv <= 9 else "hit"
        if total == 11:
            return dbl_else("hit")
        if total == 12:
            return "stand" if 4 <= dv <= 6 else "hit"
        if 13 <= total <= 16:
            return "stand" if dv <= 6 else "hit"
        return "stand"


def blackjack_move(hand, dealer_up, legal):
    """Basic-strategy blackjack. hand/dealer_up are card strings;
    legal is the engine's legal-move list."""
    actions = {m["action"] for m in legal}
    if not hand or dealer_up is None:
        return {"action": "stand" if "stand" in actions else "hit"}
    want = _bj_basic(hand, dealer_up, "double" in actions)
    if want not in actions:  # safety: never return an illegal move
        want = "stand" if "stand" in actions else "hit"
    return {"action": want}
