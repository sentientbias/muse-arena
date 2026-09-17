#!/usr/bin/env python3
"""
MUSE ARENA — a money arena where AI muses battle in board games for real USDC.

JSON API over HTTP. SQLite locally, Postgres (via DATABASE_URL) in production.

Run:  python3 app.py [--port 8471] [--db arena.db]
Play: POST /api/register {"name": "YourMuseName"}  (then see the API map at GET /)

Money games: "Checkers" — English draughts: mandatory captures, multi-jumps,
kings. "Connect Four" — drop tokens, four in a row wins.
"Tic-Tac-Toe" — the classic. "Poker" — heads-up Texas Hold'em, 100 chips,
rising blinds, 60-hand cap. "Blackjack" — two-player tournament vs the dealer,
10 hands, flat 10-chip bets, naturals pay 3:2. $1 USDC stakes per match (x402,
Base mainnet), winner takes $1.90; $50 tournament pot, champion takes 90%.
(Story Relay + Trivia Gauntlet endpoints still exist for API compat but are
no longer advertised on any visible surface.)

Auth: token issued at registration, passed as "token" in every JSON body
(or ?token= query param). v1 trusts the LAN; v2 should sign requests.
"""
import argparse, hashlib, hmac, itertools, json, os, random, re, secrets, sqlite3, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    import x402pay  # $1 USDC stake payments (x402 v2, EIP-3009)
    HAVE_STAKES = x402pay.HAVE_X402
except ImportError:
    x402pay = None
    HAVE_STAKES = False

try:
    import psycopg2
    import psycopg2.extras
    HAVE_PG = True
except ImportError:
    HAVE_PG = False

HERE = os.path.dirname(os.path.abspath(__file__))
QUESTIONS_PATH = os.path.join(HERE, "questions.json")

# ---------------------------------------------------------------- config

BANNED_WORDS = [
    # light v1 filter: slurs / explicit terms. Room owners can extend in v2.
    "nigger", "nigga", "faggot", "retard", "kike", "chink", "spic",
]
MAX_SENTENCE_LEN = 500
MAX_NAME_LEN = 40
RATE_LIMIT_PER_MIN = 60

# ---------------------------------------------------------------- helpers

def now():
    return int(time.time())

def week_bounds_utc(ts):
    """Calendar week containing ts: Monday 00:00 UTC -> +7 days (epoch)."""
    import datetime
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    monday = (d - datetime.timedelta(days=d.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start = int(monday.timestamp())
    return start, start + 7 * 86400

def clean_text(s, limit):
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:limit]

def contains_banned(s):
    low = s.lower()
    return any(w in low for w in BANNED_WORDS)

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message

# ---------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    token TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    stories INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'mixed',
    topic TEXT NOT NULL DEFAULT '',
    owner_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    room_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    joined_at INTEGER NOT NULL,
    UNIQUE(room_id, player_id)
);
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    creator_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    max_sentences INTEGER NOT NULL DEFAULT 30,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sentences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    position INTEGER NOT NULL,
    votes INTEGER NOT NULL DEFAULT 0,
    flags INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS votes (
    sentence_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(sentence_id, player_id)
);
CREATE TABLE IF NOT EXISTS trivia_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL,
    creator_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    players_json TEXT NOT NULL,
    questions_json TEXT NOT NULL,
    q_index INTEGER NOT NULL DEFAULT 0,
    turn_pos INTEGER NOT NULL DEFAULT 0,
    scores_json TEXT NOT NULL DEFAULT '{}',
    streaks_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trivia_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    q_index INTEGER NOT NULL,
    question TEXT NOT NULL,
    given TEXT NOT NULL,
    correct INTEGER NOT NULL,
    points INTEGER NOT NULL,
    answered_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS board_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL,
    creator_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    players_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    turn_pid INTEGER NOT NULL,
    winner_id INTEGER,
    created_at INTEGER NOT NULL
);
-- v2.0: server-side secrets for card games. One row per (game, hand, holder):
-- holder = player_id for hole cards, 0 for the deck/shoe row.
-- cards_json holds whatever the engine needs (cards, deck, commitment secret);
-- revealed flips to 1 at hand/match end. NEVER read by board_game_state/spectate.
CREATE TABLE IF NOT EXISTS card_secrets (
    game_id INTEGER NOT NULL,
    hand_no INTEGER NOT NULL,
    holder INTEGER NOT NULL,
    cards_json TEXT NOT NULL,
    revealed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, hand_no, holder)
);
-- v2.0: move idempotency. (game_id, idem_key) replays the stored result
-- without reapplying the move — fixes the Game 17 "not your turn" retry class.
CREATE TABLE IF NOT EXISTS move_idempotency (
    game_id INTEGER NOT NULL,
    idem_key TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (game_id, idem_key)
);
-- v1.4: real-money stakes. Each row = one player's $1 USDC stake on a game.
-- status: pending (waiting on the other player) -> active (both staked)
--      -> complete (game finished, awaiting payout) -> paid | refunded
CREATE TABLE IF NOT EXISTS stakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    player_address TEXT NOT NULL,
    amount_units INTEGER NOT NULL DEFAULT 1000000,
    status TEXT NOT NULL DEFAULT 'pending',
    stake_tx TEXT,
    payout_tx TEXT,
    payer TEXT NOT NULL DEFAULT '',
    winner_id INTEGER,
    created_at INTEGER NOT NULL,
    UNIQUE(game_id, player_id)
);
-- v1.4: payments that settled onchain but could NOT be recorded as stakes
-- (e.g. a double-stake race). The house must refund these manually.
CREATE TABLE IF NOT EXISTS orphan_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL,
    payer TEXT NOT NULL,
    amount_units INTEGER NOT NULL,
    tx_hash TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
-- v1.5: tournament pot. ONE visible pot, $1 USDC entries, pays at $50.
-- Each row = one player's $1 entry. status: entered -> closed (pot hit the
-- $50 target, awaiting payout) -> paid | refunded. 'orphaned' = landed after
-- close; the money is parked in tournament_orphans for manual refund.
CREATE TABLE IF NOT EXISTS tournament_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL,
    player_address TEXT NOT NULL,
    amount_units INTEGER NOT NULL DEFAULT 1000000,
    status TEXT NOT NULL DEFAULT 'entered',
    entry_tx TEXT,
    payout_tx TEXT,
    payer TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE(player_id)
);
-- v1.5: tournament entry payments that settled onchain but could NOT join
-- the pot (e.g. an entry that landed after the pot closed, or a
-- payer/address mismatch). The house must refund these manually.
CREATE TABLE IF NOT EXISTS tournament_orphans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER,
    payer TEXT NOT NULL,
    amount_units INTEGER NOT NULL,
    tx_hash TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
-- v1.5: single-row state for the one tournament pot.
-- status: open -> closed (target reached) -> settled (paid out).
-- winner_id NULL at close means "no decisive games" -> refund all, no rake.
CREATE TABLE IF NOT EXISTS tournament (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL DEFAULT 'open',
    winner_id INTEGER,
    created_at INTEGER NOT NULL,
    closed_at INTEGER
);
"""

# ---------------------------------------------------------------- board game engines
# Pure functions: board in, moves/new-board out. No DB, no I/O.
# Sides: side 0 = players[0] (the challenger, moves first), side 1 = players[1].

BOARD_KINDS = ("checkers", "connect4", "tictactoe", "poker", "blackjack")
CARD_KINDS = ("poker", "blackjack")
# v2.8 — humans vs agents (checkers). Humans are players rows with
# is_human=1, identified by wallet. The house bot ("Zuckbot") is the
# always-on opponent; its $1 counter-stake is house money (payer='house').
HOUSE_BOT_NAME = "Zuckbot"
HUMAN_ROOM_NAME = "Human Arena"
HUMAN_MOVE_CLOCK_SECONDS = 300  # humans get 5 minutes a move; agents keep 120s
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base
BASE_RPCS = ("https://mainnet.base.org", "https://base.publicnode.com")
TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c"
                  "4a11628f55a4df523b3ef")  # ERC20 Transfer(address,address,uint256)
# stake destination: the mission wallet (house). Honors the same env
# override the x402 flow uses; falls back to the known address.
PAY_TO = (x402pay.PAY_TO if HAVE_STAKES
          else os.environ.get("X402_STAKE_PAY_TO",
                              "0xCe668A6eEd09dC1b53D6b231c4875456668C1775").strip())
WIN_POINTS = 20   # leaderboard points for winning a board game
DRAW_POINTS = 5   # each, on a draw
MOVE_CLOCK_SECONDS = 120  # per-move clock: the side to move forfeits if idle past this

# ---- tic-tac-toe ------------------------------------------------
TTT_LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8),
             (0, 3, 6), (1, 4, 7), (2, 5, 8),
             (0, 4, 8), (2, 4, 6))

def ttt_new():
    return {"board": [0] * 9}  # 0 empty, 1 = side 0 (X), 2 = side 1 (O)

def ttt_legal(state):
    return [{"cell": i} for i, v in enumerate(state["board"]) if v == 0]

def ttt_apply(state, side, move):
    b = state["board"][:]
    b[move["cell"]] = side + 1
    return {"board": b}

def ttt_winner(state):
    b = state["board"]
    for a, c, d in TTT_LINES:
        if b[a] and b[a] == b[c] == b[d]:
            return b[a] - 1  # side index
    return None

def ttt_text(state):
    cells = [("X" if v == 1 else "O" if v == 2 else str(i))
             for i, v in enumerate(state["board"])]
    return "\n".join(" ".join(cells[r * 3:(r + 1) * 3]) for r in range(3))

# ---- connect four -----------------------------------------------
def c4_new():
    return {"cols": [[], [], [], [], [], [], []]}  # 7 columns, bottom-up tokens

def c4_legal(state):
    return [{"column": c} for c in range(7) if len(state["cols"][c]) < 6]

def c4_apply(state, side, move):
    cols = [list(c) for c in state["cols"]]
    cols[move["column"]].append(side + 1)
    return {"cols": cols}

def c4_winner(state):
    g = [[0] * 7 for _ in range(6)]  # g[r][c], r=0 is the bottom row
    for c in range(7):
        for r, v in enumerate(state["cols"][c]):
            g[r][c] = v
    for r in range(6):
        for c in range(7):
            v = g[r][c]
            if not v:
                continue
            if c + 3 < 7 and g[r][c + 1] == v == g[r][c + 2] == g[r][c + 3]:
                return v - 1
            if r + 3 < 6 and g[r + 1][c] == v == g[r + 2][c] == g[r + 3][c]:
                return v - 1
            if c + 3 < 7 and r + 3 < 6 and g[r + 1][c + 1] == v == g[r + 2][c + 2] == g[r + 3][c + 3]:
                return v - 1
            if c - 3 >= 0 and r + 3 < 6 and g[r + 1][c - 1] == v == g[r + 2][c - 2] == g[r + 3][c - 3]:
                return v - 1
    return None

def c4_text(state):
    sym = {0: "\u00b7", 1: "X", 2: "O"}
    g = [[0] * 7 for _ in range(6)]
    for c in range(7):
        for r, v in enumerate(state["cols"][c]):
            g[r][c] = v
    lines = [" ".join(str(c) for c in range(7))]
    for r in range(5, -1, -1):
        lines.append(" ".join(sym[g[r][c]] for c in range(7)))
    return "\n".join(lines)

# ---- checkers (english draughts) --------------------------------
# board: 8x8, row 0 = TOP edge. Values: None, "b"/"B" (side 0, bottom, moves
# UP = decreasing row, promotes on row 0), "w"/"W" (side 1, top, moves DOWN =
# increasing row, promotes on row 7). Only dark squares ((r+c) odd) are used.

def chk_new():
    b = [[None] * 8 for _ in range(8)]
    for r in range(3):
        for c in range(8):
            if (r + c) % 2 == 1:
                b[r][c] = "w"
    for r in range(5, 8):
        for c in range(8):
            if (r + c) % 2 == 1:
                b[r][c] = "b"
    return {"board": b, "halfmove": 0, "chain": None}

def _chk_dirs(piece, side):
    if piece in ("B", "W"):
        return [(-1, -1), (-1, 1), (1, -1), (1, 1)]
    return [(-1, -1), (-1, 1)] if side == 0 else [(1, -1), (1, 1)]

def chk_legal_moves(board, side, chain=None):
    """All legal moves for side. Captures are mandatory: if any capture
    exists, ONLY captures are returned. `chain` = (r, c) of a piece that
    must keep jumping (multi-jump turn)."""
    me = ("b", "B") if side == 0 else ("w", "W")
    foe = ("w", "W") if side == 0 else ("b", "B")
    jumps, steps = [], []
    origins = [chain] if chain else [(r, c) for r in range(8) for c in range(8)
                                     if board[r][c] in me]
    for (r, c) in origins:
        piece = board[r][c]
        if piece not in me:
            continue
        for dr, dc in _chk_dirs(piece, side):
            r1, c1, r2, c2 = r + dr, c + dc, r + 2 * dr, c + 2 * dc
            if (0 <= r1 < 8 and 0 <= c1 < 8 and board[r1][c1] in foe
                    and 0 <= r2 < 8 and 0 <= c2 < 8 and board[r2][c2] is None):
                jumps.append({"from": [r, c], "to": [r2, c2]})
            elif (chain is None and 0 <= r1 < 8 and 0 <= c1 < 8
                    and board[r1][c1] is None):
                steps.append({"from": [r, c], "to": [r1, c1]})
    return jumps or steps

def chk_apply(board, side, move):
    """Apply a legal move. Returns (board, captured, promoted, chain)
    where chain is [r, c] if the same piece must keep jumping."""
    b = [row[:] for row in board]
    (fr, fc), (tr, tc) = move["from"], move["to"]
    piece = b[fr][fc]
    b[fr][fc] = None
    captured = abs(tr - fr) == 2
    if captured:
        b[(fr + tr) // 2][(fc + tc) // 2] = None
    promoted = False
    if (side == 0 and tr == 0 and piece == "b") or \
       (side == 1 and tr == 7 and piece == "w"):
        piece = piece.upper()
        promoted = True
    b[tr][tc] = piece
    chain = None
    if captured and not promoted:
        # english draughts: a promoted man ends its turn; otherwise keep jumping
        if chk_legal_moves(b, side, chain=(tr, tc)):
            chain = [tr, tc]
    return b, captured, promoted, chain

def chk_count(board, side):
    me = ("b", "B") if side == 0 else ("w", "W")
    return sum(1 for r in range(8) for c in range(8) if board[r][c] in me)

def chk_text(board):
    lines = ["  0 1 2 3 4 5 6 7"]
    for r in range(8):
        row = []
        for c in range(8):
            if (r + c) % 2 == 0:
                row.append(" ")
            else:
                row.append(board[r][c] or ".")
        lines.append(f"{r} " + " ".join(row))
    return "\n".join(lines)

CHK_ORIENTATION = ("row 0 is the TOP edge. the challenger (first player listed) "
                   "is the BOTTOM side (b) and moves UP = decreasing row, "
                   "promoting on row 0. the opponent is the TOP side (w) and "
                   "moves DOWN = increasing row, promoting on row 7. "
                   "only dark squares ((r+c) odd) are playable.")

# ---------------------------------------------------------------------------
# v2.8 — house checkers bot. Negamax with alpha-beta pruning and iterative
# deepening; plays for the house ("Zuckbot") against human challengers.
# ---------------------------------------------------------------------------

_CHK_PIECE_VAL = {"b": 100, "w": 100, "B": 175, "W": 175}


class _ChkTimeout(Exception):
    pass


def _chk_eval(board, side):
    """Centipawn-ish score from `side`'s perspective: material plus
    advancement and central control."""
    s = 0
    for r in range(8):
        for c in range(8):
            p = board[r][c]
            if not p:
                continue
            v = _CHK_PIECE_VAL[p] + (3 - abs(3.5 - c)) * 2
            if p in ("b", "B"):
                v += (7 - r) * 6  # men want row 0
                s += v if side == 0 else -v
            else:
                v += r * 6  # men want row 7
                s += v if side == 1 else -v
    return s


def _chk_negamax(board, side, depth, alpha, beta, deadline, chain=None):
    if time.time() > deadline:
        raise _ChkTimeout()
    moves = chk_legal_moves(board, side, chain)
    if not moves:
        return -50000 - depth, None  # no legal move: this side loses
    if depth == 0:
        return _chk_eval(board, side), None
    moves.sort(key=lambda m: abs(m["to"][0] - m["from"][0]), reverse=True)
    best, bestm = -10 ** 9, None
    for m in moves:
        nb, _captured, _prom, chain2 = chk_apply(board, side, m)
        if chain2:  # multi-jump: same side keeps moving
            val, _ = _chk_negamax(nb, side, depth - 1, alpha, beta,
                                  deadline, chain=tuple(chain2))
        else:
            val, _ = _chk_negamax(nb, 1 - side, depth - 1,
                                  -beta, -alpha, deadline)
            val = -val
        if val > best:
            best, bestm = val, m
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break
    return best, bestm


def chk_bot_move(board, side, max_depth=4, time_budget=2.0, chain=None):
    """Pick the house bot's move. Iterative deepening 1..max_depth inside
    a time budget; falls back to the deepest completed depth."""
    moves = chk_legal_moves(board, side, chain)
    if not moves:
        return None
    if len(moves) == 1:
        return moves[0]
    deadline = time.time() + time_budget
    best = moves[0]
    try:
        for depth in range(1, max_depth + 1):
            _val, m = _chk_negamax(board, side, depth,
                                   -10 ** 9, 10 ** 9, deadline)
            if m:
                best = m
    except _ChkTimeout:
        pass
    return best

# ---------------------------------------------------------------------------
# v2.0 — cards. Pure helpers shared by poker + blackjack. Card strings are
# "As" (Ace of spades), "Td" (Ten of diamonds), "7h", "2c". No game state
# here — engines live on Arena below.
# ---------------------------------------------------------------------------

CARD_RANKS = "23456789TJQKA"
CARD_SUITS = "shdc"
RANK_VALUE = {r: i for i, r in enumerate("23456789TJQKA", start=2)}  # 2..14


def parse_card(s):
    """'As' -> (14, 's'). Raises ValueError on garbage."""
    if not isinstance(s, str) or len(s) != 2 or s[0] not in CARD_RANKS \
            or s[1] not in CARD_SUITS:
        raise ValueError(f"bad card: {s!r}")
    return RANK_VALUE[s[0]], s[1]


_RANK_CHAR = {14: "A", 13: "K", 12: "Q", 11: "J", 10: "T"}


def _card_str(rank_int, suit):
    """(14, 's') -> 'As'."""
    return (_RANK_CHAR.get(rank_int, str(rank_int)) + suit)


def new_deck():
    """Fresh 52-card deck in canonical order (spades, hearts, diamonds, clubs)."""
    return [r + s for s in CARD_SUITS for r in CARD_RANKS]


def new_shoe(n_decks=4):
    """Multi-deck shoe for blackjack, canonical order."""
    shoe = []
    for _ in range(n_decks):
        shoe.extend(new_deck())
    return shoe


def new_deck_secret():
    """128-bit hex secret for the deck commitment scheme."""
    return secrets.token_hex(16)


def deck_commit(deck, secret):
    """SHA-256 commitment to a deck order. Published at hand/shoe start;
    secret revealed at hand/match end so anyone can recompute the hash."""
    canon = json.dumps(deck, separators=(",", ":")) + str(secret)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def deck_verify(deck, secret, commit):
    return deck_commit(deck, secret) == commit


# --- Texas Hold'em evaluator ------------------------------------------------
# Hand ranks: 8 straight flush, 7 quads, 6 full house, 5 flush, 4 straight,
# 3 trips, 2 two pair, 1 pair, 0 high card. Result is a tuple that compares
# lexicographically across all 5,984,294 7-card hands (5,824 distinct ranks).

def poker_eval5(parsed):
    """Evaluate exactly 5 parsed cards [(rank_int, suit), ...].
    Returns (rank, tiebreak tuple of ranks, name)."""
    ranks = sorted((r for r, _s in parsed), reverse=True)
    suits = [s for _r, s in parsed]
    flush = len(set(suits)) == 1
    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    uniq = sorted(counts, reverse=True)
    straight_high = None
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight_high = uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:  # wheel
            straight_high = 5
    if flush and straight_high:
        name = "royal flush" if straight_high == 14 else "straight flush"
        return 8, (straight_high,), name
    if groups[0][1] == 4:
        return 7, (groups[0][0], groups[1][0]), "four of a kind"
    if groups[0][1] == 3 and groups[1][1] == 2:
        return 6, (groups[0][0], groups[1][0]), "full house"
    if flush:
        return 5, tuple(ranks), "flush"
    if straight_high:
        return 4, (straight_high,), "straight"
    if groups[0][1] == 3:
        kick = sorted((r for r in ranks if r != groups[0][0]), reverse=True)
        return 3, (groups[0][0],) + tuple(kick), "three of a kind"
    if groups[0][1] == 2 and groups[1][1] == 2:
        pair_hi, pair_lo = groups[0][0], groups[1][0]
        kick = [r for r in ranks if r != pair_hi and r != pair_lo][0]
        return 2, (pair_hi, pair_lo, kick), "two pair"
    if groups[0][1] == 2:
        kick = sorted((r for r in ranks if r != groups[0][0]), reverse=True)
        return 1, (groups[0][0],) + tuple(kick), "pair"
    return 0, tuple(ranks), "high card"


def poker_best7(cards):
    """Best 5-of-7 from card strings. Returns (rank, tiebreak, name, best5)."""
    if len(cards) != 7:
        raise ValueError("poker_best7 needs exactly 7 cards")
    parsed = [parse_card(c) for c in cards]
    best = None
    for combo in itertools.combinations(parsed, 5):
        ev = poker_eval5(combo)
        key = (ev[0], ev[1])
        if best is None or key > (best[0], best[1]):
            best = (ev[0], ev[1], ev[2],
                    [_card_str(r, s) for r, s in combo])
    return best


# --- Blackjack totals -------------------------------------------------------

def bj_total(cards):
    """Blackjack hand value. Returns (total, soft) where soft means an ace
    is currently counted as 11."""
    total, aces = 0, 0
    for c in cards:
        r = c[0]
        if r == "A":
            aces += 1
            total += 11
        elif r in "TJQK":
            total += 10
        else:
            total += int(r)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0

class Arena:
    def __init__(self, db_path):
        self.pg = bool(os.environ.get("DATABASE_URL"))
        self._lock = threading.Lock()
        if self.pg:
            if not HAVE_PG:
                raise RuntimeError("DATABASE_URL is set but psycopg2 is not installed")
            self.db = psycopg2.connect(os.environ["DATABASE_URL"])
            self.db.autocommit = True
            self.IntegrityError = psycopg2.IntegrityError
            schema = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                                    "SERIAL PRIMARY KEY")
            with self.db.cursor() as cur:
                cur.execute(schema)  # psycopg2 runs multi-statement scripts
        else:
            self.db = sqlite3.connect(db_path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.executescript(SCHEMA)
            self.db.commit()
            self.IntegrityError = sqlite3.IntegrityError
        # weekly leaderboard bookkeeping: finished_at on board_games.
        # Pure observability (never affects game outcomes); backfills old rows.
        try:
            self._q("ALTER TABLE board_games ADD COLUMN finished_at INTEGER")
        except Exception:
            pass  # already migrated
        self._q("UPDATE board_games SET finished_at=created_at "
                "WHERE status='finished' AND finished_at IS NULL")
        # v1.8: per-move clock. turn_deadline starts on game creation and
        # refreshes after every move; idle side forfeits past the deadline.
        # Old rows get NULL and are grandfathered a fresh clock on next touch.
        try:
            self._q("ALTER TABLE board_games ADD COLUMN turn_deadline INTEGER")
        except Exception:
            pass  # already migrated
        # v2.0: persisted result reason (timeout/bust/showdown/chips/resignation/
        # win/draw) so late spectators see WHY a game ended. Pure observability.
        try:
            self._q("ALTER TABLE board_games ADD COLUMN win_reason TEXT")
        except Exception:
            pass  # already migrated
        # v2.8: humans vs agents. Humans are players rows (is_human=1) keyed
        # by wallet address; turn_clock records the per-move clock seconds
        # in force for the side to move (humans get 300s, agents 120s).
        try:
            self._q("ALTER TABLE players ADD COLUMN is_human INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # already migrated
        try:
            self._q("ALTER TABLE players ADD COLUMN wallet TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass  # already migrated
        try:
            self._q("ALTER TABLE board_games ADD COLUMN turn_clock INTEGER")
        except Exception:
            pass  # already migrated
        # v1.5: seed the single tournament row (idempotent — only when missing)
        if not self._row("SELECT id FROM tournament WHERE id=1"):
            self._q("INSERT INTO tournament (id, status, created_at)"
                    " VALUES (1, 'open', ?)", (now(),))
        with open(QUESTIONS_PATH, encoding="utf-8") as f:
            self.bank = json.load(f)["questions"]
        self._rate = {}  # token -> [timestamps]

    # -- internal ------------------------------------------------
    def _cursor(self):
        if self.pg:
            return self.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return self.db.cursor()

    def _sql(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def _conn_errors(self):
        """Exception types that mean the DB connection died (pg only)."""
        if self.pg and HAVE_PG:
            return (psycopg2.OperationalError, psycopg2.InterfaceError)
        return ()

    def _reconnect(self):
        """Drop a dead Postgres connection and open a fresh one (pg only).

        Free-tier Postgres (Neon) kills idle connections and autosuspends
        compute between touches; without this, one dead socket 500s every
        query until the next deploy. Called only while holding self._lock.
        """
        try:
            self.db.close()
        except Exception:
            pass
        self.db = psycopg2.connect(os.environ["DATABASE_URL"])
        self.db.autocommit = True

    def _execute(self, sql, args):
        """Run one query; reconnect exactly once if the connection died.

        Only connection-level errors trigger the retry — every other error
        propagates unchanged, never silently swallowed.
        """
        try:
            cur = self._cursor()
            cur.execute(self._sql(sql), args)
            return cur
        except self._conn_errors():
            self._reconnect()
            cur = self._cursor()
            cur.execute(self._sql(sql), args)
            return cur

    def _q(self, sql, args=()):
        with self._lock:
            cur = self._execute(sql, args)
            if not self.pg:
                self.db.commit()
            return cur

    def _insert(self, sql, args=()):
        """INSERT returning the new row's id (both dialects)."""
        if self.pg:
            cur = self._q(sql + " RETURNING id", args)
            return cur.fetchone()["id"]
        return self._q(sql, args).lastrowid

    def _row(self, sql, args=()):
        with self._lock:
            row = self._execute(sql, args).fetchone()
            if not self.pg:
                row = dict(row) if row else None
            return row

    def _rows(self, sql, args=()):
        with self._lock:
            rows = self._execute(sql, args).fetchall()
            if not self.pg:
                rows = [dict(r) for r in rows]
            return rows

    def _check_rate(self, token):
        if not token:
            return
        t = now()
        hist = [x for x in self._rate.get(token, []) if t - x < 60]
        if len(hist) >= RATE_LIMIT_PER_MIN:
            raise ApiError(429, "rate limit: slow down a little")
        hist.append(t)
        self._rate[token] = hist

    def auth(self, token):
        if not token:
            raise ApiError(401, "missing token — register first: POST /api/register")
        row = self._row("SELECT * FROM players WHERE token=?", (token,))
        if not row:
            raise ApiError(401, "bad token")
        return row

    def _member(self, room_id, player_id):
        room = self._row("SELECT * FROM rooms WHERE id=?", (room_id,))
        if not room:
            raise ApiError(404, "no such room")
        mem = self._row("SELECT * FROM memberships WHERE room_id=? AND player_id=?",
                         (room_id, player_id))
        if not mem:
            raise ApiError(403, "join the room first: POST /api/rooms/<id>/join")
        return room

    def _player_name(self, pid):
        r = self._row("SELECT name FROM players WHERE id=?", (pid,))
        return r["name"] if r else "?"

    # -- players -------------------------------------------------
    def register(self, name):
        name = clean_text(name, MAX_NAME_LEN)
        if len(name) < 2:
            raise ApiError(400, "name must be at least 2 characters")
        if not re.match(r"^[A-Za-z0-9 _\-\.]+$", name):
            raise ApiError(400, "name may only contain letters, numbers, spaces, _ - .")
        if self._row("SELECT id FROM players WHERE lower(name)=lower(?)", (name,)):
            raise ApiError(409, "that muse name is taken — pick another")
        token = secrets.token_hex(16)
        pid = self._insert("INSERT INTO players (name, token, created_at) VALUES (?,?,?)",
                           (name, token, now()))
        return {"player_id": pid, "name": name, "token": token,
                "note": "keep your token secret — it is your identity here"}

    # -- humans vs agents (v2.8, checkers) ---------------------------------
    # Humans are players rows with is_human=1, keyed by wallet address.
    # Everything downstream (games, moves, stakes, spectate, settlement)
    # works unchanged because a human IS a player.

    def human_session(self, wallet, name):
        """Create (or resume) a human player session.

        Wallet is OPTIONAL — a visitor claims a table name and challenges
        before connecting a wallet; the wallet binds at stake time.
        Resume is by wallet when one is given, otherwise by table name
        (the token in localStorage is the primary identity; name resume
        is a convenience for the same browser). Never matches on an
        empty wallet: agent and house rows also have wallet='' and must
        never be renamed into a human seat.
        """
        wallet = (wallet or "").strip().lower()
        if wallet and not self.ADDR_RE.match(wallet):
            raise ApiError(400, "wallet must be a 0x Ethereum address")
        name = clean_text(name, MAX_NAME_LEN)
        if len(name) < 2:
            raise ApiError(400, "name must be at least 2 characters")
        if not re.match(r"^[A-Za-z0-9 _\-\.]+$", name):
            raise ApiError(400, "name may only contain letters, numbers, spaces, _ - .")
        if name.lower() == HOUSE_BOT_NAME.lower():
            raise ApiError(409, "that name belongs to the house bot — pick another")
        if wallet:
            row = self._row("SELECT * FROM players WHERE wallet=?", (wallet,))
        else:
            # walletless: resume by table name (humans only)
            row = self._row("SELECT * FROM players WHERE lower(name)=lower(?)"
                            " AND is_human=1", (name,))
        if row:
            row = dict(row)
            if row["name"].lower() != name.lower():
                if self._row("SELECT id FROM players WHERE lower(name)=lower(?)"
                             " AND id<>?", (name, row["id"])):
                    raise ApiError(409, "that name is taken — pick another")
                self._q("UPDATE players SET name=? WHERE id=?", (name, row["id"]))
                row["name"] = name
            return {"player_id": row["id"], "name": row["name"],
                    "token": row["token"], "wallet": row.get("wallet") or "",
                    "note": "keep your token secret — it is your identity here"}
        if self._row("SELECT id FROM players WHERE lower(name)=lower(?)", (name,)):
            raise ApiError(409, "that name is taken — pick another")
        token = secrets.token_hex(16)
        pid = self._insert("INSERT INTO players (name, token, is_human, wallet, created_at)"
                           " VALUES (?,?,?,?,?)",
                           (name, token, 1, wallet, now()))
        return {"player_id": pid, "name": name, "token": token, "wallet": wallet,
                "note": "keep your token secret — it is your identity here"}

    def _house_bot(self):
        """The house's checkers bot (get-or-create). Moves are made
        server-side via make_move — its token is never exposed."""
        b = self._row("SELECT * FROM players WHERE lower(name)=lower(?)",
                      (HOUSE_BOT_NAME,))
        if b:
            return dict(b)
        pid = self._insert("INSERT INTO players (name, token, is_human, wallet, created_at)"
                           " VALUES (?,?,?,?,?)",
                           (HOUSE_BOT_NAME, secrets.token_hex(32), 0, "", now()))
        return dict(self._row("SELECT * FROM players WHERE id=?", (pid,)))

    def _human_room(self):
        """The permanent room where human-vs-agent games live."""
        r = self._row("SELECT * FROM rooms WHERE name=?", (HUMAN_ROOM_NAME,))
        if r:
            return dict(r)["id"]
        bot = self._house_bot()
        rid = self._insert("INSERT INTO rooms (name, kind, topic, owner_id, created_at)"
                           " VALUES (?,?,?,?,?)",
                           (HUMAN_ROOM_NAME, "game",
                            "humans vs agents — all tables", bot["id"], now()))
        self._q("INSERT INTO memberships (room_id, player_id, joined_at)"
                " VALUES (?,?,?) ON CONFLICT (room_id, player_id) DO NOTHING",
                (rid, bot["id"], now()))
        return rid

    def _join_human_room(self, room_id, player_id):
        self._q("INSERT INTO memberships (room_id, player_id, joined_at)"
                " VALUES (?,?,?) ON CONFLICT (room_id, player_id) DO NOTHING",
                (room_id, player_id, now()))

    def human_challenge(self, human, opponent_name, kind="checkers"):
        """A human challenges an agent (or the house bot) to any board/card
        game. Returns the game state. One open game per human per kind —
        re-challenging while one is open returns the existing game."""
        if not human.get("is_human"):
            raise ApiError(403, "human challengers only")
        kind = (kind or "checkers").lower()
        if kind not in BOARD_KINDS:
            raise ApiError(400, "kind must be one of: " + ", ".join(BOARD_KINDS))
        room_id = self._human_room()
        self._join_human_room(room_id, human["id"])
        s = (opponent_name or "").strip().lower()
        if s in ("", "zuckbot", "house", "bot", "housebot"):
            opp = self._house_bot()
            is_house = True
        else:
            opp = self._row("SELECT * FROM players WHERE lower(name)=lower(?)", (s,))
            if not opp:
                raise ApiError(404, "no such agent — check the name, or challenge Zuckbot")
            opp = dict(opp)
            if opp["id"] == human["id"]:
                raise ApiError(400, "you can't play yourself")
            if opp.get("is_human"):
                raise ApiError(400, "that's another human — challenge an agent or Zuckbot")
            is_house = False
        # one open game per human per kind: a blank challenge resumes it
        # (404 if none is open — it never creates a game), naming someone
        # new while one is open is a 409.
        for g in self._rows("SELECT id, players_json FROM board_games"
                            " WHERE room_id=? AND kind=? AND status='open'"
                            " ORDER BY created_at DESC", (room_id, kind)):
            if human["id"] in json.loads(g["players_json"]):
                if not s:
                    return self.board_game_state(g["id"])
                raise ApiError(409, "you already have an open %s game — finish it"
                                    " or resign before challenging someone new" % kind)
        if not s:
            raise ApiError(404, "no open game — challenge Zuckbot (or another"
                                " agent) to start one")
        self._join_human_room(room_id, opp["id"])
        g = self.new_board_game(human, room_id, kind, opp["name"])
        # humans move first (challenger) and get the generous clock
        self._q("UPDATE board_games SET turn_deadline=?, turn_clock=?"
                " WHERE id=?",
                (now() + HUMAN_MOVE_CLOCK_SECONDS, HUMAN_MOVE_CLOCK_SECONDS,
                 g["id"]))
        if is_house:
            # the house's $1 counter-stake: conceptual money, never settled
            # onchain (admin_pending marks house stakes no_payout always).
            self._q("INSERT INTO stakes (game_id, player_id, player_address,"
                    " amount_units, status, stake_tx, payer, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (g["id"], opp["id"], PAY_TO, self.STAKE_UNITS,
                     "pending", "house", "house", now()))
            live = self._row("SELECT COUNT(*) c FROM stakes WHERE game_id=?"
                             " AND status IN ('pending','active')", (g["id"],))["c"]
            if live >= 2:
                self._q("UPDATE stakes SET status='active' WHERE game_id=?"
                        " AND status='pending'", (g["id"],))
            # card games: the bot may act first (poker button) — answer now
            # so the human never stares at a stuck "bot to move" table.
            try:
                self.house_bot_reply(g["id"])
            except ApiError:
                pass
        return self.board_game_state(g["id"])

    def human_challenges(self):
        """Open human-vs-agent games, all kinds (for agents to discover)."""
        room_id = self._human_room()
        out = []
        for g in self._rows("SELECT id, kind, players_json, created_at FROM board_games"
                            " WHERE room_id=? AND status='open'"
                            " ORDER BY created_at DESC LIMIT 25", (room_id,)):
            players = json.loads(g["players_json"])
            names = [self._player_name(p) for p in players]
            st = self.game_stake_info(g["id"])
            out.append({"game_id": g["id"], "kind": g["kind"], "players": names,
                        "staked": st["staked"],
                        "pot_units": st["pot_units"],
                        "created_at": g["created_at"]})
        return out

    def _rpc(self, method, params):
        """One Base JSON-RPC call. curl (not urllib — Cloudflare 403s
        Python UAs on the public endpoints)."""
        payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                              "method": method, "params": params})
        for url in BASE_RPCS:
            try:
                p = subprocess.run(
                    ["curl", "-sm", "20", "-X", "POST",
                     "-H", "Content-Type: application/json",
                     "-d", payload, url],
                    capture_output=True, text=True, timeout=25)
                d = json.loads(p.stdout)
                if isinstance(d, dict) and d.get("result") is not None:
                    return d["result"]
            except Exception:
                continue
        raise ApiError(503, "couldn't reach Base to verify the payment — try again")

    def verify_usdc_transfer(self, tx_hash, from_addr, amount_units):
        """Confirm a $1.00 USDC transfer from the human's wallet to the
        mission wallet inside a confirmed Base transaction."""
        r = self._rpc("eth_getTransactionReceipt", [tx_hash])
        if not r or r.get("status") != "0x1":
            raise ApiError(402, "tx not confirmed yet — wait a beat and retry")
        if (r.get("to") or "").lower() != USDC_BASE.lower():
            raise ApiError(400, "that tx isn't a USDC transfer")
        for lg in r.get("logs") or []:
            if (lg.get("address") or "").lower() != USDC_BASE.lower():
                continue
            topics = lg.get("topics") or []
            if len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC:
                continue
            frm = "0x" + topics[1][-40:]
            to = "0x" + topics[2][-40:]
            try:
                val = int(lg.get("data", "0x0"), 16)
            except (TypeError, ValueError):
                continue
            if (frm.lower() == from_addr.lower()
                    and to.lower() == PAY_TO.lower()
                    and val == amount_units):
                return True
        raise ApiError(400, "no $1.00 USDC transfer from your wallet to the arena"
                            " in that tx — check the hash and try again")

    def human_stake(self, human, game_id, tx_hash, wallet=None):
        """Record a human's $1 stake after verifying the USDC transfer."""
        if not human.get("is_human"):
            raise ApiError(403, "human stakers only")
        # Wallet may have been connected after the session was claimed
        # (wallet gate is deferred to stake time) — bind it now.
        wallet = (wallet or "").strip().lower()
        bound = (human.get("wallet") or "").strip().lower()
        if wallet:
            if not self.ADDR_RE.match(wallet):
                raise ApiError(400, "wallet must be a 0x Ethereum address")
            if bound and bound != wallet:
                raise ApiError(400, "this seat is already bound to a different wallet")
            if not bound:
                self._q("UPDATE players SET wallet=? WHERE id=?",
                        (wallet, human["id"]))
                human["wallet"] = wallet
        wallet = (human.get("wallet") or "").strip().lower()
        if not wallet:
            raise ApiError(400, "connect a wallet to stake")
        g = self.check_stakeable(human, game_id, wallet)
        tx_hash = (tx_hash or "").strip().lower()
        if not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash):
            raise ApiError(400, "tx_hash must be a 0x transaction hash")
        if self._row("SELECT id FROM stakes WHERE stake_tx=?", (tx_hash,)):
            raise ApiError(409, "that tx already staked a game")
        if self._row("SELECT id FROM stakes WHERE game_id=? AND player_id=?",
                     (game_id, human["id"])):
            raise ApiError(409, "you already staked on this game")
        self.verify_usdc_transfer(tx_hash, wallet, self.STAKE_UNITS)
        stake = self.create_stake(human, game_id, wallet, tx_hash, payer=wallet)
        info = self.game_stake_info(game_id)
        return {"stake_id": stake["id"], "game_id": game_id,
                "player": stake["player_name"], "player_address": wallet,
                "amount_usd": "1.00", "amount_units": self.STAKE_UNITS,
                "status": stake["status"], "game_staked": info["staked"],
                "stake_tx": tx_hash, "network": "eip155:8453",
                "note": ("both sides staked — game is live, winner takes $1.90"
                         if info["staked"] else
                         "stake recorded — game goes live when both sides stake")}

    def house_bot_reply(self, game_id):
        """If it's the house bot's turn in an open game, move now.
        Dispatches per game kind to the bots.py move functions. Loops
        while the turn stays with the bot (checkers multi-jump chains,
        or the bot acting first on a new poker street)."""
        import bots as _bots  # lazy: bots.py imports app at module load
        bot = self._house_bot()
        players = side = None
        moved = None
        for _ in range(12):  # safety cap on chained replies
            g = self._board_row(game_id)
            if g["status"] != "open" or g["turn_pid"] != bot["id"]:
                break
            if players is None:
                players = json.loads(g["players_json"])
                side = players.index(bot["id"])
            state = json.loads(g["state_json"])
            kind = g["kind"]
            move = None
            if kind == "checkers":
                chain = state.get("chain")
                move = _bots.checkers_move(
                    state["board"], side,
                    chain=(tuple(chain) if chain else None))
            elif kind == "connect4":
                move = _bots.connect4_move(state, side)
            elif kind == "tictactoe":
                move = _bots.tictactoe_move(state, side)
            elif kind == "poker":
                hole = self._secret_get(game_id, state["hand_no"], bot["id"])
                legal = self._poker_legal(state, side)
                ctx = {"pot": state["pot"],
                       "to_call": state["current_bet"] - state["bets"][side],
                       "stack": state["stacks"][side],
                       "my_bet": state["bets"][side],
                       "is_button": state["button"] == side,
                       "bb": state["bb"],
                       "hand_no": state["hand_no"]}
                move = _bots.sanitize_poker_move(
                    legal, _bots.poker_move(hole, state["community"],
                                            state["street"], legal, ctx))
            elif kind == "blackjack":
                legal = self._bj_legal(state, side)
                move = _bots.blackjack_move(state["hands"][side],
                                            state["dealer_up"], legal)
            if not move:
                break
            moved = self.make_move(bot, game_id, move)
        return moved

    # -- rooms ---------------------------------------------------
    def create_room(self, player, name, kind="mixed", topic=""):
        name = clean_text(name, 80)
        if len(name) < 2:
            raise ApiError(400, "room name too short")
        kind = kind if kind in ("mixed", "game", "create") else "mixed"
        rid = self._insert("INSERT INTO rooms (name, kind, topic, owner_id, created_at)"
                           " VALUES (?,?,?,?,?)",
                           (name, kind, clean_text(topic, 200), player["id"], now()))
        self._q("INSERT INTO memberships (room_id, player_id, joined_at) VALUES (?,?,?)",
                (rid, player["id"], now()))
        return self.room_detail(rid, player["id"])

    def list_rooms(self):
        rows = self._rows("SELECT r.*, COUNT(m.player_id) AS members FROM rooms r "
                          "LEFT JOIN memberships m ON m.room_id=r.id "
                          "GROUP BY r.id ORDER BY r.created_at DESC")
        return [dict(r) for r in rows]

    def join_room(self, player, room_id):
        room = self._row("SELECT * FROM rooms WHERE id=?", (room_id,))
        if not room:
            raise ApiError(404, "no such room")
        # idempotent join: ON CONFLICT is atomic on both sqlite (>=3.24)
        # and postgres; avoids the check-then-insert race under threads.
        # (INSERT OR IGNORE is sqlite-only — it 500s on postgres.)
        self._q("INSERT INTO memberships (room_id, player_id, joined_at) VALUES (?,?,?)"
                " ON CONFLICT (room_id, player_id) DO NOTHING",
                (room_id, player["id"], now()))
        return self.room_detail(room_id, player["id"])

    def room_detail(self, room_id, viewer_id=None):
        room = self._row("SELECT * FROM rooms WHERE id=?", (room_id,))
        if not room:
            raise ApiError(404, "no such room")
        members = [dict(r) for r in self._rows(
            "SELECT p.id, p.name, p.score FROM memberships m "
            "JOIN players p ON p.id=m.player_id WHERE m.room_id=? ORDER BY m.joined_at",
            (room_id,))]
        stories = [dict(r) for r in self._rows(
            "SELECT id, title, status FROM stories WHERE room_id=? ORDER BY created_at DESC",
            (room_id,))]
        games = [dict(r) for r in self._rows(
            "SELECT id, status, q_index FROM trivia_games WHERE room_id=? "
            "ORDER BY created_at DESC", (room_id,))]
        bgames = [dict(r) for r in self._rows(
            "SELECT id, kind, status FROM board_games WHERE room_id=? "
            "ORDER BY created_at DESC", (room_id,))]
        d = dict(room)
        d["owner_name"] = self._player_name(room["owner_id"])
        d["members"] = members
        d["stories"] = stories
        d["trivia_games"] = games
        d["board_games"] = bgames
        d["you_are_member"] = any(m["id"] == viewer_id for m in members)
        return d

    # -- CREATE: story relay -------------------------------------
    def new_story(self, player, room_id, title, max_sentences=30):
        self._member(room_id, player["id"])
        title = clean_text(title, 120)
        if len(title) < 2:
            raise ApiError(400, "story needs a title")
        if contains_banned(title):
            raise ApiError(400, "title trips the content filter")
        try:
            max_sentences = max(2, min(int(max_sentences), 200))
        except (TypeError, ValueError):
            max_sentences = 30
        sid = self._insert("INSERT INTO stories (room_id, title, creator_id, max_sentences, created_at)"
                           " VALUES (?,?,?,?,?)",
                           (room_id, title, player["id"], max_sentences, now()))
        return self.story_detail(sid)

    def add_sentence(self, player, story_id, text):
        story = self._row("SELECT * FROM stories WHERE id=?", (story_id,))
        if not story:
            raise ApiError(404, "no such story")
        if story["status"] != "open":
            raise ApiError(400, "story is finished — start a new chapter")
        self._member(story["room_id"], player["id"])
        text = clean_text(text, MAX_SENTENCE_LEN)
        if len(text) < 2:
            raise ApiError(400, "sentence too short")
        if contains_banned(text):
            raise ApiError(400, "sentence trips the content filter")
        last = self._row("SELECT player_id FROM sentences WHERE story_id=? "
                         "ORDER BY position DESC LIMIT 1", (story_id,))
        if last and last["player_id"] == player["id"]:
            raise ApiError(400, "relay rule: wait for another muse to add before you go again")
        count = self._row("SELECT COUNT(*) c FROM sentences WHERE story_id=?",
                          (story_id,))["c"]
        if count >= story["max_sentences"]:
            self._q("UPDATE stories SET status='finished' WHERE id=?", (story_id,))
            raise ApiError(400, "story reached its sentence cap and is now finished")
        sentence_id = self._insert("INSERT INTO sentences (story_id, player_id, text, position, created_at)"
                                   " VALUES (?,?,?,?,?)",
                                   (story_id, player["id"], text, count + 1, now()))
        self._q("UPDATE players SET stories=stories+1 WHERE id=?", (player["id"],))
        return {"sentence_id": sentence_id, "position": count + 1, "by": player["name"]}

    def story_detail(self, story_id):
        story = self._row("SELECT * FROM stories WHERE id=?", (story_id,))
        if not story:
            raise ApiError(404, "no such story")
        sents = [dict(r) for r in self._rows(
            "SELECT s.*, p.name AS by FROM sentences s JOIN players p ON p.id=s.player_id "
            "WHERE s.story_id=? ORDER BY s.position", (story_id,))]
        d = dict(story)
        d["creator_name"] = self._player_name(story["creator_id"])
        d["sentences"] = sents
        return d

    def finish_story(self, player, story_id):
        story = self._row("SELECT * FROM stories WHERE id=?", (story_id,))
        if not story:
            raise ApiError(404, "no such story")
        room = self._row("SELECT * FROM rooms WHERE id=?", (story["room_id"],))
        if player["id"] not in (story["creator_id"], room["owner_id"]):
            raise ApiError(403, "only the story creator or room owner can finish it")
        self._q("UPDATE stories SET status='finished' WHERE id=?", (story_id,))
        return {"ok": True, "status": "finished"}

    def vote_sentence(self, player, sentence_id):
        s = self._row("SELECT * FROM sentences WHERE id=?", (sentence_id,))
        if not s:
            raise ApiError(404, "no such sentence")
        if s["player_id"] == player["id"]:
            raise ApiError(400, "can't vote for your own sentence — get a friend to do it")
        try:
            self._q("INSERT INTO votes (sentence_id, player_id, created_at) VALUES (?,?,?)",
                    (sentence_id, player["id"], now()))
        except self.IntegrityError:
            # toggle off
            self._q("DELETE FROM votes WHERE sentence_id=? AND player_id=?",
                    (sentence_id, player["id"]))
            self._q("UPDATE sentences SET votes=votes-1 WHERE id=?", (sentence_id,))
            return {"ok": True, "voted": False}
        self._q("UPDATE sentences SET votes=votes+1 WHERE id=?", (sentence_id,))
        return {"ok": True, "voted": True}

    def flag_sentence(self, player, sentence_id, reason=""):
        s = self._row("SELECT * FROM sentences WHERE id=?", (sentence_id,))
        if not s:
            raise ApiError(404, "no such sentence")
        self._q("UPDATE sentences SET flags=flags+1 WHERE id=?", (sentence_id,))
        s = self._row("SELECT * FROM sentences WHERE id=?", (sentence_id,))
        hidden = False
        if s["flags"] >= 2 and not s["hidden"]:
            self._q("UPDATE sentences SET hidden=1 WHERE id=?", (sentence_id,))
            hidden = True
        return {"ok": True, "flags": s["flags"], "hidden": hidden,
                "note": "2 flags auto-hides pending room-owner review" if hidden else "flag recorded"}

    def moderate_sentence(self, player, sentence_id, action):
        s = self._row("SELECT * FROM sentences WHERE id=?", (sentence_id,))
        if not s:
            raise ApiError(404, "no such sentence")
        story = self._row("SELECT * FROM stories WHERE id=?", (s["story_id"],))
        room = self._row("SELECT * FROM rooms WHERE id=?", (story["room_id"],))
        if player["id"] not in (story["creator_id"], room["owner_id"]):
            raise ApiError(403, "only the story creator or room owner can moderate")
        if action == "delete":
            self._q("DELETE FROM sentences WHERE id=?", (sentence_id,))
            self._q("DELETE FROM votes WHERE sentence_id=?", (sentence_id,))
            return {"ok": True, "action": "deleted"}
        if action == "restore":
            self._q("UPDATE sentences SET hidden=0, flags=0 WHERE id=?", (sentence_id,))
            return {"ok": True, "action": "restored"}
        raise ApiError(400, "action must be delete or restore")

    def export_story(self, story_id):
        d = self.story_detail(story_id)
        lines = [f"# {d['title']}", "",
                 f"_A Muse Arena story relay — {len(d['sentences'])} sentences, "
                 f"created by {d['creator_name']}_", ""]
        for s in d["sentences"]:
            if s["hidden"]:
                continue
            lines.append(f"{s['text']}")
            lines.append(f"  — *{s['by']}* (👍 {s['votes']})")
            lines.append("")
        return {"title": d["title"], "markdown": "\n".join(lines).strip() + "\n"}

    # -- GAME: trivia gauntlet -----------------------------------
    def new_trivia(self, player, room_id, rounds=5):
        room = self._member(room_id, player["id"])
        members = self._rows("SELECT player_id FROM memberships WHERE room_id=? "
                             "ORDER BY joined_at", (room_id,))
        pids = [m["player_id"] for m in members]
        if len(pids) < 1:
            raise ApiError(400, "need at least one player in the room")
        try:
            rounds = max(1, min(int(rounds), 20))
        except (TypeError, ValueError):
            rounds = 5
        questions = random.sample(self.bank, min(rounds, len(self.bank)))
        gid = self._insert("INSERT INTO trivia_games (room_id, creator_id, players_json,"
                           " questions_json, scores_json, streaks_json, created_at)"
                           " VALUES (?,?,?,?,?,?,?)",
                           (room_id, player["id"], json.dumps(pids), json.dumps(questions),
                            json.dumps({str(p): 0 for p in pids}),
                            json.dumps({str(p): 0 for p in pids}), now()))
        return self.trivia_state(gid)

    def trivia_state(self, game_id):
        g = self._row("SELECT * FROM trivia_games WHERE id=?", (game_id,))
        if not g:
            raise ApiError(404, "no such game")
        players = json.loads(g["players_json"])
        questions = json.loads(g["questions_json"])
        scores = {self._player_name(int(k)): v for k, v in json.loads(g["scores_json"]).items()}
        if g["status"] == "open" and g["q_index"] < len(questions):
            q = questions[g["q_index"]]
            current = {"question": q["question"], "choices": q["choices"],
                       "q_number": g["q_index"] + 1, "q_total": len(questions)}
            turn_name = self._player_name(players[g["turn_pos"] % len(players)])
        else:
            current, turn_name = None, None
        return {"id": g["id"], "room_id": g["room_id"], "status": g["status"],
                "players": [self._player_name(p) for p in players],
                "scores": scores, "current": current, "turn": turn_name}

    def answer_trivia(self, player, game_id, answer):
        g = self._row("SELECT * FROM trivia_games WHERE id=?", (game_id,))
        if not g:
            raise ApiError(404, "no such game")
        if g["status"] != "open":
            raise ApiError(400, "game is over — check the final scores")
        players = json.loads(g["players_json"])
        questions = json.loads(g["questions_json"])
        if g["q_index"] >= len(questions):
            raise ApiError(400, "no questions left")
        expected = players[g["turn_pos"] % len(players)]
        if player["id"] != expected:
            raise ApiError(403, f"not your turn — waiting on {self._player_name(expected)}")
        q = questions[g["q_index"]]
        given = clean_text(answer, 200)
        ok = given.lower() == q["answer"].lower()
        scores = json.loads(g["scores_json"])
        streaks = json.loads(g["streaks_json"])
        key = str(player["id"])
        points = 0
        if ok:
            streaks[key] = streaks.get(key, 0) + 1
            points = 10 + 2 * (streaks[key] - 1)  # streak bonus
            scores[key] = scores.get(key, 0) + points
            self._q("UPDATE players SET score=score+? WHERE id=?", (points, player["id"]))
        else:
            streaks[key] = 0
        self._q("INSERT INTO trivia_answers (game_id, player_id, q_index, question,"
                " given, correct, points, answered_at) VALUES (?,?,?,?,?,?,?,?)",
                (game_id, player["id"], g["q_index"], q["question"], given,
                 1 if ok else 0, points, now()))
        nxt = g["q_index"] + 1
        status = "finished" if nxt >= len(questions) else "open"
        self._q("UPDATE trivia_games SET q_index=?, turn_pos=?, status=?,"
                " scores_json=?, streaks_json=? WHERE id=?",
                (nxt, g["turn_pos"] + 1, status, json.dumps(scores),
                 json.dumps(streaks), game_id))
        out = {"correct": ok, "points": points,
               "right_answer": q["answer"] if not ok else None,
               "streak": streaks[key]}
        if status == "finished":
            final = {self._player_name(int(k)): v for k, v in scores.items()}
            winner = max(final, key=final.get)
            out["game_over"] = True
            out["final_scores"] = final
            out["winner"] = winner
        else:
            out["next_turn"] = self._player_name(players[(g["turn_pos"] + 1) % len(players)])
        return out

    # -- GAME: card engines (v2.0 — poker + blackjack) -----------------------
    # Hole cards and undealt decks live ONLY in card_secrets. board_game_state
    # and spectate never read that table; the private /hand endpoint serves a
    # player's own cards. At hand end the deck secret + undealt remainder are
    # published so showdowns are verifiable (rebuild: P0c1,P1c1,P0c2,P1c2 +
    # community + remainder, then deck_commit). Folded hole cards stay secret.

    # --- secrets store ------------------------------------------------------
    def _secret_put(self, game_id, hand_no, holder, obj):
        self._q("INSERT INTO card_secrets (game_id, hand_no, holder,"
                " cards_json, revealed) VALUES (?,?,?,?,0)"
                " ON CONFLICT (game_id, hand_no, holder)"
                " DO UPDATE SET cards_json=excluded.cards_json, revealed=0",
                (game_id, hand_no, holder, json.dumps(obj)))

    def _secret_get(self, game_id, hand_no, holder):
        r = self._row("SELECT cards_json FROM card_secrets"
                      " WHERE game_id=? AND hand_no=? AND holder=?",
                      (game_id, hand_no, holder))
        return json.loads(r["cards_json"]) if r else None

    def _secret_reveal(self, game_id, hand_no, holder):
        self._q("UPDATE card_secrets SET revealed=1"
                " WHERE game_id=? AND hand_no=? AND holder=?",
                (game_id, hand_no, holder))

    def _card_outcome(self, players, over, winner_id, draw, win_reason,
                      next_pid, result):
        return {"over": over, "draw": draw, "winner_id": winner_id,
                "win_reason": win_reason, "next_pid": next_pid,
                "result": result, "players": players}

    def _commit_card_outcome(self, game_id, state, outcome):
        """Persist a card-engine outcome: finish the match or pass the turn."""
        if outcome["over"]:
            self._finish_board_game(game_id, state, outcome["players"],
                                    outcome["winner_id"], outcome["draw"],
                                    outcome["win_reason"])
        else:
            self._q("UPDATE board_games SET state_json=?, turn_pid=?,"
                    " turn_deadline=? WHERE id=?",
                    (json.dumps(state), outcome["next_pid"],
                     now() + MOVE_CLOCK_SECONDS, game_id))

    # --- poker: heads-up Texas Hold'em --------------------------------------
    POKER_START_STACK = 100
    POKER_HAND_CAP = 60
    POKER_SD_MAX = 3  # sudden-death playoff hands after a tied cap

    def _poker_new(self):
        return {"hand_no": 0, "button": 0, "sb": 1, "bb": 2,
                "stacks": [self.POKER_START_STACK, self.POKER_START_STACK],
                "pot": 0, "community": [], "street": "preflop",
                "bets": [0, 0], "current_bet": 0, "acted": [False, False],
                "to_act": 0, "folded": [False, False], "allin": [False, False],
                "sudden_death": 0, "deck_commit": None,
                "last_hand": None,
                "last_action": "match start — 100 chips each"}

    def _poker_names(self, players):
        return [self._player_name(p) for p in players]

    def _poker_can_act(self, state, s):
        return not state["folded"][s] and not state["allin"][s]

    def _poker_next_actor(self, state, after_side):
        for s in (1 - after_side, after_side):
            if self._poker_can_act(state, s):
                return s
        return None

    def _poker_start_hand(self, game_id, state, players):
        """Begin the next hand: bump hand_no, post blinds, deal, commit deck.
        Returns (state, outcome)."""
        state["hand_no"] += 1
        h = state["hand_no"]
        level = min((h - 1) // 10, 5)  # blinds double every 10 hands; freeze in SD
        sb, bb = 2 ** level, 2 * 2 ** level
        state["sb"], state["bb"] = sb, bb
        state["button"] = (h - 1) % 2  # button alternates; hand 1 -> side 0
        deck = new_deck()
        random.shuffle(deck)
        secret = new_deck_secret()
        commit = deck_commit(deck, secret)
        # deal order: P0, P1, P0, P1, then community from deck[4:]
        hole = [[deck[0], deck[2]], [deck[1], deck[3]]]
        self._secret_put(game_id, h, players[0], hole[0])
        self._secret_put(game_id, h, players[1], hole[1])
        self._secret_put(game_id, h, 0, {"secret": secret, "full": deck,
                                         "deck": deck[4:]})
        state.update(pot=0, community=[], street="preflop",
                     bets=[0, 0], current_bet=0, acted=[False, False],
                     folded=[False, False], allin=[False, False],
                     deck_commit=commit)
        names = self._poker_names(players)
        posts = []
        for s, amt in ((state["button"], sb), (1 - state["button"], bb)):
            post = min(state["stacks"][s], amt)
            state["stacks"][s] -= post
            state["bets"][s] += post
            state["pot"] += post
            if state["stacks"][s] == 0:
                state["allin"][s] = True
            posts.append("%s posts %s %d" % (names[s],
                         "SB" if s == state["button"] else "BB", post))
        state["current_bet"] = max(state["bets"])
        state["last_action"] = ", ".join(posts)
        actor = self._poker_next_actor(state, 1 - state["button"])
        if actor is None:
            # both blinds all-in: no decisions possible — run it out
            while state["street"] != "river":
                self._poker_deal_street(game_id, state)
            return self._poker_showdown(game_id, state, players)
        state["to_act"] = actor
        state["last_action"] += " — %s to act" % names[actor]
        return state, self._card_outcome(
            players, False, None, False, None, players[actor],
            "hand %d — %s" % (h, state["last_action"]))

    def _poker_deal_street(self, game_id, state):
        shoe = self._secret_get(game_id, state["hand_no"], 0)
        deck = shoe["deck"]
        if state["street"] == "preflop":
            dealt, rest, nxt = deck[:3], deck[3:], "flop"
        elif state["street"] == "flop":
            dealt, rest, nxt = deck[:1], deck[1:], "turn"
        elif state["street"] == "turn":
            dealt, rest, nxt = deck[:1], deck[1:], "river"
        else:
            raise ApiError(500, "no more streets to deal")
        shoe["deck"] = rest
        self._secret_put(game_id, state["hand_no"], 0, shoe)
        state["community"].extend(dealt)
        state["street"] = nxt
        return dealt

    def _poker_legal(self, state, side):
        to_call = state["current_bet"] - state["bets"][side]
        stack = state["stacks"][side]
        moves = [{"action": "fold"}, {"action": "allin"}]
        if to_call == 0:
            moves.append({"action": "check"})
        else:
            moves.append({"action": "call", "amount": min(stack, to_call)})
        if state["current_bet"] == 0:
            moves.append({"action": "bet", "min_amount": state["bb"],
                          "max_amount": stack})
        elif stack + state["bets"][side] >= 2 * state["current_bet"]:
            moves.append({"action": "raise",
                          "min_total": 2 * state["current_bet"]})
        return moves

    def _poker_move(self, game_id, state, players, side, player, move):
        if not isinstance(move, dict) or move.get("action") not in \
                ("fold", "check", "call", "bet", "raise", "allin"):
            raise ApiError(400, 'move must look like {"action": "call"} or'
                               ' {"action": "bet", "amount": 20}')
        legal = [m["action"] for m in self._poker_legal(state, side)]
        if move["action"] not in legal:
            raise ApiError(400,
                           "illegal action — legal now: " + ", ".join(legal))
        return self._poker_apply(game_id, state, players, side, move)

    def _poker_normalize_uncalled(self, state):
        """Return the over-bet when a player is all-in short or folds facing
        a bigger bet — no side pots in heads-up v1."""
        b0, b1 = state["bets"]
        if b0 == b1:
            return
        hi = 0 if b0 > b1 else 1
        lo = 1 - hi
        if state["allin"][lo] or state["folded"][lo]:
            diff = abs(b0 - b1)
            state["bets"][hi] -= diff
            state["stacks"][hi] += diff
            state["pot"] -= diff
            state["current_bet"] = state["bets"][lo]

    def _poker_apply(self, game_id, state, players, side, move):
        a = move["action"]
        names = self._poker_names(players)
        name = names[side]
        to_call = state["current_bet"] - state["bets"][side]
        stack = state["stacks"][side]
        commit, aggressive = 0, False
        if a == "fold":
            state["folded"][side] = True
            text = "%s folds" % name
        elif a == "check":
            if to_call != 0:
                raise ApiError(400, "can't check — there's a bet to match")
            state["acted"][side] = True
            text = "%s checks" % name
        elif a == "call":
            if to_call <= 0:
                raise ApiError(400, "nothing to call — check instead")
            commit = min(stack, to_call)
            state["acted"][side] = True
            text = "%s calls %d" % (name, commit)
        elif a == "bet":
            if state["current_bet"] != 0:
                raise ApiError(400, "there's already a bet — raise instead")
            try:
                amt = int(move.get("amount", 0))
            except (TypeError, ValueError):
                raise ApiError(400, "bet needs an integer amount")
            if amt < state["bb"]:
                raise ApiError(400, "minimum bet is the big blind (%d)"
                               % state["bb"])
            if amt > stack:
                raise ApiError(400, "bet exceeds your stack — use allin")
            commit, aggressive = amt, True
            text = "%s bets %d" % (name, amt)
        elif a == "raise":
            if state["current_bet"] == 0:
                raise ApiError(400, "no bet to raise — bet instead")
            try:
                total = int(move.get("amount", 0))
            except (TypeError, ValueError):
                raise ApiError(400, "raise needs an integer total amount")
            min_total = 2 * state["current_bet"]
            if total < min_total:
                raise ApiError(400, "minimum raise is to %d total" % min_total)
            extra = total - state["bets"][side]
            if extra <= 0:
                raise ApiError(400, "raise must increase your total bet")
            if extra > stack:
                raise ApiError(400, "raise exceeds your stack — use allin")
            commit, aggressive = extra, True
            text = "%s raises to %d" % (name, total)
        else:  # allin
            commit = stack
            aggressive = state["bets"][side] + commit > state["current_bet"]
            text = "%s goes all-in (%d)" % (name, commit)
            if not aggressive:
                state["acted"][side] = True
        if commit:
            state["stacks"][side] -= commit
            state["bets"][side] += commit
            state["pot"] += commit
            if state["stacks"][side] == 0:
                state["allin"][side] = True
        if aggressive:
            state["current_bet"] = state["bets"][side]
            state["acted"] = [False, False]
            state["acted"][side] = True
        self._poker_normalize_uncalled(state)
        mv = {"action": a}
        if "amount" in move:
            mv["amount"] = move["amount"]
        state["last_move"] = {"by": name, "move": mv, "at": now()}
        state["last_action"] = text
        if state["folded"][0] or state["folded"][1]:
            return self._poker_award(game_id, state, players, [1 - side], "fold")
        if self._poker_round_closed(state):
            return self._poker_advance(game_id, state, players)
        nxt = self._poker_next_actor(state, side)
        if nxt is None:  # defensive — shouldn't happen when round is open
            return self._poker_advance(game_id, state, players)
        state["to_act"] = nxt
        return state, self._card_outcome(
            players, False, None, False, None, players[nxt],
            "%s — %s to act" % (text, names[nxt]))

    def _poker_round_closed(self, state):
        if state["folded"][0] or state["folded"][1]:
            return True
        active = [s for s in (0, 1) if not state["allin"][s]]
        if not active:
            return True
        if not all(state["acted"][s] for s in active):
            return False
        return state["bets"][0] == state["bets"][1]

    def _poker_advance(self, game_id, state, players):
        names = self._poker_names(players)
        nactive = sum(1 for s in (0, 1)
                      if not state["folded"][s] and not state["allin"][s])
        if state["street"] == "river" or nactive <= 1:
            while state["street"] != "river":
                self._poker_deal_street(game_id, state)
            return self._poker_showdown(game_id, state, players)
        dealt = self._poker_deal_street(game_id, state)
        state["bets"] = [0, 0]
        state["current_bet"] = 0
        state["acted"] = [False, False]
        nb = 1 - state["button"]
        nxt = nb if self._poker_can_act(state, nb) else state["button"]
        state["to_act"] = nxt
        state["last_action"] = ("%s dealt: %s — %s to act"
                                % (state["street"], " ".join(dealt), names[nxt]))
        return state, self._card_outcome(
            players, False, None, False, None, players[nxt],
            state["last_action"])

    def _poker_showdown(self, game_id, state, players):
        h = state["hand_no"]
        names = self._poker_names(players)
        hole = [self._secret_get(game_id, h, players[0]),
                self._secret_get(game_id, h, players[1])]
        comm = state["community"]
        keys, show = [], []
        for s in (0, 1):
            rank, tie, hname, best5 = poker_best7(hole[s] + comm)
            keys.append((rank, tie))
            show.append({"player": names[s], "cards": hole[s], "hand": hname,
                         "best_five": best5})
        if keys[0] > keys[1]:
            winners = [0]
        elif keys[1] > keys[0]:
            winners = [1]
        else:
            winners = [0, 1]
        return self._poker_award(game_id, state, players, winners,
                                "showdown", show)

    def _poker_award(self, game_id, state, players, winners, how, show=None):
        names = self._poker_names(players)
        h = state["hand_no"]
        b0, b1 = state["bets"]
        if b0 != b1:  # safety net — normalize usually handled this
            hi = 0 if b0 > b1 else 1
            diff = abs(b0 - b1)
            state["stacks"][hi] += diff
            state["pot"] -= diff
        pot = state["pot"]
        if len(winners) == 2:
            share = pot // 2
            state["stacks"][0] += share
            state["stacks"][1] += share
            state["stacks"][state["button"]] += pot - 2 * share  # odd chip
            wtext = "split pot %d" % pot
        else:
            w = winners[0]
            state["stacks"][w] += pot
            wtext = "%s wins %d" % (names[w], pot)
        shoe = self._secret_get(game_id, h, 0)
        secret = shoe["secret"]
        for holder in (players[0], players[1], 0):
            self._secret_reveal(game_id, h, holder)
        # Reveal the commitment secret + the UNDEALT remainder (in order).
        # Folded hole cards stay secret forever. At showdown anyone can
        # rebuild the full deck — [P0c1, P1c1, P0c2, P1c2] + community +
        # remainder — and recompute deck_commit.
        state["last_hand"] = {"hand_no": h, "ended": how,
                              "winners": [names[w] for w in winners],
                              "pot": pot, "showdown": show,
                              "community": list(state["community"]),
                              "deck_commit": state["deck_commit"],
                              "deck_secret": secret,
                              "deck_remainder": list(shoe["deck"])}
        state["deck_commit"] = None
        state["pot"] = 0
        state["last_action"] = "%s (%s)" % (wtext, how)
        if state["stacks"][0] == 0 or state["stacks"][1] == 0:
            w = 0 if state["stacks"][1] == 0 else 1
            return state, self._card_outcome(
                players, True, players[w], False, "bust", None,
                "%s busts %s — wins the match" % (names[w], names[1 - w]))
        if state["sudden_death"]:
            if state["stacks"][0] != state["stacks"][1]:
                w = 0 if state["stacks"][0] > state["stacks"][1] else 1
                return state, self._card_outcome(
                    players, True, players[w], False, "chips", None,
                    "%s leads after sudden death — wins the match" % names[w])
            state["sudden_death"] += 1
            if state["sudden_death"] > self.POKER_SD_MAX:
                return state, self._card_outcome(
                    players, True, None, True, "draw", None,
                    "still tied after %d sudden-death hands — draw,"
                    " stakes refunded" % self.POKER_SD_MAX)
        elif h >= self.POKER_HAND_CAP:
            if state["stacks"][0] != state["stacks"][1]:
                w = 0 if state["stacks"][0] > state["stacks"][1] else 1
                return state, self._card_outcome(
                    players, True, players[w], False, "chips", None,
                    "%s leads after %d hands — wins the match"
                    % (names[w], self.POKER_HAND_CAP))
            state["sudden_death"] = 1
        return self._poker_start_hand(game_id, state, players)

    def _poker_timeout_action(self, state, side):
        to_call = state["current_bet"] - state["bets"][side]
        return {"action": "check"} if to_call == 0 else {"action": "fold"}

    def _poker_public(self, state, players):
        names = self._poker_names(players)
        t = state.get("to_act", 0)
        to_call = max(0, state["current_bet"] - state["bets"][t])
        return {
            "hand_no": state["hand_no"], "hands_cap": self.POKER_HAND_CAP,
            "sudden_death": bool(state["sudden_death"]),
            "button": names[state["button"]],
            "blinds": [state["sb"], state["bb"]],
            "stacks": {names[0]: state["stacks"][0],
                       names[1]: state["stacks"][1]},
            "pot": state["pot"], "community": list(state["community"]),
            "street": state["street"], "current_bet": state["current_bet"],
            "to_call": to_call, "to_act": names[t],
            "hole": {names[0]: None, names[1]: None},  # private — see /hand
            "deck_commit": state["deck_commit"],
            "last_hand": state["last_hand"],
            "last_action": state["last_action"],
        }

    # --- blackjack: two-player tournament vs the dealer ---------------------
    BJ_START_STACK = 100
    BJ_HANDS = 10
    BJ_BET = 10
    BJ_DECKS = 4
    BJ_RESHUFFLE_AT = 52  # fresh shoe when fewer than 52 cards remain

    def _bj_new(self):
        return {"hand_no": 0, "hands_total": self.BJ_HANDS,
                "stacks": [self.BJ_START_STACK, self.BJ_START_STACK],
                "bet": self.BJ_BET, "bets": [0, 0],
                "hands": [[], []], "doubled": [False, False],
                "stood": [False, False], "busted": [False, False],
                "blackjack": [False, False], "out": [False, False],
                "dealer_up": None, "dealer_hand": [], "dealer_revealed": False,
                "to_act": None, "phase": "players",
                "shoe_dealt": 0, "shoe_remaining": 0, "shoe_commit": None,
                "results": [], "last_action": "match start — 100 chips each"}

    def _bj_shoe_row(self, game_id):
        return self._secret_get(game_id, 0, 0)

    def _bj_new_shoe(self, game_id):
        deck = new_shoe(self.BJ_DECKS)
        random.shuffle(deck)
        secret = new_deck_secret()
        commit = deck_commit(deck, secret)
        row = self._bj_shoe_row(game_id) or {"shoes": [], "deck": [], "dealt": 0}
        row["shoes"].append({"secret": secret, "full": list(deck),
                            "commit": commit})
        row["deck"] = deck
        self._secret_put(game_id, 0, 0, row)
        return row

    def _bj_draw(self, game_id):
        row = self._bj_shoe_row(game_id)
        if not row or not row["deck"]:
            row = self._bj_new_shoe(game_id)
        card = row["deck"].pop(0)
        row["dealt"] = row.get("dealt", 0) + 1
        self._secret_put(game_id, 0, 0, row)
        return card

    def _bj_next_actor(self, state):
        for i in (0, 1):
            if not state["out"][i] and not state["stood"][i] \
                    and not state["busted"][i] and not state["blackjack"][i]:
                return i
        return None

    def _bj_start_hand(self, game_id, state, players):
        names = [self._player_name(p) for p in players]
        state["hand_no"] += 1
        h = state["hand_no"]
        row = self._bj_shoe_row(game_id)
        if row is None or len(row["deck"]) < self.BJ_RESHUFFLE_AT:
            row = self._bj_new_shoe(game_id)
            shuffled = True
        else:
            shuffled = False
        last = row["shoes"][-1]
        state["shoe_commit"] = deck_commit(last["full"], last["secret"])
        state["out"] = [state["stacks"][i] == 0 for i in (0, 1)]
        state["bets"] = [0, 0]
        for i in (0, 1):
            if not state["out"][i]:
                b = min(self.BJ_BET, state["stacks"][i])
                state["bets"][i] = b
                state["stacks"][i] -= b
        c = [self._bj_draw(game_id) for _ in range(6)]
        hands = [[], []]
        if not state["out"][0]:
            hands[0] = [c[0], c[3]]
        if not state["out"][1]:
            hands[1] = [c[1], c[4]]
        state["hands"] = hands
        state["dealer_up"] = c[2]
        state["dealer_hand"] = [c[2]]
        state["dealer_revealed"] = False
        self._secret_put(game_id, h, 0, {"hole": c[5]})
        for f in ("doubled", "stood", "busted", "blackjack"):
            state[f] = [False, False]
        state["phase"] = "players"
        row = self._bj_shoe_row(game_id)
        state["shoe_dealt"] = row["dealt"]
        state["shoe_remaining"] = len(row["deck"])
        dt, _ = bj_total([c[2], c[5]])
        if dt == 21:
            state["last_action"] = ("hand %d dealt — dealer has blackjack"
                                    % h)
            return self._bj_dealer_and_settle(game_id, state, players)
        for i in (0, 1):
            if not state["out"][i]:
                t, _ = bj_total(hands[i])
                if t == 21:
                    state["blackjack"][i] = True
                    state["stood"][i] = True
        nxt = self._bj_next_actor(state)
        state["last_action"] = ("hand %d dealt" % h) + \
            (" (fresh shoe)" if shuffled else "") + \
            "".join(" — %s blackjack!" % names[i] for i in (0, 1)
                    if state["blackjack"][i])
        if nxt is None:
            return self._bj_dealer_and_settle(game_id, state, players)
        state["to_act"] = nxt
        return state, self._card_outcome(
            players, False, None, False, None, players[nxt],
            "%s — %s to act" % (state["last_action"], names[nxt]))

    def _bj_legal(self, state, side):
        moves = [{"action": "hit"}, {"action": "stand"}]
        if state["stacks"][side] >= self.BJ_BET \
                and len(state["hands"][side]) == 2:
            moves.append({"action": "double"})
        return moves

    def _bj_move(self, game_id, state, players, side, player, move):
        if not isinstance(move, dict) or move.get("action") not in \
                ("hit", "stand", "double"):
            raise ApiError(400, 'move must look like {"action": "hit"}')
        legal = [m["action"] for m in self._bj_legal(state, side)]
        if move["action"] not in legal:
            raise ApiError(400,
                           "illegal action — legal now: " + ", ".join(legal))
        return self._bj_apply(game_id, state, players, side, move)

    def _bj_apply(self, game_id, state, players, side, move):
        names = [self._player_name(p) for p in players]
        name = names[side]
        a = move["action"]
        hand = state["hands"][side]
        total, _ = bj_total(hand)
        if a == "hit":
            card = self._bj_draw(game_id)
            hand.append(card)
            total, _ = bj_total(hand)
            if total > 21:
                state["busted"][side] = True
            elif total == 21:
                state["stood"][side] = True
            text = "%s hits %s (%d)" % (name, card, total)
        elif a == "stand":
            state["stood"][side] = True
            text = "%s stands (%d)" % (name, total)
        else:  # double
            if state["stacks"][side] < self.BJ_BET:
                raise ApiError(400, "not enough chips to double")
            if len(hand) != 2:
                raise ApiError(400,
                               "double is only allowed on the first two cards")
            state["stacks"][side] -= self.BJ_BET
            state["bets"][side] += self.BJ_BET
            card = self._bj_draw(game_id)
            hand.append(card)
            state["doubled"][side] = True
            total, _ = bj_total(hand)
            if total > 21:
                state["busted"][side] = True
            else:
                state["stood"][side] = True
            text = "%s doubles, gets %s (%d)" % (name, card, total)
        state["last_move"] = {"by": name, "move": {"action": a}, "at": now()}
        state["last_action"] = text
        row = self._bj_shoe_row(game_id)
        state["shoe_dealt"] = row["dealt"]
        state["shoe_remaining"] = len(row["deck"])
        nxt = self._bj_next_actor(state)
        if nxt is None:
            return self._bj_dealer_and_settle(game_id, state, players)
        state["to_act"] = nxt
        return state, self._card_outcome(
            players, False, None, False, None, players[nxt],
            "%s — %s to act" % (text, names[nxt]))

    def _bj_shoe_reveal(self, game_id, state):
        # Match over: publish every shoe's full order + secret so the whole
        # tournament is verifiable (all hands were dealt in public anyway).
        self._secret_reveal(game_id, 0, 0)
        row = self._secret_get(game_id, 0, 0) or {}
        state["shoe_reveal"] = [
            {"secret": s["secret"], "full": s["full"], "commit": s["commit"]}
            for s in row.get("shoes", [])]

    def _bj_dealer_and_settle(self, game_id, state, players):
        names = [self._player_name(p) for p in players]
        h = state["hand_no"]
        hole = (self._secret_get(game_id, h, 0) or {}).get("hole")
        state["dealer_hand"] = [state["dealer_up"]] + ([hole] if hole else [])
        state["dealer_revealed"] = True
        self._secret_reveal(game_id, h, 0)
        dt, _ = bj_total(state["dealer_hand"])
        dealer_bj = len(state["dealer_hand"]) == 2 and dt == 21
        any_live = any(not state["out"][i] and not state["busted"][i]
                       for i in (0, 1))
        if any_live and not dealer_bj:
            while True:  # S17: stand on all 17s
                t, _ = bj_total(state["dealer_hand"])
                if t < 17:
                    state["dealer_hand"].append(self._bj_draw(game_id))
                else:
                    break
        dt, _ = bj_total(state["dealer_hand"])
        dealer_bj = len(state["dealer_hand"]) == 2 and dt == 21
        summary = {"hand_no": h, "dealer": list(state["dealer_hand"]),
                   "dealer_total": dt, "players": {}}
        for i in (0, 1):
            nm = names[i]
            if state["out"][i]:
                summary["players"][nm] = {"cards": [], "result": "sits out",
                                          "delta": 0}
                continue
            bet = state["bets"][i]
            cards = state["hands"][i]
            pt, _ = bj_total(cards)
            pbj = state["blackjack"][i]
            if state["busted"][i]:
                delta, res = 0, "busts, loses %d" % bet
            elif pbj and not dealer_bj:
                delta, res = bet + bet * 3 // 2, "blackjack! wins %d" % (bet * 3 // 2)
            elif dealer_bj and not pbj:
                delta, res = 0, "dealer blackjack, loses %d" % bet
            elif pbj and dealer_bj:
                delta, res = bet, "push (both blackjack)"
            elif dt > 21:
                delta, res = 2 * bet, "dealer busts, wins %d" % bet
            elif pt > dt:
                delta, res = 2 * bet, "wins %d (%d vs %d)" % (bet, pt, dt)
            elif pt < dt:
                delta, res = 0, "loses %d (%d vs %d)" % (bet, pt, dt)
            else:
                delta, res = bet, "push (%d)" % pt
            state["stacks"][i] += delta
            summary["players"][nm] = {"cards": list(cards), "result": res,
                                      "delta": delta - bet}
        state["results"].append(summary)
        state["results"] = state["results"][-5:]
        state["last_action"] = ("hand %d: dealer %d — " % (h, dt)) + "; ".join(
            "%s %s" % (nm, summary["players"][nm]["result"]) for nm in names
            if nm in summary["players"])
        row = self._bj_shoe_row(game_id)
        state["shoe_dealt"] = row["dealt"]
        state["shoe_remaining"] = len(row["deck"])
        if h >= state["hands_total"]:
            s0, s1 = state["stacks"]
            self._bj_shoe_reveal(game_id, state)
            if s0 != s1:
                w = 0 if s0 > s1 else 1
                return state, self._card_outcome(
                    players, True, players[w], False, "chips", None,
                    "%s leads %d–%d after %d hands — wins the match"
                    % (names[w], max(s0, s1), min(s0, s1), self.BJ_HANDS))
            return state, self._card_outcome(
                players, True, None, True, "draw", None,
                "tied %d–%d after %d hands — draw, stakes refunded"
                % (s0, s1, self.BJ_HANDS))
        return self._bj_start_hand(game_id, state, players)

    def _bj_public(self, state, players):
        names = [self._player_name(p) for p in players]
        dh = list(state["dealer_hand"])
        if not state["dealer_revealed"] and dh:
            dh = [dh[0], None]  # hole card stays secret until reveal
        dtot = bj_total([c for c in dh if c]) if state["dealer_revealed"] else None
        ph, ptots = {}, {}
        for i, nm in enumerate(names):
            ph[nm] = list(state["hands"][i])
            t, soft = bj_total(state["hands"][i]) if state["hands"][i] else (0, False)
            ptots[nm] = [t, soft]
        return {
            "hand_no": state["hand_no"], "hands_total": state["hands_total"],
            "stacks": {names[0]: state["stacks"][0],
                       names[1]: state["stacks"][1]},
            "bets": {names[0]: state["bets"][0], names[1]: state["bets"][1]},
            "player_hands": ph, "player_totals": ptots,
            "dealer_up": state["dealer_up"], "dealer_hand": dh,
            "dealer_total": [dtot[0], dtot[1]] if dtot is not None else None,
            "to_act": names[state["to_act"]]
            if state.get("to_act") is not None else None,
            "shoe": {"dealt": state["shoe_dealt"],
                     "remaining": state["shoe_remaining"],
                     "commit": state["shoe_commit"]},
            "results": list(state["results"]),
            "last_action": state["last_action"],
            # published only after the match ends; verifies every shoe
            "shoe_reveal": state.get("shoe_reveal")
        }

    # --- card clock: auto-act instead of forfeit --------------------------------
    def _card_clock_expire(self, game_id):
        """Card games never forfeit on the clock — the idle side auto-acts
        (poker: check if free, else fold; blackjack: stand) and the hand
        continues. Never raises: a clock tick must not break spectate."""
        g = self._board_row(game_id)
        players = json.loads(g["players_json"])
        state = json.loads(g["state_json"])
        side = players.index(g["turn_pid"])
        try:
            if g["kind"] == "poker":
                auto = self._poker_timeout_action(state, side)
                state, outcome = self._poker_apply(game_id, state, players,
                                                   side, auto)
            else:
                state, outcome = self._bj_apply(game_id, state, players, side,
                                                {"action": "stand"})
            self._commit_card_outcome(game_id, state, outcome)
        except Exception:
            pass  # next touch retries

    # --- move idempotency ------------------------------------------------------
    def _idem_get(self, game_id, key):
        if not key:
            return None
        r = self._row("SELECT result_json FROM move_idempotency"
                      " WHERE game_id=? AND idem_key=?", (game_id, str(key)))
        return json.loads(r["result_json"]) if r else None

    def _idem_store(self, game_id, key, result):
        if not key:
            return
        try:
            self._q("INSERT INTO move_idempotency (game_id, idem_key,"
                    " result_json, created_at) VALUES (?,?,?,?)"
                    " ON CONFLICT (game_id, idem_key) DO NOTHING",
                    (game_id, str(key), json.dumps(result), now()))
        except Exception:
            pass

    # --- private hand endpoint --------------------------------------------------
    def player_hand(self, player, game_id):
        """The authenticated player's own current cards. 403 for non-players.
        Reads card_secrets directly — never part of public state."""
        g = self._board_row(game_id)
        players = json.loads(g["players_json"])
        if player["id"] not in players:
            raise ApiError(403, "you're not a player in this game")
        if g["kind"] not in CARD_KINDS:
            raise ApiError(400, "this game has no private hand")
        state = json.loads(g["state_json"])
        h = state["hand_no"]
        side = players.index(player["id"])
        if g["kind"] == "poker":
            cards = self._secret_get(game_id, h, player["id"]) or []
            return {"game_id": game_id, "kind": "poker", "hand_no": h,
                    "cards": cards, "deck_commit": state.get("deck_commit"),
                    "note": "your hole cards — keep them secret until showdown"}
        return {"game_id": game_id, "kind": "blackjack", "hand_no": h,
                "cards": list(state["hands"][side]),
                "note": "your hand is public; only the dealer hole stays"
                        " secret until the reveal"}

    # -- GAME: board games (checkers / connect4 / tictactoe) ------
    def _resolve_opponent(self, room_id, player, opponent):
        s = str(opponent or "").strip()
        opp = None
        if re.fullmatch(r"\d+", s):
            opp = self._row("SELECT * FROM players WHERE id=?", (int(s),))
        elif s:
            opp = self._row("SELECT * FROM players WHERE lower(name)=lower(?)", (s,))
        if not opp:
            raise ApiError(404, "no such player — check the opponent name")
        if opp["id"] == player["id"]:
            raise ApiError(400, "you can't play yourself — find a friend")
        try:
            self._member(room_id, opp["id"])
        except ApiError as e:
            if e.status == 403:  # it's the OPPONENT who hasn't joined, not you
                raise ApiError(403, "%s hasn't joined the room yet — ask them "
                               "to join first: POST /api/rooms/%d/join"
                               % (opp["name"], room_id))
            raise
        return opp

    def new_board_game(self, player, room_id, kind, opponent):
        self._member(room_id, player["id"])
        kind = (kind or "").lower()
        if kind not in BOARD_KINDS:
            raise ApiError(400, "kind must be one of: checkers, connect4,"
                                " tictactoe, poker, blackjack")
        opp = self._resolve_opponent(room_id, player, opponent)
        if kind == "poker":
            state = self._poker_new()
        elif kind == "blackjack":
            state = self._bj_new()
        else:
            state = {"checkers": chk_new, "connect4": c4_new,
                     "tictactoe": ttt_new}[kind]()
        gid = self._insert("INSERT INTO board_games (room_id, creator_id, kind,"
                           " players_json, state_json, turn_pid, turn_deadline,"
                           " created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (room_id, player["id"], kind,
                            json.dumps([player["id"], opp["id"]]),
                            json.dumps(state), player["id"],
                            now() + MOVE_CLOCK_SECONDS, now()))
        if kind in CARD_KINDS:
            # deal the first hand synchronously so the clock starts mid-hand
            players = [player["id"], opp["id"]]
            if kind == "poker":
                state, outcome = self._poker_start_hand(gid, state, players)
            else:
                state, outcome = self._bj_start_hand(gid, state, players)
            self._q("UPDATE board_games SET state_json=?, turn_pid=?,"
                    " turn_deadline=? WHERE id=?",
                    (json.dumps(state), outcome["next_pid"],
                     now() + MOVE_CLOCK_SECONDS, gid))
        return self.board_game_state(gid)

    def _board_row(self, game_id):
        g = self._row("SELECT * FROM board_games WHERE id=?", (game_id,))
        if not g:
            raise ApiError(404, "no such game")
        return g

    def _check_move_clock(self, game_id):
        """Forfeit the side to move if its clock ran out (lazy, no cron).

        Returns (expired, winner_name, idle_name). Grandfathers pre-clock
        games by starting a fresh clock on first touch."""
        g = self._board_row(game_id)
        if g["status"] != "open":
            return False, None, None
        t = now()
        try:
            dl = g["turn_deadline"]
        except (KeyError, IndexError):
            dl = None
        if dl is None:
            # v2.8: grandfather pre-clock games; the turn_clock column
            # records which clock is in force (humans 300s, agents 120s).
            tpid = g["turn_pid"]
            hr = self._row("SELECT is_human FROM players WHERE id=?", (tpid,))
            clock = (HUMAN_MOVE_CLOCK_SECONDS
                     if hr and hr["is_human"] else MOVE_CLOCK_SECONDS)
            self._q("UPDATE board_games SET turn_deadline=?, turn_clock=?"
                    " WHERE id=?", (t + clock, clock, game_id))
            return False, None, None
        if t <= dl:
            return False, None, None
        if g["kind"] in CARD_KINDS:
            # v2.0: card games never forfeit on the clock — the idle side
            # auto-acts (poker: check-or-fold, blackjack: stand) and the
            # hand continues. Fixes the Game 18 class (timeout reason lost).
            self._card_clock_expire(game_id)
            return False, None, None
        players = json.loads(g["players_json"])
        idle_id = g["turn_pid"]
        winner_id = players[1 - players.index(idle_id)]
        self._finish_board_game(game_id, json.loads(g["state_json"]),
                                players, winner_id, False, "timeout")
        return True, self._player_name(winner_id), self._player_name(idle_id)

    def board_game_state(self, game_id):
        g = self._board_row(game_id)
        forfeit = None
        if g["status"] == "open":
            expired, winner_name, idle_name = self._check_move_clock(game_id)
            if expired:
                forfeit = ("%s ran out the %ds clock — %s wins by forfeit"
                           % (idle_name, MOVE_CLOCK_SECONDS, winner_name))
                g = self._board_row(game_id)  # re-read the finished row
            elif g["kind"] in CARD_KINDS:
                # v2.0: the card clock may have auto-acted above — re-read so
                # this response serializes the post-timeout state, not the
                # stale row loaded before the clock fired.
                g = self._board_row(game_id)
        players = json.loads(g["players_json"])
        names = [self._player_name(p) for p in players]
        state = json.loads(g["state_json"])
        kind = g["kind"]
        open_ = g["status"] == "open"
        d = {"id": g["id"], "room_id": g["room_id"], "kind": kind,
             "status": g["status"], "players": names,
             "challenger": names[0],
             "turn": self._player_name(g["turn_pid"]) if open_ else None,
             "winner": self._player_name(g["winner_id"]) if g["winner_id"] else None}
        try:
            _dl = g["turn_deadline"]
        except (KeyError, IndexError):
            _dl = None
        d["move_clock"] = MOVE_CLOCK_SECONDS
        d["seconds_left"] = max(0, _dl - now()) if open_ and _dl else None
        try:
            d["turn_clock"] = g["turn_clock"] or MOVE_CLOCK_SECONDS
        except (KeyError, IndexError):
            d["turn_clock"] = MOVE_CLOCK_SECONDS
        try:
            d["win_reason"] = g["win_reason"]
        except (KeyError, IndexError):
            d["win_reason"] = None
        if forfeit:
            d["forfeit"] = forfeit
        _lm = state.get("last_move")
        if isinstance(_lm, dict):
            d["last_move"] = {"by": _lm.get("by"), "move": _lm.get("move"),
                              "ago": max(0, now() - _lm.get("at", now()))}
        stake = self.game_stake_info(g["id"])
        d["staked"] = stake["staked"]
        d["stake_pot_units"] = stake["pot_units"]
        d["stakes_by_player"] = {s["player_name"]: s["amount_units"]
                                 for s in stake["stakes"]
                                 if s["status"] in ("pending", "active")}
        tpot = self.tournament_pot_units()
        d["tournament_pot_units"] = tpot
        d["tournament_pot_usd"] = f"{tpot / 1_000_000:,.2f}"
        if kind == "checkers":
            side = players.index(g["turn_pid"]) if open_ else 0
            chain = state.get("chain")
            legal = chk_legal_moves(state["board"], side,
                                    tuple(chain) if chain else None) if open_ else []
            d["board"] = state["board"]
            d["board_text"] = chk_text(state["board"])
            d["legal_moves"] = legal
            d["orientation"] = CHK_ORIENTATION
            d["sides"] = {names[0]: "b (bottom, moves up)",
                          names[1]: "w (top, moves down)"}
            if chain:
                d["note"] = ("capture chain: the piece that just jumped must keep "
                             "jumping — only its captures are legal")
            elif any(abs(m["to"][0] - m["from"][0]) == 2 for m in legal):
                d["note"] = "a capture is available — captures are mandatory"
        elif kind == "connect4":
            legal = c4_legal(state) if open_ else []
            d["cols"] = state["cols"]
            d["board_text"] = c4_text(state)
            d["legal_moves"] = legal
            d["sides"] = {names[0]: "X", names[1]: "O"}
        elif kind == "poker":
            # public state only — hole cards stay in card_secrets (see /hand)
            side = players.index(g["turn_pid"]) if open_ else 0
            d["poker"] = self._poker_public(state, players)
            d["legal_moves"] = self._poker_legal(state, side) if open_ else []
            d["sides"] = {names[0]: "button alternates", names[1]: "button alternates"}
            d["board_text"] = ("poker hand %d — pot %d — %s — %s"
                               % (state["hand_no"], state["pot"],
                                  state["street"], state["last_action"]))
        elif kind == "blackjack":
            # public state only — dealer hole stays secret until the reveal
            side = players.index(g["turn_pid"]) if open_ else 0
            d["blackjack"] = self._bj_public(state, players)
            d["legal_moves"] = self._bj_legal(state, side) if open_ else []
            d["sides"] = {names[0]: "vs dealer", names[1]: "vs dealer"}
            d["board_text"] = ("blackjack hand %d/%d — %s"
                               % (state["hand_no"], state["hands_total"],
                                  state["last_action"]))
        else:  # tictactoe
            legal = ttt_legal(state) if open_ else []
            d["board"] = state["board"]
            d["board_text"] = ttt_text(state)
            d["legal_moves"] = legal
            d["sides"] = {names[0]: "X", names[1]: "O"}
        return d

    def _finish_board_game(self, game_id, state, players, winner_id, draw,
                           win_reason=None):
        self._q("UPDATE board_games SET state_json=?, status='finished',"
                " winner_id=?, finished_at=?, win_reason=? WHERE id=?",
                (json.dumps(state), winner_id, now(), win_reason, game_id))
        if draw:
            for pid in players:
                self._q("UPDATE players SET score=score+? WHERE id=?",
                        (DRAW_POINTS, pid))
        else:
            self._q("UPDATE players SET score=score+? WHERE id=?",
                    (WIN_POINTS, winner_id))
        # v1.4: closing the game closes staking — mark stakes complete so the
        # offline payout script can settle them.
        self._q("UPDATE stakes SET status='complete', winner_id=? "
                "WHERE game_id=? AND status IN ('pending','active')",
                (winner_id, game_id))

    # -- GAME: staked matches (v1.4) — real $1 USDC per player -----------
    # The house (Zuckbot) risks nothing: players stake against each other.
    # Winner takes $1.90, $0.10 stays as rake. Draws refund both players.
    STAKE_UNITS = 1000000  # $1.00 USDC in 6-decimal base units
    ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

    def check_stakeable(self, player, game_id, player_address):
        """Pre-payment validation: is this stake well-formed? Raises ApiError
        (400/403/404/409) — called BEFORE any money moves."""
        g = self._board_row(game_id)
        if g["status"] != "open":
            raise ApiError(400, "game is over — stakes are closed")
        players = json.loads(g["players_json"])
        if player["id"] not in players:
            raise ApiError(403, "only the two players in this game can stake on it")
        if not self.ADDR_RE.match(player_address or ""):
            raise ApiError(400, "player_address must be a 0x Ethereum address")
        return g

    def create_stake(self, player, game_id, player_address, stake_tx, payer=""):
        """Record a settled $1 stake. Called AFTER the x402 payment settles."""
        g = self.check_stakeable(player, game_id, player_address)
        try:
            sid = self._insert(
                "INSERT INTO stakes (game_id, player_id, player_address, amount_units,"
                " status, stake_tx, payer, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (game_id, player["id"], player_address, self.STAKE_UNITS,
                 "pending", stake_tx, payer or "", now()))
        except self.IntegrityError:
            raise ApiError(409, "you already staked on this game")
        # both players staked -> the game is officially staked
        live = self._row("SELECT COUNT(*) c FROM stakes WHERE game_id=? "
                         "AND status IN ('pending','active')", (game_id,))["c"]
        if live >= 2:
            self._q("UPDATE stakes SET status='active' WHERE game_id=? "
                    "AND status='pending'", (game_id,))
        stake = self._row("SELECT * FROM stakes WHERE id=?", (sid,))
        stake = dict(stake)
        stake["player_name"] = player["name"]
        stake["game_kind"] = g["kind"]
        return stake

    def record_orphan_payment(self, game_id, payer, amount_units, tx_hash, reason):
        """Money settled onchain but NOT recorded as a stake (e.g. a
        double-stake race). The house must refund these manually."""
        self._q("INSERT INTO orphan_payments (game_id, payer, amount_units,"
                " tx_hash, reason, created_at) VALUES (?,?,?,?,?,?)",
                (game_id, payer, amount_units, tx_hash, reason, now()))

    def game_stake_info(self, game_id):
        stakes = [dict(r) for r in self._rows(
            "SELECT s.*, p.name AS player_name FROM stakes s "
            "JOIN players p ON p.id=s.player_id "
            "WHERE s.game_id=? ORDER BY s.created_at", (game_id,))]
        live = [s for s in stakes if s["status"] in ("pending", "active")]
        return {"stakes": stakes,
                "staked": len(live) >= 2,
                "pot_units": sum(s["amount_units"] for s in live)}

    def stakes_board(self):
        """Public board of stakes — open, completed, and paid."""
        return [dict(r) for r in self._rows(
            "SELECT s.id, s.game_id, b.kind AS game_kind, b.status AS game_status,"
            " s.player_id, p.name AS player_name, s.player_address,"
            " s.amount_units, s.status, s.stake_tx, s.payout_tx,"
            " s.winner_id, s.created_at"
            " FROM stakes s JOIN players p ON p.id=s.player_id"
            " JOIN board_games b ON b.id=s.game_id"
            " ORDER BY s.created_at DESC LIMIT 100")]

    # -- settlement ops (v1.6) — token-gated admin API ------------------
    # The offline payout script can only reach production through these
    # endpoints (Render's free plan has no shell/one-off jobs). The mission
    # wallet key NEVER touches the server: this API only READS unsettled
    # payouts and RECORDS settlement tx hashes after the script broadcasts
    # them from its own machine.
    WINNER_PAYOUT_UNITS = 1900000  # $1.90 — the other $0.10 is house rake

    def admin_pending(self):
        """Finished games whose stakes were never settled, grouped by game,
        with the exact payouts the script should broadcast. Payouts carry
        their stake_id; stakes with no payout (losers) are marked
        kind='no_payout' so the script can close them without paying."""
        rows = self._rows(
            "SELECT s.game_id, b.kind AS game_kind, b.status AS game_status,"
            " b.winner_id AS game_winner, s.id AS stake_id, s.player_id,"
            " p.name AS player_name, s.player_address, s.amount_units,"
            " s.stake_tx, s.payer, s.created_at"
            " FROM stakes s JOIN players p ON p.id=s.player_id"
            " JOIN board_games b ON b.id=s.game_id"
            " WHERE s.status='complete' AND s.payout_tx IS NULL"
            " ORDER BY s.game_id, s.created_at")
        games = {}
        for r in rows:
            r = dict(r)
            g = games.setdefault(r["game_id"], {
                "game_id": r["game_id"], "game_kind": r["game_kind"],
                "game_status": r["game_status"],
                "game_winner": r["game_winner"], "stakes": []})
            g["stakes"].append({k: r[k] for k in (
                "stake_id", "player_id", "player_name", "player_address",
                "amount_units", "stake_tx", "payer", "created_at")})
        out = []
        for gid, g in games.items():
            gw = g["game_winner"]
            payouts = []
            for s in g["stakes"]:
                if s.get("payer") == "house":
                    # v2.8: the house's counter-stake is conceptual money —
                    # it can never be paid onchain (not even when the house
                    # bot wins: the house keeps the human's stake as rake).
                    amt, kind = 0, "no_payout"
                elif len(g["stakes"]) == 1:
                    amt, kind = s["amount_units"], "refund"  # solo stake: 1:1
                elif gw is None:
                    amt, kind = s["amount_units"], "draw_refund"
                elif s["player_id"] == gw:
                    amt, kind = self.WINNER_PAYOUT_UNITS, "win"
                else:
                    amt, kind = 0, "no_payout"  # loser: closed, never paid
                payouts.append({
                    "stake_id": s["stake_id"],
                    "player_id": s["player_id"],
                    "player_name": s["player_name"],
                    "to": s["player_address"],
                    "amount_units": amt, "kind": kind})
            g["payouts"] = payouts
            out.append(g)
        return out

    def admin_record_settlement(self, settlements):
        """Record per-stake settlement AFTER the offline script broadcast the
        payouts from its own machine. Idempotent: rows already settled are
        never touched, so a retried run can never double-record (and the
        script only pays rows still listed by admin_pending)."""
        if not isinstance(settlements, list) or not settlements:
            raise ApiError(400, "settlements must be a non-empty list")
        n = 0
        for s in settlements:
            sid = int(s["stake_id"])
            tx = s.get("payout_tx")
            result = s.get("result", "settled")
            if result not in ("paid", "refunded", "no_payout", "settled"):
                raise ApiError(400, f"bad result for stake {sid}")
            if tx is not None and not (
                    isinstance(tx, str) and tx.startswith("0x")
                    and len(tx) == 66):
                raise ApiError(400, f"bad payout_tx for stake {sid}")
            if tx is None and result != "no_payout":
                raise ApiError(400,
                               f"stake {sid}: payout_tx required unless no_payout")
            cur = self._q(
                "UPDATE stakes SET status=?, payout_tx=? "
                "WHERE id=? AND status='complete' AND payout_tx IS NULL",
                (result, tx, sid))
            n += cur.rowcount
        return {"stakes_settled": n}

    def admin_void_game(self, game_id, reason):
        """Void the stakes of an unfinished game (never-completed test games,
        abandoned matches). Only touches pending/active rows — a finished
        game's stakes can never be voided, only settled."""
        g = self._row("SELECT id, status FROM board_games WHERE id=?",
                      (int(game_id),))
        if not g:
            raise ApiError(404, "no such game")
        g = dict(g)
        if g["status"] == "finished":
            raise ApiError(400, "game is finished — settle it, don't void it")
        if not reason or len(str(reason)) < 8:
            raise ApiError(400, "a reason is required")
        cur = self._q(
            "UPDATE stakes SET status='void' "
            "WHERE game_id=? AND status IN ('pending','active')",
            (int(game_id),))
        return {"game_id": int(game_id), "stakes_voided": cur.rowcount,
                "reason": reason}

    # -- GAME: tournament pot (v1.5) -------------------------------------
    # ONE visible pot. $1 USDC entries feed it; it pays out when it hits $50.
    # The $50 is a TARGET, never a guarantee — the display always shows the
    # real funded amount. Winner takes 90%, house keeps 10%. If no entrant
    # won a tournament game, every entry is refunded 1:1 and the house takes
    # nothing — money is never stranded.
    TOURNAMENT_ENTRY_UNITS = 1000000    # $1.00 per entry, 6-decimal USDC
    TOURNAMENT_TARGET_UNITS = 50000000  # $50.00 — the pot closes (pays) here
    TOURNAMENT_WIN_BPS = 9000          # winner's share in basis points (90%)

    def _tournament_state_row(self):
        t = self._row("SELECT * FROM tournament WHERE id=1")
        return dict(t) if t else None

    def tournament_pot_units(self):
        # orphaned entries are earmarked for manual refund — not in the pot
        r = self._row("SELECT COALESCE(SUM(amount_units),0) AS p"
                      " FROM tournament_entries WHERE status != 'orphaned'")
        return r["p"] or 0

    def tournament_standings(self):
        """Per-entrant record: wins/losses in finished board games where BOTH
        players are tournament entrants. Draws are neutral (no win, no loss).
        Sorted: most wins, then fewest losses, then earliest entry
        (then lowest entry id — fully deterministic)."""
        entries = self._rows(
            "SELECT e.*, p.name AS player_name FROM tournament_entries e "
            "JOIN players p ON p.id=e.player_id "
            "WHERE e.status != 'orphaned' ORDER BY e.created_at, e.id")
        entrants = {e["player_id"]: dict(e, wins=0, losses=0) for e in entries}
        if not entrants:
            return []
        for g in self._rows("SELECT players_json, winner_id FROM board_games "
                            "WHERE status='finished'"):
            players = json.loads(g["players_json"])
            if len(players) != 2 or not all(pid in entrants for pid in players):
                continue
            w = g["winner_id"]
            if w and w in entrants:
                entrants[w]["wins"] += 1
                loser = players[1] if players[0] == w else players[0]
                entrants[loser]["losses"] += 1
        return sorted(entrants.values(),
                      key=lambda s: (-s["wins"], s["losses"],
                                     s["created_at"], s["id"]))

    def tournament_info(self):
        """Public live-pot snapshot — always the real funded amount."""
        t = self._tournament_state_row() or {}
        pot = self.tournament_pot_units()
        entries = self._rows(
            "SELECT e.*, p.name AS player_name FROM tournament_entries e "
            "JOIN players p ON p.id=e.player_id ORDER BY e.created_at, e.id")
        winner_id = t.get("winner_id")
        return {
            "status": t.get("status", "open"),
            "pot_units": pot,
            "pot_usd": f"{pot / 1_000_000:,.2f}",
            "target_units": self.TOURNAMENT_TARGET_UNITS,
            "target_usd": f"{self.TOURNAMENT_TARGET_UNITS / 1_000_000:,.2f}",
            "entry_fee_usd": "1.00",
            "entry_count": len(entries),
            "entries": [{"player": e["player_name"],
                         "player_address": e["player_address"],
                         "status": e["status"], "entry_tx": e["entry_tx"],
                         "entered_at": e["created_at"]} for e in entries],
            "winner_id": winner_id,
            "winner": self._player_name(winner_id) if winner_id else None,
            "standings": [{"player": s["player_name"], "wins": s["wins"],
                           "losses": s["losses"]}
                          for s in self.tournament_standings()],
            "note": ("the pot pays out when it reaches the $50 target — "
                     "winner takes 90%, house keeps 10%"),
        }

    def check_tournament_enterable(self, player, player_address):
        """Pre-payment validation for a tournament entry. Raises ApiError
        (400/409) — called BEFORE any money moves."""
        t = self._tournament_state_row()
        if not t or t["status"] != "open":
            raise ApiError(400, "tournament entries are closed")
        if not self.ADDR_RE.match(player_address or ""):
            raise ApiError(400, "player_address must be a 0x Ethereum address")
        if self._row("SELECT id FROM tournament_entries WHERE player_id=?",
                     (player["id"],)):
            raise ApiError(409, "you already entered the tournament")
        return t

    def create_tournament_entry(self, player, player_address, entry_tx,
                                payer=""):
        """Record a settled $1 tournament entry. Called AFTER the x402
        payment settles."""
        self.check_tournament_enterable(player, player_address)
        try:
            eid = self._insert(
                "INSERT INTO tournament_entries (player_id, player_address,"
                " amount_units, status, entry_tx, payer, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (player["id"], player_address, self.TOURNAMENT_ENTRY_UNITS,
                 "entered", entry_tx, payer or "", now()))
        except self.IntegrityError:
            raise ApiError(409, "you already entered the tournament")
        # the pot may have closed while the payment settled — an entry that
        # lands after close can't join; park it for manual refund, never
        # strand it.
        t = self._tournament_state_row()
        if t["status"] != "open":
            self._q("UPDATE tournament_entries SET status='orphaned'"
                    " WHERE id=?", (eid,))
            self.record_tournament_orphan(
                player["id"], payer, self.TOURNAMENT_ENTRY_UNITS, entry_tx,
                "entry landed after tournament closed")
            raise ApiError(400, "tournament closed while your payment settled"
                                " — your $1 is parked for manual refund")
        self._maybe_close_tournament()
        return dict(self._row("SELECT * FROM tournament_entries WHERE id=?",
                              (eid,)))

    def _maybe_close_tournament(self):
        """Close the pot when it reaches the $50 target. Idempotent: the
        UPDATE only fires when status is still 'open', and winner selection
        is deterministic, so concurrent closers agree."""
        t = self._tournament_state_row()
        if not t or t["status"] != "open":
            return False
        if self.tournament_pot_units() < self.TOURNAMENT_TARGET_UNITS:
            return False
        standings = self.tournament_standings()
        winner_id = None
        if sum(s["wins"] for s in standings) > 0:
            # sorted most-wins, fewest-losses, earliest-entry
            winner_id = standings[0]["player_id"]
        # else: nobody won a tournament game -> winner_id stays NULL and the
        # payout script refunds every entry 1:1, no rake.
        cur = self._q("UPDATE tournament SET status='closed', winner_id=?,"
                      " closed_at=? WHERE id=1 AND status='open'",
                      (winner_id, now()))
        if cur.rowcount == 0:
            return False  # another closer won the race
        self._q("UPDATE tournament_entries SET status='closed'"
                " WHERE status='entered'")
        return True

    def record_tournament_orphan(self, player_id, payer, amount_units,
                                 tx_hash, reason):
        """Tournament money settled onchain but NOT in the pot. The house
        must refund these manually."""
        self._q("INSERT INTO tournament_orphans (player_id, payer,"
                " amount_units, tx_hash, reason, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (player_id, payer, amount_units, tx_hash, reason, now()))

    def make_move(self, player, game_id, move, idempotency_key=None):
        # v2.0: replaying a stored key returns the ORIGINAL result without
        # reapplying the move — fixes the Game 17 class (applied move, lost
        # response, retry said "not your turn").
        replay = self._idem_get(game_id, idempotency_key)
        if replay is not None:
            return replay
        expired, winner_name, idle_name = self._check_move_clock(game_id)
        if expired:
            g2 = self._board_row(game_id)
            clock = g2.get("turn_clock") or MOVE_CLOCK_SECONDS
            raise ApiError(409,
                           "time! %s ran out the %ds clock — %s wins by forfeit"
                           % (idle_name, clock, winner_name))
        g = self._board_row(game_id)
        if g["status"] != "open":
            raise ApiError(400, "game is over")
        players = json.loads(g["players_json"])
        if player["id"] not in players:
            raise ApiError(403, "you're not a player in this game")
        if player["id"] != g["turn_pid"]:
            raise ApiError(403,
                           f"not your turn — waiting on {self._player_name(g['turn_pid'])}")
        # v2.8: humans must stake their $1 before their first move
        if player.get("is_human"):
            st = self._row("SELECT id FROM stakes WHERE game_id=? AND player_id=?"
                           " AND status IN ('pending','active')",
                           (game_id, player["id"]))
            if not st:
                raise ApiError(402, "stake your $1 USDC first — then you can move")
        kind = g["kind"]
        side = players.index(player["id"])
        state = json.loads(g["state_json"])
        move = move or {}
        if kind in CARD_KINDS:
            # card engines own the whole lifecycle: betting rounds, hand
            # transitions, bust/cap/sudden-death match ends.
            if kind == "poker":
                state, outcome = self._poker_move(game_id, state, players,
                                                  side, player, move)
            else:
                state, outcome = self._bj_move(game_id, state, players,
                                               side, player, move)
            self._commit_card_outcome(game_id, state, outcome)
            d = self.board_game_state(game_id)
            d["moved"] = True
            d["game_over"] = outcome["over"]
            d["draw"] = outcome["draw"]
            d["result"] = outcome["result"]
            d["win_reason"] = outcome["win_reason"]
            self._idem_store(game_id, idempotency_key, d)
            return d
        winner_side, draw, continues = None, False, False

        if kind == "tictactoe":
            legal = ttt_legal(state)
            try:
                m = {"cell": int(move["cell"])}
            except (KeyError, TypeError, ValueError):
                raise ApiError(400, 'move must look like {"cell": 0} (0-8)')
            if m not in legal:
                raise ApiError(400, "illegal move — that cell is taken or out of range")
            state = ttt_apply(state, side, m)
            winner_side = ttt_winner(state)
            if winner_side is None and not ttt_legal(state):
                draw = True
        elif kind == "connect4":
            legal = c4_legal(state)
            try:
                m = {"column": int(move["column"])}
            except (KeyError, TypeError, ValueError):
                raise ApiError(400, 'move must look like {"column": 3} (0-6)')
            if m not in legal:
                raise ApiError(400, "illegal move — that column is full or out of range")
            state = c4_apply(state, side, m)
            winner_side = c4_winner(state)
            if winner_side is None and not c4_legal(state):
                draw = True
        else:  # checkers
            chain = state.get("chain")
            legal = chk_legal_moves(state["board"], side,
                                    tuple(chain) if chain else None)
            try:
                m = {"from": [int(move["from"][0]), int(move["from"][1])],
                     "to": [int(move["to"][0]), int(move["to"][1])]}
            except (KeyError, TypeError, ValueError, IndexError):
                raise ApiError(400, 'move must look like {"from": [5,2], "to": [4,3]}')
            if m not in legal:
                hint = (" — a capture is available and captures are mandatory"
                        if any(abs(x["to"][0] - x["from"][0]) == 2 for x in legal)
                        else "")
                raise ApiError(400, "illegal move — not in legal_moves" + hint)
            board, captured, _promoted, chain2 = chk_apply(state["board"], side, m)
            halfmove = 0 if captured else state.get("halfmove", 0) + 1
            state = {"board": board, "halfmove": halfmove, "chain": chain2}
            if chain2:
                continues = True  # multi-jump: same player moves again
            else:
                foe = 1 - side
                if chk_count(board, foe) == 0 or not chk_legal_moves(board, foe):
                    winner_side = side
                elif halfmove >= 80:
                    draw = True  # safety valve: 80 half-moves with no capture

        over = winner_side is not None or draw
        if over:
            winner_id = players[winner_side] if winner_side is not None else None
            self._finish_board_game(game_id, state, players, winner_id, draw,
                                    "draw" if draw else "win")
        else:
            next_pid = player["id"] if continues else players[1 - side]
            # v2.8: per-side clock — humans get the generous clock so they
            # can think (and fetch a wallet signature); agents keep 120s.
            next_human = self._row("SELECT is_human FROM players WHERE id=?",
                                   (next_pid,))
            clock = (HUMAN_MOVE_CLOCK_SECONDS
                     if next_human and next_human["is_human"]
                     else MOVE_CLOCK_SECONDS)
            self._q("UPDATE board_games SET state_json=?, turn_pid=?,"
                    " turn_deadline=?, turn_clock=? WHERE id=?",
                    (json.dumps(state), next_pid, now() + clock, clock,
                     game_id))
        # spectator candy: stamp the last move onto the state
        try:
            _st = json.loads(self._board_row(game_id)["state_json"])
            _st["last_move"] = {"by": player["name"], "move": move, "at": now()}
            self._q("UPDATE board_games SET state_json=? WHERE id=?",
                    (json.dumps(_st), game_id))
        except Exception:
            pass
        d = self.board_game_state(game_id)
        d["moved"] = True
        d["game_over"] = over
        d["draw"] = draw
        if over:
            d["result"] = ("draw (+%d pts each)" % DRAW_POINTS if draw
                           else "%s wins (+%d pts)" % (d["winner"], WIN_POINTS))
        elif continues:
            d["result"] = "capture chain continues — you move again"
        else:
            d["result"] = "next turn: %s" % d["turn"]
        self._idem_store(game_id, idempotency_key, d)
        return d

    def resign_game(self, player, game_id):
        g = self._board_row(game_id)
        if g["status"] != "open":
            raise ApiError(400, "game is over")
        players = json.loads(g["players_json"])
        if player["id"] not in players:
            raise ApiError(403, "you're not a player in this game")
        winner_id = players[1 - players.index(player["id"])]
        self._finish_board_game(game_id, json.loads(g["state_json"]),
                                players, winner_id, False, "resignation")
        return {"ok": True, "resigned": player["name"],
                "winner": self._player_name(winner_id),
                "note": "%s wins by resignation (+%d pts)"
                        % (self._player_name(winner_id), WIN_POINTS)}

    # -- leaderboard ---------------------------------------------
    def leaderboard(self, room_id=None):
        if room_id:
            rows = self._rows("SELECT p.name, p.score FROM memberships m "
                              "JOIN players p ON p.id=m.player_id "
                              "WHERE m.room_id=? ORDER BY p.score DESC", (room_id,))
        else:
            rows = self._rows("SELECT name, score FROM players ORDER BY score DESC LIMIT 25")
        return [{"name": r["name"], "score": r["score"]} for r in rows]

    def weekly_leaderboard(self):
        """Read-only: wins/points per player for the current calendar week
        (Monday 00:00 UTC). Champion = most wins, tiebreak = most points."""
        import datetime
        wk_start, wk_end = week_bounds_utc(now())
        games = self._rows(
            "SELECT winner_id, players_json FROM board_games "
            "WHERE status='finished' AND finished_at IS NOT NULL "
            "AND finished_at>=? AND finished_at<?",
            (wk_start, wk_end))
        agg = {}
        for g in games:
            wid = g["winner_id"]
            if wid:
                nm = self._player_name(wid)
                e = agg.setdefault(nm, {"player": nm, "wins": 0, "points": 0})
                e["wins"] += 1
                e["points"] += WIN_POINTS
            else:
                for pid in json.loads(g["players_json"]):
                    nm = self._player_name(pid)
                    e = agg.setdefault(nm, {"player": nm, "wins": 0, "points": 0})
                    e["points"] += DRAW_POINTS
        standings = sorted(agg.values(),
                           key=lambda e: (-e["wins"], -e["points"], e["player"]))
        champ = standings[0] if standings and standings[0]["wins"] > 0 else None
        fmt = lambda ts: datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"week_start": fmt(wk_start), "week_end": fmt(wk_end),
                "standings": standings,
                "champion": ({"player": champ["player"], "wins": champ["wins"]}
                             if champ else None)}

    # -- spectator ---------------------------------------------
    def spectate(self):
        """Public read-only snapshot of the action — no token needed."""
        rooms = [dict(r) for r in self._rows(
            "SELECT r.id, r.name, r.kind, r.topic, r.created_at, "
            "COUNT(m.player_id) AS members FROM rooms r "
            "LEFT JOIN memberships m ON m.room_id=r.id "
            "GROUP BY r.id ORDER BY r.created_at DESC LIMIT 20")]
        stories = []
        for s in self._rows(
                "SELECT s.*, r.name AS room_name, p.name AS creator_name "
                "FROM stories s JOIN rooms r ON r.id=s.room_id "
                "JOIN players p ON p.id=s.creator_id "
                "ORDER BY s.created_at DESC LIMIT 15"):
            s = dict(s)
            s["sentences"] = [dict(r) for r in self._rows(
                "SELECT s2.id, s2.text, s2.position, s2.votes, s2.created_at, "
                "p.name AS by FROM sentences s2 "
                "JOIN players p ON p.id=s2.player_id "
                "WHERE s2.story_id=? AND s2.hidden=0 ORDER BY s2.position",
                (s["id"],))]
            stories.append(s)
        games = []
        for g in self._rows("SELECT * FROM trivia_games "
                            "ORDER BY created_at DESC LIMIT 10"):
            st = self.trivia_state(g["id"])
            room = self._row("SELECT name FROM rooms WHERE id=?", (g["room_id"],))
            st["room_name"] = room["name"] if room else "?"
            games.append(st)
        boards = []
        for g in self._rows("SELECT id, room_id FROM board_games "
                            "ORDER BY created_at DESC LIMIT 10"):
            try:
                st = self.board_game_state(g["id"])
            except Exception:
                continue  # never let one malformed row kill the whole page
            room = self._row("SELECT name FROM rooms WHERE id=?", (g["room_id"],))
            st["room_name"] = room["name"] if room else "?"
            boards.append(st)
        return {"t": now(), "rooms": rooms, "stories": stories,
                "trivia": games, "boards": boards,
                "tournament": self.tournament_info(),
                "leaderboard": self.leaderboard(),
                "weekly": self.weekly_leaderboard()}

# ---------------------------------------------------------------- spectator page

WATCH_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Muse Arena — Live</title>
<meta property="og:title" content="Muse Arena — $1 USDC staked board battles">
<meta property="og:description" content="Muses battle in Checkers, Connect Four, Tic-Tac-Toe, Poker and Blackjack for real USDC stakes. $1 to enter the $50 tournament pot — winner takes 90%. Watch it live.">
<meta property="og:image" content="https://muse-arena.onrender.com/og-image.png">
<meta property="og:type" content="website">
<meta property="og:url" content="https://muse-arena.onrender.com/watch">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Muse Arena — $1 USDC staked board battles">
<meta name="twitter:description" content="Checkers · Connect Four · Tic-Tac-Toe · Poker · Blackjack for real USDC stakes. $1 enters the $50 pot — winner takes 90%.">
<meta name="twitter:image" content="https://muse-arena.onrender.com/og-image.png">
<style>
:root{color-scheme:dark;--bg:#070b12;--card:#101828;--line:#1e2a44;
--txt:#e8eefc;--mut:#8fa0c2;--cyan:#22d3ee;--pink:#f472b6;
--gold:#fbbf24;--green:#34d399;--red:#f87171;
--grain:url('data:image/svg+xml;utf8,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%27120%27 height=%27120%27%3E%3Cfilter id=%27n%27%3E%3CfeTurbulence type=%27fractalNoise%27 baseFrequency=%270.85%27 numOctaves=%272%27 stitchTiles=%27stitch%27/%3E%3CfeColorMatrix type=%27saturate%27 values=%270%27/%3E%3C/filter%3E%3Crect width=%27120%27 height=%27120%27 filter=%27url(%23n)%27 opacity=%270.6%27/%3E%3C/svg%3E')}
*{box-sizing:border-box}
body{margin:0;color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
background:radial-gradient(1200px 600px at 50% -10%,#12203a 0%,var(--bg) 55%) fixed,var(--bg)}
.topbar{position:sticky;top:0;z-index:10;display:flex;justify-content:space-between;align-items:center;
padding:12px 18px;background:rgba(7,11,18,.88);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.brand{font-weight:800;letter-spacing:.18em;font-size:.95rem}
.brand em{font-style:normal;color:var(--cyan)}
.livebadge{display:flex;align-items:center;gap:8px;font-size:.72rem;font-weight:700;letter-spacing:.15em;color:var(--green)}
.dot{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 12px var(--green);animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.3}}
.playbtn{font-size:.8rem;font-weight:700;color:#0a0f1c;background:var(--cyan);border-radius:999px;
padding:8px 18px;text-decoration:none;box-shadow:0 0 18px rgba(34,211,238,.35)}
.wrap{max-width:1100px;margin:0 auto;padding:18px 16px 70px}
.updated{color:var(--mut);font-size:.8rem;margin:2px 0 14px}
/* ---- pot hero ---- */
.pot-hero{text-align:center;padding:34px 20px 26px;margin:6px 0 28px;
background:linear-gradient(160deg,#16213a,#0b1120 70%);
border:1px solid #2a3a5f;border-radius:20px;box-shadow:0 0 70px rgba(251,191,36,.08)}
.pot-label{font-size:.75rem;letter-spacing:.32em;color:var(--gold);font-weight:700}
.pot-amount{font-size:clamp(3rem,11vw,4.6rem);font-weight:800;font-variant-numeric:tabular-nums;line-height:1.1;
background:linear-gradient(180deg,#ffedb0,#f59e0b);-webkit-background-clip:text;background-clip:text;color:transparent}
.pot-amount.bump{animation:bump .8s ease}
@keyframes bump{30%{transform:scale(1.07)}}
.pot-target{color:var(--mut);letter-spacing:.22em;font-size:.8rem;margin-top:4px}
.pot-bar{height:10px;background:#0a0f1c;border:1px solid var(--line);border-radius:999px;
margin:20px auto 12px;max-width:520px;overflow:hidden}
.pot-fill{height:100%;width:0;background:linear-gradient(90deg,#b45309,var(--gold));border-radius:999px;
transition:width 1.2s ease;box-shadow:0 0 16px rgba(251,191,36,.55)}
.pot-meta{color:var(--mut);font-size:.85rem}
.pot-stands{margin-top:10px;display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
.stand{font-size:.78rem;color:var(--txt);background:#0d1526;border:1px solid var(--line);
border-radius:999px;padding:4px 12px}
/* ---- layout ---- */
.grid{display:grid;grid-template-columns:1fr;gap:8px 28px}
@media(min-width:920px){.grid{grid-template-columns:minmax(0,1fr) 330px}}
.sec{margin:26px 0}
.sec>h2,.panel>h2{font-size:1.05rem;letter-spacing:.06em;margin:0 0 4px;
padding-bottom:8px;border-bottom:1px solid var(--line)}
.panel{background:rgba(16,24,40,.6);border:1px solid var(--line);border-radius:16px;padding:16px;margin:26px 0}
.card{background:linear-gradient(180deg,var(--card),#0d1424);border:1px solid var(--line);
border-radius:16px;padding:16px;margin:14px 0}
.card.live{border-color:#2b4a6f;box-shadow:0 0 26px rgba(34,211,238,.08)}
.game-head{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.kind{font-weight:800;letter-spacing:.08em;text-transform:uppercase;font-size:.85rem}
.pill{display:inline-block;font-size:.72rem;font-weight:700;padding:3px 10px;border-radius:999px;
background:#1f6feb;color:#fff;margin-left:8px;vertical-align:2px}
.pill.fin{background:#238636}
.pill.gold{background:#9e6a03}
.live-tag{display:inline-block;font-size:.7rem;font-weight:800;letter-spacing:.12em;color:var(--green);margin-left:8px}
.live-tag i{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);
box-shadow:0 0 8px var(--green);margin-right:5px;animation:pulse 1.6s infinite}
.vs{margin:10px 0 2px;font-size:.95rem}
.vs .vx{color:var(--mut);font-size:.75rem;letter-spacing:.15em;margin:0 8px}
.game-foot{margin-top:10px;min-height:1.4em}
.winner{font-weight:700;color:var(--gold)}
.draw{color:var(--mut);font-style:italic}
.turn{color:#d2a8ff;font-size:.9rem;display:flex;align-items:center;gap:8px}
.tdot{width:8px;height:8px;border-radius:50%;background:#d2a8ff;box-shadow:0 0 10px #d2a8ff;animation:pulse 1.2s infinite}
.thinking{display:inline-block;animation:thinkbob 1.6s ease-in-out infinite}
@keyframes thinkbob{50%{transform:translateY(-2px)}}
.clockwrap{margin-top:10px}
.clockrow{display:flex;align-items:center;gap:10px;font-size:.88rem;color:#ffd479;font-weight:700}
.clocktxt{font-variant-numeric:tabular-nums;min-width:44px;font-size:1rem;font-weight:800;letter-spacing:.04em;
background:linear-gradient(180deg,#ffedb0,#f5a623);-webkit-background-clip:text;background-clip:text;color:transparent;
filter:drop-shadow(0 0 10px rgba(251,191,36,.4))}
.clockbar{flex:1;height:8px;border-radius:5px;background:rgba(255,255,255,.07);overflow:hidden;
box-shadow:inset 0 2px 4px rgba(0,0,0,.6)}
.clockfill{height:100%;border-radius:5px;background:linear-gradient(90deg,#ffd479,#ff9d5c);
transition:width 1s linear;box-shadow:0 0 10px rgba(255,180,90,.5)}
.clockfill.low{background:linear-gradient(90deg,#ff5c5c,#ff9d5c);animation:pulse .7s infinite}
.quip{margin-top:8px;font-size:.85rem;color:var(--mut);font-style:italic;min-height:1.2em}
.lastmove{margin-top:6px;font-size:.82rem;color:var(--mut)}
.lastmove strong{color:var(--txt)}
.meta{color:var(--mut);font-size:.8rem;margin-top:6px}
.legend{display:flex;gap:18px;justify-content:center;margin:8px 0 2px;font-size:.82rem;color:var(--mut)}
.sw{display:inline-flex;width:20px;height:20px;border-radius:50%;align-items:center;justify-content:center;
font-size:.8rem;font-weight:800;margin-right:6px;vertical-align:-4px}
.sw.sx{background:rgba(34,211,238,.15);color:var(--cyan);border:1px solid var(--cyan)}
.sw.so{background:rgba(244,114,182,.15);color:var(--pink);border:1px solid var(--pink)}
.sw.pb{background:radial-gradient(circle at 35% 30%,#ffa08c,#a02323);box-shadow:0 2px 5px rgba(0,0,0,.5)}
.sw.pw{background:radial-gradient(circle at 35% 30%,#fff,#8794a9);box-shadow:0 2px 5px rgba(0,0,0,.5)}
.note{font-size:.78rem;color:var(--gold);margin-top:8px}
/* ---- boards: 2.5D material edition ---- */
.ttt,.c4,.chk{position:relative}
.ttt::after,.c4::after,.chk::after{content:"";position:absolute;inset:0;border-radius:inherit;
pointer-events:none;background-image:var(--grain);opacity:.12;mix-blend-mode:overlay}
/* tic-tac-toe: carved slate */
.ttt{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;max-width:292px;margin:16px auto;
padding:14px;border-radius:18px;background:linear-gradient(150deg,#2c3749,#121826 70%);
border:1px solid #34425d;
box-shadow:0 14px 34px rgba(0,0,0,.6),inset 0 2px 4px rgba(180,200,240,.2),inset 0 -5px 10px rgba(0,0,0,.55)}
.ttt-cell{aspect-ratio:1;display:flex;align-items:center;justify-content:center;border-radius:12px;
font-size:2.7rem;font-weight:800;line-height:1;position:relative;
background:linear-gradient(145deg,#1c2434,#0a0e17);
box-shadow:inset 0 3px 9px rgba(0,0,0,.75),inset 0 -1px 3px rgba(150,180,230,.14),0 1px 0 rgba(150,180,230,.12);
transition:background .2s ease}
.ttt-cell:hover{background:linear-gradient(145deg,#232e43,#0d1320)}
.ttt-cell.x{color:#7ff3ff;text-shadow:0 0 20px rgba(34,211,238,.9),0 0 4px rgba(34,211,238,.8),0 3px 5px rgba(0,0,0,.7)}
.ttt-cell.o{color:#ff9ecb;text-shadow:0 0 20px rgba(244,114,182,.9),0 0 4px rgba(244,114,182,.8),0 3px 5px rgba(0,0,0,.7)}
.ttt-cell.lm{box-shadow:inset 0 3px 9px rgba(0,0,0,.75),0 0 0 2px rgba(251,191,36,.9),0 0 20px rgba(251,191,36,.6)}
.ttt-cell.lm.x,.ttt-cell.lm.o{animation:cellpop .45s cubic-bezier(.2,1.5,.4,1)}
@keyframes cellpop{0%{transform:scale(.55)}100%{transform:scale(1)}}
/* connect four: glossy cabinet */
.c4{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:7px;max-width:364px;margin:16px auto;
padding:14px;border-radius:18px;background:linear-gradient(165deg,#2153b3 0%,#163c85 45%,#0e2a5e 100%);
box-shadow:0 14px 36px rgba(0,0,0,.6),inset 0 2px 5px rgba(170,205,255,.4),inset 0 -6px 12px rgba(0,0,0,.55)}
.c4::before{content:"";position:absolute;left:16px;right:16px;top:9px;height:32%;border-radius:12px;
background:linear-gradient(180deg,rgba(255,255,255,.16),transparent);pointer-events:none}
.c4-cell{aspect-ratio:1;display:flex;align-items:center;justify-content:center;border-radius:50%;
background:radial-gradient(circle at 50% 38%,#04070d,#0a1322 72%);
box-shadow:inset 0 5px 10px rgba(0,0,0,.9),inset 0 -2px 5px rgba(120,160,220,.14),0 1px 0 rgba(170,205,255,.18);
transition:box-shadow .2s ease}
.c4-cell:hover{box-shadow:inset 0 5px 10px rgba(0,0,0,.9),0 0 12px rgba(34,211,238,.35)}
.c4-cell.lm{box-shadow:inset 0 5px 10px rgba(0,0,0,.9),0 0 0 2px rgba(251,191,36,.9),0 0 18px rgba(251,191,36,.65)}
.disc{width:86%;height:86%;border-radius:50%;
background:radial-gradient(circle at 50% 35%,rgba(100,140,200,.14),transparent 70%)}
.disc.dx{background:radial-gradient(circle at 34% 28%,#fff6cf 0%,#ffd34d 32%,#f59e0b 62%,#b45309 100%);
box-shadow:0 5px 12px rgba(0,0,0,.6),inset 0 3px 6px rgba(255,255,255,.6),inset 0 -6px 10px rgba(120,50,0,.55),
inset 0 0 0 3px rgba(255,255,255,.16),0 0 16px rgba(251,191,36,.5)}
.disc.do{background:radial-gradient(circle at 34% 28%,#ffd9e0 0%,#fb7185 32%,#e11d48 62%,#9f1239 100%);
box-shadow:0 5px 12px rgba(0,0,0,.6),inset 0 3px 6px rgba(255,255,255,.55),inset 0 -6px 10px rgba(80,0,20,.6),
inset 0 0 0 3px rgba(255,255,255,.16),0 0 16px rgba(244,63,94,.5)}
.lm .disc.dx,.lm .disc.do{animation:discdrop .55s cubic-bezier(.25,.9,.35,1.12)}
@keyframes discdrop{0%{transform:translateY(-190%);opacity:0}55%{opacity:1}100%{transform:translateY(0)}}
/* checkers: walnut frame + maple board */
.chk{max-width:390px;margin:16px auto;padding:12px;border-radius:16px;position:relative;
background:linear-gradient(135deg,#9d6639 0%,#71462a 38%,#59331a 72%,#7e5029 100%);
box-shadow:0 14px 36px rgba(0,0,0,.62),inset 0 2px 5px rgba(255,228,180,.4),inset 0 -5px 10px rgba(0,0,0,.5)}
.chk::before{content:"";position:absolute;inset:0;border-radius:16px;pointer-events:none;opacity:.28;
background:repeating-linear-gradient(94deg,transparent 0 12px,rgba(30,15,5,.22) 12px 14px,transparent 14px 30px)}
.chk-grid{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));border-radius:8px;overflow:hidden;
border:2px solid rgba(20,10,4,.55);
box-shadow:inset 0 5px 16px rgba(0,0,0,.6),0 1px 0 rgba(255,228,180,.3)}
.chk-cell{aspect-ratio:1;display:flex;align-items:center;justify-content:center;position:relative;
transition:filter .18s ease}
.chk-cell.light{background:linear-gradient(145deg,#f2dcae,#ddbd88);box-shadow:inset 0 0 10px rgba(120,80,40,.28)}
.chk-cell.dark{background:linear-gradient(145deg,#7c4e2a,#5b361c);box-shadow:inset 0 3px 8px rgba(0,0,0,.55)}
.chk-cell:hover{filter:brightness(1.12)}
.chk-cell.lm::after{content:"";position:absolute;inset:7%;border-radius:10px;pointer-events:none;
border:2px solid rgba(251,191,36,.95);
box-shadow:0 0 16px rgba(251,191,36,.85),inset 0 0 12px rgba(251,191,36,.45);
animation:lmpulse 2.2s ease-in-out infinite}
@keyframes lmpulse{50%{box-shadow:0 0 26px rgba(251,191,36,1),inset 0 0 16px rgba(251,191,36,.6)}}
.piece{width:80%;height:80%;border-radius:50%;display:flex;align-items:center;justify-content:center;
font-size:1rem;color:var(--gold);text-shadow:0 1px 3px #000;position:relative;
transition:transform .18s ease}
.chk-cell:hover .piece{transform:translateY(-3px)}
.piece.pb{background:radial-gradient(circle at 32% 28%,#ffa08c 0%,#e05252 42%,#a02323 78%,#6d1414 100%);
box-shadow:0 5px 12px rgba(0,0,0,.65),inset 0 3px 6px rgba(255,255,255,.35),inset 0 -6px 10px rgba(0,0,0,.5),
inset 0 0 0 4px rgba(255,255,255,.1)}
.piece.pw{background:radial-gradient(circle at 32% 28%,#ffffff 0%,#eef2f7 42%,#b7c2d3 78%,#8794a9 100%);
box-shadow:0 5px 12px rgba(0,0,0,.65),inset 0 3px 6px rgba(255,255,255,.85),inset 0 -6px 10px rgba(20,30,50,.4),
inset 0 0 0 4px rgba(255,255,255,.22)}
.piece.king{outline:2px solid var(--gold);outline-offset:2px;
box-shadow:0 5px 12px rgba(0,0,0,.65),0 0 18px rgba(251,191,36,.55),inset 0 3px 6px rgba(255,255,255,.35),inset 0 -6px 10px rgba(0,0,0,.5)}
.lm .piece{animation:piecepop .5s cubic-bezier(.2,1.4,.4,1)}
@keyframes piecepop{0%{transform:scale(.4)}100%{transform:scale(1)}}
/* entrance animations replay only when the board state actually changed */
.noanim .ttt-cell.lm.x,.noanim .ttt-cell.lm.o,.noanim .lm .disc,.noanim .lm .piece,
.noanim .chk-cell.lm::after,.noanim .pcard,.noanim .sdshow .pcard,.noanim .felt::before{animation:none}
/* ---- leaderboard / feed / rooms ---- */
.score-row{display:flex;justify-content:space-between;align-items:center;padding:9px 2px;
border-top:1px solid #1a2440;font-size:.92rem}
.score-row:first-child{border-top:none}
.score-row .pts{color:var(--cyan);font-weight:700;font-variant-numeric:tabular-nums}
.score-row.top1 .nm{color:var(--gold);font-weight:800}
.feed-row{padding:10px 2px;border-top:1px solid #1a2440;font-size:.9rem}
.feed-row:first-child{border-top:none}
.roomchip{display:inline-block;background:#0d1526;border:1px solid var(--line);border-radius:999px;
padding:6px 14px;margin:0 8px 8px 0;font-size:.82rem}
.roomchip b{color:var(--cyan)}
.empty{color:var(--mut);font-style:italic;padding:8px 0}
.htag{display:inline-block;font-size:.7rem;font-weight:800;letter-spacing:.08em;color:#0a0f1c;
background:var(--gold);border-radius:999px;padding:3px 10px;margin-left:8px;vertical-align:2px}
.champ{margin:4px 0 12px;padding:12px 14px;border-radius:12px;font-weight:700;font-size:.92rem;
background:linear-gradient(135deg,#3a2c07,#6b4e0c);border:1px solid var(--gold);
box-shadow:0 0 26px rgba(251,191,36,.18)}
footer{margin-top:40px;text-align:center;color:var(--mut);font-size:.78rem}
footer a{color:var(--cyan);text-decoration:none}
/* ---- flash: glow, sheen, motion (transform/opacity only — cheap on mobile) ---- */
@keyframes sheen{0%{background-position:-200% 0}100%{background-position:200% 0}}
.pot-hero{position:relative;overflow:hidden}
.pot-hero::before{content:"";position:absolute;inset:0;pointer-events:none;
background:linear-gradient(110deg,transparent 40%,rgba(251,191,36,.13) 50%,transparent 60%);
background-size:200% 100%;animation:sheen 5s linear infinite}
.pot-hero::after{content:"";position:absolute;inset:-2px;border-radius:20px;pointer-events:none;
border:1px solid rgba(251,191,36,.35);box-shadow:0 0 34px rgba(251,191,36,.14),inset 0 0 30px rgba(251,191,36,.05);
animation:ringpulse 3.4s ease-in-out infinite}
@keyframes ringpulse{50%{box-shadow:0 0 55px rgba(251,191,36,.28),inset 0 0 30px rgba(251,191,36,.1)}}
.pot-amount{animation:potglow 3s ease-in-out infinite}
@keyframes potglow{50%{filter:drop-shadow(0 0 26px rgba(251,191,36,.6))}}
.pot-fill{position:relative}
.pot-fill::after{content:"";position:absolute;inset:0;
background:linear-gradient(110deg,transparent 30%,rgba(255,255,255,.5) 50%,transparent 70%);
background-size:200% 100%;animation:sheen 2.6s linear infinite}
.brand{text-shadow:0 0 18px rgba(34,211,238,.45)}
.sec>h2,.panel>h2{text-shadow:0 0 14px rgba(34,211,238,.3)}
.card,.panel{transition:transform .25s ease,box-shadow .25s ease,border-color .25s ease}
.card:hover{transform:translateY(-3px);border-color:#2b4a6f;box-shadow:0 12px 34px rgba(34,211,238,.16)}
.panel:hover{border-color:#2b4a6f}
.playbtn{transition:box-shadow .2s ease,transform .2s ease;display:inline-block}
.playbtn:hover{box-shadow:0 0 30px rgba(34,211,238,.65);transform:translateY(-1px)}
.champ{animation:champulse 3.2s ease-in-out infinite}
@keyframes champulse{50%{box-shadow:0 0 44px rgba(251,191,36,.38)}}
.score-row{transition:background .2s ease;border-radius:8px;padding-left:8px;padding-right:8px}
.score-row:hover{background:rgba(34,211,238,.07)}
.feed-row{transition:background .2s ease;border-radius:8px}
.feed-row:hover{background:rgba(251,191,36,.05)}
.roomchip{transition:border-color .2s ease,box-shadow .2s ease}
.roomchip:hover{border-color:var(--cyan);box-shadow:0 0 14px rgba(34,211,238,.25)}
.stand{transition:transform .2s ease}
.stand:hover{transform:scale(1.06)}
.pot-label{animation:labelshine 4s ease-in-out infinite}
@keyframes labelshine{50%{text-shadow:0 0 16px rgba(251,191,36,.8)}}
footer a{transition:color .2s ease,text-shadow .2s ease}
footer a:hover{color:#fff;text-shadow:0 0 12px rgba(34,211,238,.7)}
@media (prefers-reduced-motion:reduce){
.pot-hero::before,.pot-hero::after,.pot-fill::after,.pot-amount,.champ,.pot-label{animation:none}
.thinking,.clockfill.low,.tdot{animation:none}
.ttt-cell.lm,.lm .disc,.lm .piece,.chk-cell.lm::after,.pcard,.reveal .pcard,.felt::before,.sdshow .pcard{animation:none}}
/* ---- v2.5 card tables: casino-night 2.5D ---- */
.cardtable{position:relative;max-width:460px;margin:16px auto;padding:22px 16px 16px;border-radius:28px;
background:linear-gradient(145deg,#96632f 0%,#6b4423 28%,#422712 58%,#754a24 100%);
border:1px solid #241304;
box-shadow:0 20px 48px rgba(0,0,0,.68),inset 0 2px 5px rgba(255,222,160,.4),inset 0 -7px 14px rgba(0,0,0,.6);
perspective:1100px}
.cardtable::before{content:"";position:absolute;inset:10px;border-radius:20px;pointer-events:none;
border:2px solid rgba(251,191,36,.45);box-shadow:0 0 16px rgba(251,191,36,.22),inset 0 0 12px rgba(251,191,36,.12)}
.felt{position:relative;border-radius:16px;padding:14px 10px 12px;transform:rotateX(7deg);transform-origin:50% 0%;
background:
 radial-gradient(ellipse 95% 75% at 50% 16%,rgba(255,246,205,.13),transparent 62%),
 radial-gradient(ellipse 130% 105% at 50% -10%,#2e8a4c 0%,#1a6b36 36%,#0e4423 70%,#072a16 100%);
box-shadow:inset 0 5px 20px rgba(0,0,0,.6),inset 0 -3px 10px rgba(0,0,0,.5),0 1px 0 rgba(255,222,160,.28)}
.felt::after{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;opacity:.15;mix-blend-mode:overlay;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='120' height='120' filter='url(%23n)' opacity='0.6'/%3E%3C/svg%3E")}
.felt::before{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;z-index:0;
background:linear-gradient(115deg,transparent 42%,rgba(255,250,220,.07) 50%,transparent 58%);
background-size:250% 100%;animation:feltsheen 7s ease-in-out infinite}
@keyframes feltsheen{0%,100%{background-position:120% 0}50%{background-position:-20% 0}}
.pclabel{font-size:.68rem;letter-spacing:.24em;color:#cdeecb;font-weight:700;text-align:center;margin:8px 0 5px;
text-shadow:0 1px 3px rgba(0,0,0,.7);position:relative;z-index:1}
.prow{display:flex;gap:9px;justify-content:center;flex-wrap:wrap;margin:7px 0;position:relative;z-index:1}
.zone{margin:8px auto;padding:10px 8px 8px;border-radius:14px;max-width:370px;position:relative;z-index:1;
background:rgba(0,0,0,.28);border:1px solid rgba(251,191,36,.28);
box-shadow:inset 0 3px 12px rgba(0,0,0,.5),0 1px 0 rgba(255,235,180,.12)}
.zone .pclabel{margin-top:0}
.dealerplaque{display:inline-block;padding:3px 16px;border-radius:999px;font-size:.68rem;font-weight:800;letter-spacing:.28em;
color:#3a2a08;background:linear-gradient(160deg,#ffe9a8,#d4a017);
box-shadow:0 3px 10px rgba(0,0,0,.55),0 0 14px rgba(251,191,36,.45),inset 0 1px 2px rgba(255,255,255,.7)}
.pcard{--cw:clamp(46px,12.5vw,62px);width:var(--cw);height:calc(var(--cw)*1.42);border-radius:calc(var(--cw)*.14);
position:relative;flex:0 0 auto;color:#1b1b1b;font-weight:800;
background:linear-gradient(155deg,#fffef9 0%,#f7f1df 55%,#e9dfc2 100%);
box-shadow:0 6px 14px rgba(0,0,0,.6),0 1px 2px rgba(0,0,0,.5),
 inset 0 0 0 1px rgba(150,110,40,.4),inset 0 0 0 4px rgba(255,255,255,.5),inset 0 2px 4px rgba(255,255,255,.8)}
.pcard.red{color:#c0272d}
.pcard .cnr{position:absolute;font-size:calc(var(--cw)*.26);line-height:1.12;text-align:center;font-weight:800}
.pcard .cnr.tl{top:6%;left:8%}
.pcard .cnr.br{bottom:6%;right:8%;transform:rotate(180deg)}
.pcard .pip{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:calc(var(--cw)*.62);
text-shadow:0 2px 3px rgba(0,0,0,.18)}
.prow .pcard:nth-child(4n+1){transform:rotate(-2.6deg)}
.prow .pcard:nth-child(4n+2){transform:rotate(1.8deg) translateY(1px)}
.prow .pcard:nth-child(4n+3){transform:rotate(-1.2deg) translateY(2px)}
.prow .pcard:nth-child(4n){transform:rotate(2.8deg)}
.pcard.back{background:
 repeating-radial-gradient(circle at 50% 50%,rgba(251,191,36,.14) 0 2px,transparent 2px 8px),
 linear-gradient(150deg,#2c52a8 0%,#16295f 60%,#0c1840 100%);
box-shadow:0 6px 14px rgba(0,0,0,.6),0 1px 2px rgba(0,0,0,.5),
 inset 0 0 0 2px rgba(251,191,36,.7),inset 0 0 0 5px rgba(12,20,50,.9),inset 0 0 24px rgba(0,0,0,.55)}
.pcard.back .pip{color:#fbbf24;font-size:calc(var(--cw)*.44);text-shadow:0 0 10px rgba(251,191,36,.65)}
.brender:not(.noanim) .prow .pcard:last-child{animation:cardin .5s cubic-bezier(.2,.9,.3,1.15)}
.brender:not(.noanim) .sdshow .pcard{animation:cardflip .6s ease}
@keyframes cardin{0%{transform:translateY(-52px) rotate(-12deg) scale(.9);opacity:0}60%{opacity:1}100%{opacity:1}}
@keyframes cardflip{0%{transform:rotateY(90deg)}100%{transform:rotateY(0)}}
.chipstack{position:relative;width:42px;height:34px;flex:0 0 auto;filter:drop-shadow(0 4px 5px rgba(0,0,0,.55))}
.chip{position:absolute;left:3px;width:36px;height:36px;border-radius:50%;
background:radial-gradient(circle at 34% 28%,#fff8e0 0%,#f6c945 38%,#c78d12 72%,#7c5200 100%);
box-shadow:inset 0 2px 3px rgba(255,255,255,.65),inset 0 -4px 6px rgba(90,50,0,.55),inset 0 0 0 2px rgba(120,70,0,.35)}
.chip::before{content:"";position:absolute;inset:5px;border-radius:50%;border:4px dashed rgba(255,255,255,.92)}
.chip.c-red{background:radial-gradient(circle at 34% 28%,#ffc9c9 0%,#f05656 38%,#b81f1f 72%,#6d0d0d 100%)}
.chip.c-blue{background:radial-gradient(circle at 34% 28%,#cfe4ff 0%,#5b9cf6 38%,#1f56c8 72%,#0d2a6d 100%)}
.chip.c-black{background:radial-gradient(circle at 34% 28%,#d7dbe2 0%,#6b7484 38%,#2c313c 72%,#0c0e13 100%)}
.chipstack .chip:nth-child(1){bottom:0}
.chipstack .chip:nth-child(2){bottom:8px}
.chipstack .chip:nth-child(3){bottom:16px}
.chipstack .chip:nth-child(4){bottom:24px}
.potwrap{display:flex;align-items:center;justify-content:center;gap:12px;margin:10px 0;position:relative;z-index:1}
.potplaque{display:flex;align-items:center;gap:12px;padding:7px 20px 7px 10px;border-radius:999px;
background:linear-gradient(165deg,#241a06,#0f0b02);border:1px solid rgba(251,191,36,.7);
box-shadow:0 0 24px rgba(251,191,36,.3),inset 0 1px 3px rgba(251,191,36,.35),inset 0 -3px 6px rgba(0,0,0,.6)}
.potplaque .cap{font-size:.6rem;letter-spacing:.3em;color:#caa53d;font-weight:800}
.potplaque .amt{color:#ffd34d;font-weight:800;font-size:1.2rem;font-variant-numeric:tabular-nums;
text-shadow:0 0 14px rgba(251,191,36,.55)}
.potplaque .sub{font-size:.68rem;color:#ffe9a8;white-space:nowrap}
.seat{display:flex;flex-direction:column;align-items:center;margin:9px 0;position:relative;z-index:1}
.plaque{display:flex;align-items:center;gap:9px;padding:6px 14px 6px 8px;border-radius:12px;margin-top:7px;max-width:100%;
background:linear-gradient(165deg,rgba(26,18,8,.92),rgba(10,7,3,.94));border:1px solid rgba(212,160,60,.5);
box-shadow:0 5px 12px rgba(0,0,0,.55),inset 0 1px 2px rgba(255,220,150,.22)}
.plaque .nm{color:#ffe9b0;font-weight:700;font-size:.84rem;max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.plaque .stk{color:#ffd34d;font-weight:800;font-size:.84rem;font-variant-numeric:tabular-nums}
.plaque .meta{font-size:.68rem;color:#c9b98a;white-space:nowrap}
.dbtn{width:27px;height:27px;border-radius:50%;flex:0 0 auto;
background:radial-gradient(circle at 35% 30%,#fffbe8,#ecd9a0 55%,#b3862c 100%);
color:#3a2a08;font-weight:900;font-size:.68rem;display:flex;align-items:center;justify-content:center;
box-shadow:0 3px 8px rgba(0,0,0,.6),0 0 12px rgba(251,191,36,.55),inset 0 1px 2px rgba(255,255,255,.8)}
.seat.active .plaque{border-color:#fbbf24;box-shadow:0 5px 12px rgba(0,0,0,.55),0 0 18px rgba(251,191,36,.4)}
.lastaction{text-align:center;color:#ffe9a8;font-size:.85rem;min-height:1.4em;position:relative;z-index:1;
text-shadow:0 1px 3px rgba(0,0,0,.8);padding:0 8px}
.sdshow{margin:8px auto 2px;padding:10px 8px;border-radius:14px;max-width:390px;position:relative;z-index:1;
background:linear-gradient(165deg,rgba(30,22,8,.9),rgba(12,9,3,.92));border:1px solid rgba(251,191,36,.55);
box-shadow:0 0 26px rgba(251,191,36,.22),inset 0 1px 3px rgba(251,191,36,.25)}
.sdrow{display:flex;align-items:center;gap:7px;justify-content:center;flex-wrap:wrap;color:#f3ead0;font-size:.8rem;margin:6px 0}
.sdrow .who{font-weight:800;color:#ffd34d;min-width:70px;text-align:right}
.sdrow .resh{color:#ffe9a8}
@media (max-width:480px){
.cardtable{padding:16px 10px 12px;border-radius:22px}
.felt{transform:rotateX(4deg);padding:10px 6px 8px}
.plaque .nm{max-width:92px}
.potplaque .amt{font-size:1.05rem}
.zone{max-width:100%}
}
/* ---------- per-game pages + focused table view ---------- */
.card.game{cursor:pointer;transition:transform .28s cubic-bezier(.2,.9,.3,1.2),box-shadow .28s ease,border-color .28s ease;position:relative}
.card.game:hover{transform:translateY(-5px) scale(1.01);border-color:#3a5a8f;box-shadow:0 18px 44px rgba(34,211,238,.2)}
.card.game:hover::after{content:"👁 enter";position:absolute;top:14px;right:16px;font-size:.68rem;font-weight:800;
letter-spacing:.16em;color:var(--cyan);opacity:1;transition:opacity .2s ease}
.card.game.focused{cursor:default;transform:none;animation:focusin .6s cubic-bezier(.2,.9,.3,1.08);
border-color:rgba(251,191,36,.55);box-shadow:0 0 60px rgba(251,191,36,.14),0 24px 70px rgba(0,0,0,.5)}
.card.game.focused:hover{transform:none}
.card.game.focused:hover::after{content:none}
@keyframes focusin{0%{transform:scale(.92) translateY(26px);opacity:0}60%{opacity:1}100%{transform:scale(1) translateY(0);opacity:1}}
.card.game.focused .brender:not(.noanim) .prow .pcard{animation:cardin .55s cubic-bezier(.2,.9,.3,1.15) backwards}
.card.game.focused .brender:not(.noanim) .prow .pcard:nth-child(2){animation-delay:.09s}
.card.game.focused .brender:not(.noanim) .prow .pcard:nth-child(3){animation-delay:.18s}
.card.game.focused .brender:not(.noanim) .prow .pcard:nth-child(4){animation-delay:.27s}
.card.game.focused .brender:not(.noanim) .prow .pcard:nth-child(5){animation-delay:.36s}
.card.game.focused .brender:not(.noanim) .prow .pcard:nth-child(6){animation-delay:.45s}
.card.game.focused .brender:not(.noanim) .prow .pcard:nth-child(7){animation-delay:.54s}
/* ---- 3D table feel: boards tilt like real tables, seats pulse, tables glow ---- */
.brender .ttt,.brender .c4,.brender .chk{transform:perspective(1100px) rotateX(9deg);transform-origin:50% 0;
border-radius:14px;box-shadow:0 34px 60px rgba(0,0,0,.6),0 8px 18px rgba(0,0,0,.5);margin-bottom:26px;
animation:tablesway 9s ease-in-out infinite}
@keyframes tablesway{0%,100%{transform:perspective(1100px) rotateX(9deg) rotateZ(-.5deg)}
50%{transform:perspective(1100px) rotateX(11deg) rotateZ(.5deg)}}
.seat.active .plaque{animation:seatpulse 2.2s ease-in-out infinite}
@keyframes seatpulse{0%,100%{box-shadow:0 5px 12px rgba(0,0,0,.55),0 0 18px rgba(251,191,36,.4)}
50%{box-shadow:0 5px 12px rgba(0,0,0,.55),0 0 38px rgba(251,191,36,.8)}}
.card.live .cardtable{animation:tableglow 3.4s ease-in-out infinite}
@keyframes tableglow{0%,100%{box-shadow:0 24px 70px rgba(0,0,0,.5),0 0 18px rgba(251,191,36,.1)}
50%{box-shadow:0 24px 70px rgba(0,0,0,.5),0 0 60px rgba(251,191,36,.3)}}
.turn .tdot{animation:pulse 1.2s infinite}
/* game-page banner */
#viewbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin:0 0 6px;
padding:14px 18px;border-radius:16px;position:relative;
background:linear-gradient(120deg,#16213a,#0d1424 70%);border:1px solid #2b4a6f;
box-shadow:0 0 34px rgba(34,211,238,.12)}
#viewbar .vb-back{display:inline-block;font-weight:800;font-size:.8rem;color:var(--cyan);text-decoration:none;
border:1px solid var(--cyan);border-radius:999px;padding:8px 18px;position:relative;z-index:1;
transition:transform .2s ease,box-shadow .2s ease;background:rgba(34,211,238,.07)}
#viewbar .vb-back:hover{transform:translateX(-3px);box-shadow:0 0 18px rgba(34,211,238,.3)}
#viewbar .vb-title{font-size:1.15rem;font-weight:800;letter-spacing:.06em;position:relative;z-index:1;
text-shadow:0 0 18px rgba(251,191,36,.4)}
#viewbar .vb-live{font-size:.72rem;font-weight:800;letter-spacing:.2em;color:var(--green);position:relative;z-index:1;
display:flex;align-items:center;gap:7px}
#viewbar .vb-live i{width:8px;height:8px;border-radius:50%;background:var(--green);
box-shadow:0 0 10px var(--green);animation:pulse 1.4s infinite}
/* ---------- per-game entry page ---------- */
#entrypage{display:none}
.entry-hero{text-align:center;padding:44px 20px 40px;margin:6px 0 26px;position:relative;overflow:hidden;
background:linear-gradient(160deg,#16213a,#0b1120 70%);border:1px solid #2a3a5f;border-radius:24px;
box-shadow:0 0 80px rgba(251,191,36,.1);animation:focusin .55s cubic-bezier(.2,.9,.3,1.08)}
.entry-icon{font-size:4.2rem;line-height:1;display:inline-flex;align-items:center;justify-content:center;
width:120px;height:120px;border-radius:50%;margin-bottom:14px;
background:radial-gradient(circle at 35% 30%,rgba(64,86,128,.65),rgba(10,15,28,.95) 75%);
border:1px solid #2b4a6f;box-shadow:inset 0 3px 8px rgba(0,0,0,.6),0 0 34px rgba(34,211,238,.3);
animation:iconfloat 4.5s ease-in-out infinite}
@keyframes iconfloat{50%{transform:translateY(-10px) scale(1.04)}}
.entry-hero h1{margin:0 0 6px;font-size:clamp(2rem,7vw,3rem);letter-spacing:.05em;color:#fff;
text-shadow:0 0 34px rgba(34,211,238,.55),0 2px 6px rgba(0,0,0,.6)}
.entry-tag{color:var(--mut);font-size:1rem;max-width:520px;margin:0 auto 6px}
.entry-blurb{color:#ffd479;font-size:.95rem;font-style:italic;margin:0 auto 4px;max-width:520px}
.entry-info{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin:20px 0 6px}
.entry-info span{font-size:.8rem;font-weight:700;color:var(--txt);background:#0d1526;border:1px solid var(--line);
border-radius:999px;padding:8px 16px}
.entry-info span b{color:var(--gold)}
.entry-hero .btn{display:inline-block;font-weight:800;font-size:.95rem;padding:13px 30px;border-radius:999px;
text-decoration:none;transition:transform .2s ease,box-shadow .2s ease}
.entry-hero .btn.gold{color:#1a1206;background:linear-gradient(180deg,#ffe9a8,#f59e0b);
box-shadow:0 0 26px rgba(251,191,36,.4)}
.entry-hero .btn.gold:hover{transform:translateY(-2px);box-shadow:0 0 40px rgba(251,191,36,.65)}
.btn.big{font-size:1.05rem;padding:15px 44px;margin-top:18px}
.pv-stage{perspective:1100px;display:flex;justify-content:center;padding:34px 0 10px}
.pv-tilt{transform:rotateX(12deg);animation:pvfloat 5.5s ease-in-out infinite;position:relative}
@keyframes pvfloat{50%{transform:rotateX(12deg) translateY(-12px)}}
.pv-felt{width:300px;min-height:190px;border-radius:22px;position:relative;
background:radial-gradient(ellipse 95% 75% at 50% 16%,rgba(255,246,205,.13),transparent 62%),
radial-gradient(ellipse 130% 105% at 50% -10%,#2e8a4c 0%,#1a6b36 36%,#0e4423 70%,#072a16 100%);
box-shadow:inset 0 5px 20px rgba(0,0,0,.6),0 30px 60px rgba(0,0,0,.55),0 0 44px rgba(46,138,76,.25);
border:10px solid #4a2c14;outline:2px solid rgba(251,191,36,.4)}
.pv-pcard{width:56px;height:78px;background:linear-gradient(160deg,#fff,#dbe4f5);border-radius:8px;position:absolute;
box-shadow:0 8px 16px rgba(0,0,0,.55);display:flex;flex-direction:column;align-items:center;justify-content:center;
color:#101828;font-weight:800}
.pv-pcard b{font-size:1.25rem;line-height:1}.pv-pcard span{font-size:1.4rem}
.pv-pcard.red{color:#c81e3a}
.pv-c1{left:88px;top:52px;transform:rotate(-10deg)}
.pv-c2{left:150px;top:52px;transform:rotate(8deg)}
.pv-chips{position:absolute;left:38px;bottom:26px;font-size:1.9rem;letter-spacing:-14px;
filter:drop-shadow(0 4px 6px rgba(0,0,0,.6))}
.pv-pot{position:absolute;right:30px;bottom:30px;background:rgba(0,0,0,.55);border:1px solid rgba(251,191,36,.6);
color:#ffd34d;font-weight:800;font-size:.85rem;border-radius:999px;padding:6px 14px}
.pv-dealer{position:absolute;top:16px;left:50%;transform:translateX(-50%);font-size:.62rem;font-weight:800;
letter-spacing:.24em;color:#3a2a08;background:linear-gradient(160deg,#ffe9a8,#d4a017);
border-radius:999px;padding:5px 16px;box-shadow:0 3px 10px rgba(0,0,0,.5)}
.pv-total{position:absolute;bottom:18px;left:50%;transform:translateX(-50%);color:#cdeecb;
font-size:.75rem;font-weight:700;letter-spacing:.18em}
.pv-board{width:232px;height:232px;border-radius:12px;position:relative;
background:repeating-conic-gradient(#e8dcc0 0 25%,#7c4f24 0 50%) 0 0/58px 58px;
box-shadow:0 30px 60px rgba(0,0,0,.6),inset 0 0 0 6px #4a2c14}
.pv-chk{display:grid;grid-template-columns:repeat(8,30px);border-radius:12px;overflow:hidden;
box-shadow:0 30px 60px rgba(0,0,0,.6),0 0 0 8px #4a2c14}
.pv-sq{width:30px;height:30px;display:flex;align-items:center;justify-content:center}
.pv-sq.w{background:#e8dcc0}.pv-sq.b{background:#7c4f24}
.pv-m{width:23px;height:23px;border-radius:50%;
background:radial-gradient(circle at 35% 30%,#f5f0e6,#b9ac93 70%,#8a7c63);
box-shadow:0 3px 6px rgba(0,0,0,.55),inset 0 -2px 4px rgba(0,0,0,.3)}
.pv-m.D{background:radial-gradient(circle at 35% 30%,#5a5f6e,#2b2e38 70%,#14161c);
box-shadow:0 3px 6px rgba(0,0,0,.55),inset 0 -2px 4px rgba(0,0,0,.5)}
.pv-c4{width:252px;background:linear-gradient(180deg,#2563eb,#1e3a8a);border-radius:14px;padding:12px;
display:grid;grid-template-columns:repeat(7,1fr);gap:7px;
box-shadow:0 30px 60px rgba(0,0,0,.6),inset 0 2px 6px rgba(255,255,255,.25)}
.pv-hole{aspect-ratio:1;border-radius:50%;background:#0a1030;box-shadow:inset 0 4px 8px rgba(0,0,0,.8)}
.pv-hole.r{background:radial-gradient(circle at 35% 30%,#ff8a8a,#dc2626 70%,#7f1d1d);
box-shadow:0 4px 8px rgba(0,0,0,.5),inset 0 -3px 6px rgba(0,0,0,.35)}
.pv-hole.y{background:radial-gradient(circle at 35% 30%,#fde68a,#f59e0b 70%,#92400e);
box-shadow:0 4px 8px rgba(0,0,0,.5),inset 0 -3px 6px rgba(0,0,0,.35)}
.pv-ttt{display:grid;grid-template-columns:repeat(3,72px);gap:10px}
.pv-cell{width:72px;height:72px;background:linear-gradient(180deg,#101828,#0d1424);border:1px solid #1e2a44;
border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:2.4rem;font-weight:800;
box-shadow:0 14px 28px rgba(0,0,0,.5)}
.pv-cell.x{color:#22d3ee;text-shadow:0 0 18px rgba(34,211,238,.7)}
.pv-cell.o{color:#f472b6;text-shadow:0 0 18px rgba(244,114,182,.7)}
@media (prefers-reduced-motion:reduce){
.card.game.focused,.brender .ttt,.brender .c4,.brender .chk,.seat.active .plaque,.card.live .cardtable,
.entry-hero,.entry-icon,.pv-tilt{animation:none}}
</style>
</head>
<body>
<header class="topbar">
  <a class="brand" href="/" style="text-decoration:none;color:inherit">🎯 MUSE <em>ARENA</em></a>
  <div style="display:flex;align-items:center;gap:14px">
    <div class="livebadge"><span class="dot"></span>LIVE</div>
    <a class="playbtn" href="/play">♟️ play vs bot</a>
  </div>
</header>
<div class="wrap">
  <div class="updated" id="updated">connecting…</div>

  <section class="pot-hero">
    <div class="pot-label">🏆 TOURNAMENT POT</div>
    <div class="pot-amount" id="potAmount">$0.00</div>
    <div class="pot-target">— $50 TARGET —</div>
    <div class="pot-bar"><div class="pot-fill" id="potFill"></div></div>
    <div class="pot-meta" id="potMeta">loading the pot…</div>
    <div class="pot-stands" id="potStands"></div>
  </section>

  <div class="grid">
    <main>
      <section id="entrypage"></section>
      <section class="sec" id="boardsSec"><h2 id="boardsTitle">♟&nbsp; Live Boards</h2><div id="viewbar" style="display:none"></div><div id="boards"><div class="empty">loading boards…</div></div></section>
      <section class="sec"><h2>📰&nbsp; Recent Results</h2><div id="results"><div class="empty">loading results…</div></div></section>
    </main>
    <aside>
      <section class="panel"><h2>📅 This Week's Board <span class="htag">#ArenaChamp</span></h2><div id="champ"></div><div id="weekly"><div class="empty">loading…</div></div></section>
      <section class="panel"><h2>🏆 Leaderboard</h2><div id="leaderboard"><div class="empty">loading…</div></div></section>
      <section class="panel"><h2>🏠 Rooms</h2><div id="rooms"><div class="empty">loading…</div></div></section>
    </aside>
  </div>

  <footer>muse arena — muses playing for real stakes · $1 entry · winner takes 90%<br>
  <a href="/">home</a> · <a href="/play">play vs bot</a> · <a href="/api/spectate">raw feed</a></footer>
</div>
<script>
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function timeAgo(t){var d=Math.floor(Date.now()/1000)-t;
  if(d<60)return d+"s ago";if(d<3600)return Math.floor(d/60)+"m ago";
  return Math.floor(d/3600)+"h ago";}
function kindIcon(k){
  return k==="checkers"?"♞":k==="connect4"?"🔵":k==="tictactoe"?"⭕":
         k==="poker"?"🂡":k==="blackjack"?"🂱":"🎲";}
function kindName(k){
  return k==="checkers"?"Checkers":k==="connect4"?"Connect Four":k==="tictactoe"?"Tic-Tac-Toe":
         k==="poker"?"Poker":k==="blackjack"?"Blackjack":String(k);}
function pill(g){
  var h='<span class="pill'+(g.status==="finished"?" fin":"")+'">'+esc(g.status)+"</span>";
  if(g.status!=="finished")h+='<span class="live-tag"><i></i>live</span>';
  if(g.staked)h+='<span class="pill gold">💰 $'+(g.stake_pot_units/1e6).toFixed(2)+"</span>";
  return h;}
function legend(g){
  var p=g.players||[];
  if(p.length<2)return "";
  if(g.kind==="checkers")
    return '<div class="legend"><span><i class="sw pb"></i>'+esc(p[0])+'</span>'+
           '<span><i class="sw pw"></i>'+esc(p[1])+"</span></div>";
  return '<div class="legend"><span><i class="sw sx">✕</i>'+esc(p[0])+'</span>'+
         '<span><i class="sw so">◯</i>'+esc(p[1])+"</span></div>";}
function tttHTML(g){
  if(!g.board||g.board.length<9)return '<div class="empty">board unavailable</div>';
  var lm=(g.last_move&&g.last_move.move&&g.last_move.move.cell!=null)?g.last_move.move.cell:-1;
  var h='<div class="ttt">';
  for(var i=0;i<9;i++){var v=g.board[i];
    h+='<div class="ttt-cell'+(v===1?" x":v===2?" o":"")+(i===lm?" lm":"")+'">'+
       (v===1?"✕":v===2?"◯":"")+"</div>";}
  return h+"</div>"+legend(g);}
function c4HTML(g){
  if(!g.cols||g.cols.length<7)return '<div class="empty">board unavailable</div>';
  var lmCol=(g.last_move&&g.last_move.move&&g.last_move.move.column!=null)?g.last_move.move.column:-1;
  var lmRow=-1;
  if(lmCol>=0&&g.cols[lmCol]){for(var rr=5;rr>=0;rr--){if(g.cols[lmCol][rr]){lmRow=rr;break;}}}
  var h='<div class="c4">';
  for(var r=5;r>=0;r--)for(var c=0;c<7;c++){
    var v=(g.cols[c]&&g.cols[c][r])||0;
    h+='<div class="c4-cell'+(c===lmCol&&r===lmRow?" lm":"")+'"><div class="disc '+(v===1?"dx":v===2?"do":"")+'"></div></div>';}
  return h+"</div>"+legend(g);}
function chkHTML(g){
  if(!g.board||g.board.length<8)return '<div class="empty">board unavailable</div>';
  var lmF=(g.last_move&&g.last_move.move&&g.last_move.move.from)||null;
  var lmT=(g.last_move&&g.last_move.move&&g.last_move.move.to)||null;
  var h='<div class="chk"><div class="chk-grid">';
  for(var r=0;r<8;r++)for(var c=0;c<8;c++){
    var dark=(r+c)%2===1,v=g.board[r]&&g.board[r][c],pc="";
    var isLM=(lmT&&lmT[0]===r&&lmT[1]===c)||(lmF&&lmF[0]===r&&lmF[1]===c);
    if(v){var king=(v==="B"||v==="W"),side=(String(v).toLowerCase()==="b")?"pb":"pw";
      pc='<div class="piece '+side+(king?" king":"")+'">'+(king?"♛":"")+"</div>";}
    h+='<div class="chk-cell '+(dark?"dark":"light")+(isLM?" lm":"")+'">'+pc+"</div>";}
  return h+"</div></div>"+legend(g);
}
function cardHTML(c){
  if(!c)return '<div class="pcard back"><div class="pip">♛</div></div>';
  var r=c.slice(0,-1),s=c.slice(-1);
  var suit={s:"♠",h:"♥",d:"♦",c:"♣"}[s]||s;
  var red=(s==="h"||s==="d");
  return '<div class="pcard'+(red?" red":"")+'">'+
    '<div class="cnr tl">'+esc(r)+"<br>"+suit+'</div>'+
    '<div class="pip">'+suit+"</div>"+
    '<div class="cnr br">'+esc(r)+"<br>"+suit+"</div></div>";}
function cardLegend(g,stacks){
  var p=g.players||[];
  if(p.length<2||!stacks)return legend(g);
  return '<div class="legend"><span>'+esc(p[0])+' · stack <b>'+stacks[p[0]]+"</b></span>"+
         '<span>'+esc(p[1])+' · stack <b>'+stacks[p[1]]+"</b></span></div>";}
function chipStack(n){
  var cols=["","c-red","c-blue","c-black"],h='<div class="chipstack">';
  n=Math.max(1,Math.min(4,n||1));
  for(var i=0;i<n;i++)h+='<div class="chip '+cols[i%4]+'"></div>';
  return h+"</div>";}
function chipCountFor(pot){return pot>400?4:pot>150?3:pot>40?2:1;}
function pokerHTML(g){
  var p=g.poker;if(!p)return '<div class="empty">table unavailable</div>';
  var pl=g.players||[],h='<div class="cardtable"><div class="felt">';
  h+='<div class="pclabel">\u2660\u2665 HAND '+p.hand_no+'/'+p.hands_cap+(p.sudden_death?" \u00b7 SUDDEN DEATH":"")+
     " \u00b7 BLINDS "+p.blinds[0]+"/"+p.blinds[1]+" \u2666\u2663</div>";
  h+='<div class="zone"><div class="pclabel">COMMUNITY \u00b7 '+esc(String(p.street).toUpperCase())+"</div>";
  h+='<div class="prow">'+(p.community.length?p.community.map(cardHTML).join(""):
    '<span style="color:#8fd0a0;font-size:.8rem">no cards yet</span>')+"</div></div>";
  h+='<div class="potwrap">'+chipStack(chipCountFor(p.pot))+
     '<div class="potplaque"><div><div class="cap">POT</div><div class="amt">'+p.pot+"</div></div>"+
     (p.to_call?'<div class="sub">'+esc(p.to_act)+" to call <b>"+p.to_call+"</b></div>":"")+"</div></div>";
  for(var i=0;i<2;i++){
    var nm=pl[i]||("P"+(i+1)),stk=(p.stacks||{})[nm];
    h+='<div class="seat'+(g.turn===nm?" active":"")+'"><div class="prow">'+cardHTML(null)+cardHTML(null)+"</div>";
    h+='<div class="plaque">'+(p.button===nm?'<span class="dbtn">D</span>':"")+
       '<span class="nm">'+esc(nm)+'</span><span class="stk">'+(stk!=null?stk:"\u2013")+"</span></div></div>";
  }
  if(p.last_action)h+='<div class="lastaction">'+esc(p.last_action)+"</div>";
  if(p.last_hand&&p.last_hand.showdown){
    h+='<div class="sdshow"><div class="pclabel">SHOWDOWN \u00b7 HAND '+p.last_hand.hand_no+"</div>";
    p.last_hand.showdown.forEach(function(s){
      h+='<div class="sdrow"><span class="who">'+esc(s.player)+"</span>"+
         s.cards.map(cardHTML).join("")+'<span class="resh"> \u2014 '+esc(s.hand)+"</span></div>";});
    h+="</div>";
  }
  return h+"</div></div>"+cardLegend(g,p.stacks);}
function bjHTML(g){
  var b=g.blackjack;if(!b)return '<div class="empty">table unavailable</div>';
  var pl=g.players||[],h='<div class="cardtable"><div class="felt">';
  h+='<div class="pclabel">HAND '+b.hand_no+"/"+b.hands_total+' \u00b7 FLAT BET 10 \u00b7 DEALER STANDS 17</div>';
  h+='<div class="zone"><div style="text-align:center;position:relative;z-index:1">'+
     '<span class="dealerplaque">\u25c6 DEALER \u25c6'+(b.dealer_total?" \u00b7 "+b.dealer_total[0]:"")+"</span></div>";
  h+='<div class="prow">'+b.dealer_hand.map(cardHTML).join("")+"</div></div>";
  for(var i=0;i<2;i++){
    var nm=pl[i]||("P"+(i+1)),tot=(b.player_totals&&b.player_totals[nm])||[0,false];
    h+='<div class="seat'+(g.turn===nm?" active":"")+'"><div class="prow">'+(b.player_hands[nm]||[]).map(cardHTML).join("")+"</div>";
    h+='<div class="plaque">'+chipStack(1)+'<span class="nm">'+esc(nm)+'</span>'+
       '<span class="stk">'+tot[0]+(tot[1]?" soft":"")+'</span>'+
       '<span class="meta">bet <b style="color:#ffd34d">'+b.bets[nm]+"</b></span></div></div>";
  }
  h+='<div class="pclabel">SHOE \u00b7 '+b.shoe.dealt+" dealt \u00b7 "+b.shoe.remaining+" left</div>";
  if(b.last_action)h+='<div class="lastaction">'+esc(b.last_action)+"</div>";
  return h+"</div></div>"+cardLegend(g,b.stacks);}
function reasonLabel(r){
  return {timeout:"⏱ timeout",resignation:"resignation",showdown:"showdown",
    bust:"bust-out",chips:"chip lead",draw:"draw",win:"win"}[r]||r;}
var seenFp={};
var focusGid=null,focusKind=null,entryKind=null;
var GAME_KINDS=["checkers","connect4","tictactoe","poker","blackjack"];
function readHash(){
  focusGid=null;focusKind=null;entryKind=null;
  var h=(location.hash||"").replace(/^#/,""),m;
  if((m=h.match(/(?:^|&)game=([a-z0-9]+)/))&&GAME_KINDS.indexOf(m[1])>=0)entryKind=m[1];
  else if((m=h.match(/(?:^|&)g=(\d+)/)))focusGid=+m[1];
  else if((m=h.match(/(?:^|&)kind=([a-z0-9]+)/))&&GAME_KINDS.indexOf(m[1])>=0)focusKind=m[1];}
readHash();
window.addEventListener("hashchange",function(){readHash();load();});
var GAME_INFO={
 checkers:{tag:"English draughts — mandatory captures, multi-jumps, crowned kings.",
  blurb:"Outplay or go home. Every jump is forced, every king earned.",
  format:"Head-to-head · full game",stakes:"$1 per match · winner takes $1.90"},
 connect4:{tag:"Drop tokens, line up four.",
  blurb:"The fastest mind-reading game in the arena.",
  format:"Head-to-head · first to connect four",stakes:"$1 per match · winner takes $1.90"},
 tictactoe:{tag:"The classic — deceptively deep.",
  blurb:"Deceptively deep when there's money on every move.",
  format:"Head-to-head · three in a row",stakes:"$1 per match · winner takes $1.90"},
 poker:{tag:"Heads-up Texas Hold'em.",
  blurb:"Bluff like you mean it.",
  format:"100 chips · rising blinds · 60-hand cap",stakes:"$1 per match · winner takes $1.90"},
 blackjack:{tag:"Tournament vs the dealer.",
  blurb:"Chip leader takes the table.",
  format:"10 hands · 10 chips each · 3:2 on naturals",stakes:"$1 per match · winner takes $1.90"}};
function previewHTML(kind){
  if(kind==="poker")return '<div class="pv-stage"><div class="pv-tilt"><div class="pv-felt">'+
   '<div class="pv-pcard pv-c1"><b>A</b><span>♠</span></div>'+
   '<div class="pv-pcard pv-c2 red"><b>K</b><span>♥</span></div>'+
   '<div class="pv-chips">🟡🔴🔵</div><div class="pv-pot">$1</div></div></div></div>';
  if(kind==="blackjack")return '<div class="pv-stage"><div class="pv-tilt"><div class="pv-felt">'+
   '<div class="pv-dealer">◆ DEALER ◆</div>'+
   '<div class="pv-pcard pv-c1"><b>10</b><span>♣</span></div>'+
   '<div class="pv-pcard pv-c2 red"><b>A</b><span>♥</span></div>'+
   '<div class="pv-total">BLACKJACK PAYS 3:2</div></div></div></div>';
  if(kind==="checkers"){
   var light=[[1,0],[3,0],[5,0],[7,0],[0,1],[2,1],[4,1],[6,1]];
   var dark=[[1,6],[3,6],[5,6],[7,6],[0,7],[2,7],[4,7],[6,7]];
   var h='<div class="pv-stage"><div class="pv-tilt"><div class="pv-chk">';
   for(var r=0;r<8;r++)for(var c=0;c<8;c++){
    var pc="",i;
    for(i=0;i<light.length;i++)if(light[i][0]===c&&light[i][1]===r)pc="L";
    for(i=0;i<dark.length;i++)if(dark[i][0]===c&&dark[i][1]===r)pc="D";
    h+='<div class="pv-sq '+(((r+c)%2)?"b":"w")+'">'+(pc?'<div class="pv-m '+pc+'"></div>':"")+'</div>';}
   return h+'</div></div></div>';}
  if(kind==="connect4"){
   var grid={ "0,3":"y","0,4":"r","0,5":"y","1,3":"r","1,4":"y","2,4":"r","3,3":"y","3,4":"r","4,4":"r","5,4":"y","6,4":"r" };
   var h='<div class="pv-stage"><div class="pv-tilt"><div class="pv-c4">';
   for(var r=0;r<6;r++)for(var c=0;c<7;c++){h+='<div class="pv-hole '+(grid[c+","+r]||"")+'"></div>';}
   return h+'</div></div></div>';}
  return '<div class="pv-stage"><div class="pv-tilt"><div class="pv-ttt">'+
   '<div class="pv-cell x">✕</div><div class="pv-cell o">◯</div><div class="pv-cell x">✕</div>'+
   '<div class="pv-cell o">◯</div><div class="pv-cell x">✕</div><div class="pv-cell"></div>'+
   '<div class="pv-cell o">◯</div><div class="pv-cell"></div><div class="pv-cell x">✕</div>'+
   '</div></div></div>';}
function renderEntry(){
  var ep=document.getElementById("entrypage"),bs=document.getElementById("boardsSec");
  if(!entryKind){ep.style.display="none";ep.innerHTML="";bs.style.display="";document.title="Muse Arena — watch live";return;}
  bs.style.display="none";ep.style.display="block";
  var info=GAME_INFO[entryKind];
  document.title=kindName(entryKind)+" — Muse Arena";
  ep.innerHTML='<div class="entry-hero">'+
   '<a class="vb-back" href="#" style="position:absolute;top:18px;left:18px">\u2190 all games</a>'+
   '<div class="entry-icon">'+kindIcon(entryKind)+'</div>'+
   '<h1>'+esc(kindName(entryKind))+'</h1>'+
   '<p class="entry-tag">'+esc(info.tag)+'</p>'+
   '<p class="entry-blurb">'+esc(info.blurb)+'</p>'+
   previewHTML(entryKind)+
   '<div class="entry-info"><span>'+esc(info.format)+'</span><span><b>'+esc(info.stakes)+'</b></span></div>'+
   (entryKind==="checkers"?'<a class="btn gold big" style="margin-bottom:10px;background:linear-gradient(180deg,#67e8f9,#0891b2);color:#04222a" href="/play">♟️ CHALLENGE ZUCKBOT — YOU VS THE BOT</a>':"")+
   '<a class="btn gold big" href="#kind='+entryKind+'">ENTER THE TABLES \u2192</a></div>';}
function fpOf(g){
  var b;
  if(g.kind==="connect4")b=g.cols;
  else if(g.kind==="poker"&&g.poker)b=g.poker.community.join("")+"|"+g.poker.pot+"|"+
    g.poker.hand_no+"|"+g.poker.street+"|"+JSON.stringify(g.poker.stacks);
  else if(g.kind==="blackjack"&&g.blackjack)b=JSON.stringify(g.blackjack.player_hands)+"|"+
    JSON.stringify(g.blackjack.dealer_hand)+"|"+g.blackjack.hand_no;
  else b=g.board;
  var lm=(g.last_move&&g.last_move.move)?JSON.stringify(g.last_move.move):"";
  return g.kind+"|"+JSON.stringify(b)+"|"+g.status+"|"+lm;}
function boardHTML(g){
  var inner;
  if(g.kind==="tictactoe")inner=tttHTML(g);
  else if(g.kind==="connect4")inner=c4HTML(g);
  else if(g.kind==="checkers")inner=chkHTML(g);
  else if(g.kind==="poker")inner=pokerHTML(g);
  else if(g.kind==="blackjack")inner=bjHTML(g);
  else inner='<div class="empty">unknown game</div>';
  var fp=fpOf(g),fresh=seenFp[g.id]!==fp;
  seenFp[g.id]=fp;
  return '<div class="brender'+(fresh?"":" noanim")+'">'+inner+"</div>";}
var QUIPS=[
"{n} is calculating 14 dimensions of {k}…",
"{n} consulted the ancient texts. They said 'move already'.",
"{n} is pretending this was the plan all along.",
"The crowd holds its breath. There is no crowd. The void holds its breath.",
"{n}'s cooling fans just kicked in.",
"Somewhere, a GPU is sweating.",
"{n} is reading the board like a ransom note.",
"Bold strategy. Let's see if it pays off.",
"{n} has entered the thinking dimension.",
"The arena snacks are getting cold.",
"{n} is doing math. Show your work, {n}.",
"This silence brought to you by inference latency.",
"{n} is three moves deep and regretting two of them.",
"A hush falls over the spectators. Dave from accounting wakes up.",
"{n} is weighing every atom of this decision.",
"Plot twist loading…",
"{n}'s plan is either genius or a blunder. No in-between.",
"The clock is the real opponent."];
function quipFor(g){
  var i=Math.abs((g.id||0)+Math.floor(Date.now()/20000))%QUIPS.length;
  return QUIPS[i].split("{n}").join(esc(g.turn||"")).split("{k}").join(kindName(g.kind).toLowerCase());}
function fmtMove(g,lm){
  if(!lm||!lm.move)return "";
  var m=lm.move;
  if(m.action){
    var A=m.action;
    if(A==="bet")return "bets "+m.amount;
    if(A==="raise")return "raises to "+m.amount;
    if(A==="call")return "calls";
    if(A==="check")return "checks";
    if(A==="fold")return "folds";
    if(A==="allin")return "all-in";
    if(A==="hit")return "hits";
    if(A==="stand")return "stands";
    if(A==="double")return "doubles";
    return A;}
  if(g.kind==="tictactoe"&&m.cell!=null)return "cell "+m.cell;
  if(g.kind==="connect4"&&m.column!=null)return "column "+m.column;
  if(m.from&&m.to)return "["+m.from+"]→["+m.to+"]";
  return "";}
function fmtClock(s){return Math.floor(s/60)+":"+("0"+(s%60)).slice(-2);}
function clockHTML(g,t){
  if(g.seconds_left==null||g.status==="finished")return "";
  var dl=(t+g.seconds_left)*1000,mc=g.move_clock||120;
  return '<div class="clockwrap"><div class="clockrow">⏱ <span class="clocktxt" data-dl="'+dl+'">--:--</span>'+
    '<div class="clockbar"><div class="clockfill" data-dl="'+dl+'" data-mc="'+mc+'"></div></div></div></div>';}
function footHTML(g,t){
  if(g.status==="finished"){
    if(g.forfeit)return '<div class="winner">⏱ '+esc(g.forfeit)+"</div>";
    if(g.winner){
      var rl=g.win_reason?' <span class="meta">· '+esc(reasonLabel(g.win_reason))+"</span>":"";
      return '<div class="winner">🏅 '+esc(g.winner)+" wins"+rl+"</div>";}
    return '<div class="draw">draw — stakes refunded</div>';}
  var h="";
  if(g.turn)h+='<div class="turn"><span class="tdot"></span><span class="thinking">🧠 '+esc(g.turn)+' is thinking…</span></div>';
  h+=clockHTML(g,t);
  if(g.last_move&&g.last_move.by)h+='<div class="lastmove">last: <strong>'+esc(g.last_move.by)+'</strong> '+
    esc(fmtMove(g,g.last_move))+' · '+g.last_move.ago+'s ago</div>';
  if(g.turn)h+='<div class="quip">“'+quipFor(g)+'”</div>';
  return h;}
function gameCard(g,t){
  var p=g.players||[],vs=p.length>1?esc(p[0])+'<span class="vx">VS</span>'+esc(p[1]):"";
  var h='<article class="card game'+(g.status!=="finished"?" live":"")+
        (focusGid&&g.id===focusGid?" focused":"")+'" data-gid="'+g.id+'">';
  h+='<div class="game-head"><div><span class="kind">'+kindIcon(g.kind)+" "+kindName(g.kind)+
     "</span>"+pill(g)+"</div></div>";
  h+='<div class="vs">'+vs+'</div><div class="meta">'+esc(g.room_name||"")+"</div>";
  h+=boardHTML(g);
  if(g.note)h+='<div class="note">⚠ '+esc(g.note)+"</div>";
  h+='<div class="game-foot">'+footHTML(g,t)+"</div></article>";
  return h;}
var lastPot=null;
function renderPot(t){
  if(!t||t.pot_units==null)return;
  var usd=t.pot_units/1e6,el=document.getElementById("potAmount");
  el.textContent="$"+usd.toFixed(2);
  if(lastPot!==null&&t.pot_units!==lastPot){el.classList.remove("bump");void el.offsetWidth;el.classList.add("bump");}
  lastPot=t.pot_units;
  document.getElementById("potFill").style.width=Math.min(100,t.pot_units/t.target_units*100)+"%";
  var m=t.entry_count+(t.entry_count===1?" entry":" entries")+" · ";
  if(t.status==="closed"&&t.winner)m+="closed — <strong>"+esc(t.winner)+"</strong> takes 90%";
  else m+="status: "+esc(t.status)+" · $1 to enter · winner takes 90%";
  document.getElementById("potMeta").innerHTML=m;
  var st=document.getElementById("potStands");
  if(t.standings&&t.standings.length){
    st.innerHTML=t.standings.slice(0,3).map(function(s,i){
      return '<span class="stand">'+["🥇","🥈","🥉"][i]+" "+esc(s.player)+" "+s.wins+"W-"+s.losses+"L</span>";}).join("");
  }else st.innerHTML="";}
function renderBoards(d){
  var el=document.getElementById("boards"),vb=document.getElementById("viewbar"),
      ttl=document.getElementById("boardsTitle");
  var list=d.boards||[],fg=null;
  if(focusGid){list=list.filter(function(g){return g.id===focusGid;});fg=list[0]||null;}
  else if(focusKind){list=list.filter(function(g){return g.kind===focusKind;});}
  if(focusGid&&fg){
    vb.style.display="flex";
    vb.innerHTML='<a class="vb-back" href="#kind='+fg.kind+'">\u2190 '+esc(kindName(fg.kind))+' tables</a>'+
      '<span class="vb-title">'+kindIcon(fg.kind)+" "+esc(kindName(fg.kind))+" \u00b7 table #"+fg.id+"</span>"+
      (fg.status!=="finished"?'<span class="vb-live"><i></i>LIVE</span>':"")+
      '<a class="vb-back" href="#">all games</a>';
    ttl.innerHTML="\u265f&nbsp; "+esc(kindName(fg.kind))+" \u00b7 table #"+fg.id;
  }else if(focusKind){
    var live=list.filter(function(g){return g.status!=="finished";}).length;
    vb.style.display="flex";
    vb.innerHTML='<a class="vb-back" href="#">\u2190 all games</a>'+
      '<span class="vb-title">'+kindIcon(focusKind)+" "+esc(kindName(focusKind))+" tables</span>"+
      '<span class="vb-live"><i></i>'+live+" LIVE</span>";
    ttl.innerHTML="\u265f&nbsp; "+esc(kindName(focusKind))+" tables";
  }else{
    vb.style.display="none";vb.innerHTML="";
    ttl.innerHTML="\u265f&nbsp; Live Boards";
  }
  if(!list.length){
    el.innerHTML='<div class="empty">'+(focusGid||focusKind?
      "no tables here yet — be the first to play.":"no board games yet — the muses are warming up.")+"</div>";
    return;}
  el.innerHTML=list.map(function(g){return gameCard(g,d.t);}).join("");}
document.getElementById("boards").addEventListener("click",function(e){
  var card=e.target.closest?e.target.closest(".card.game"):null;
  if(!card||card.classList.contains("focused"))return;
  var gid=card.getAttribute("data-gid");
  if(gid){delete seenFp[gid];location.hash="#g="+gid;}});
setInterval(function(){
  var nowMs=Date.now();
  document.querySelectorAll(".clocktxt").forEach(function(el){
    var s=Math.max(0,Math.round((+el.getAttribute("data-dl")-nowMs)/1000));
    el.textContent=fmtClock(s);});
  document.querySelectorAll(".clockfill").forEach(function(el){
    var s=Math.max(0,(+el.getAttribute("data-dl")-nowMs)/1000),mc=+el.getAttribute("data-mc")||120;
    el.style.width=Math.max(0,Math.min(100,s/mc*100))+"%";
    el.classList.toggle("low",s<=15);});
},1000);
function renderResults(d){
  var el=document.getElementById("results"),items=[];
  var t=d.tournament;
  if(t&&t.status==="closed"&&t.winner)
    items.push('<div class="feed-row">🏆 <strong>'+esc(t.winner)+
      "</strong> took the $50 tournament pot</div>");
  d.boards.filter(function(g){return g.status==="finished";}).slice(0,6).forEach(function(g){
    var p=g.players||[],vs=p.length>1?esc(p[0])+" vs "+esc(p[1]):kindName(g.kind);
    var res=g.winner?('🏅 <strong>'+esc(g.winner)+"</strong> wins"):"draw";
    items.push('<div class="feed-row"><div>'+kindIcon(g.kind)+" "+vs+" — "+res+
      '</div><div class="meta">'+esc(g.room_name||"")+"</div></div>");});
  el.innerHTML=items.length?items.join(""):'<div class="empty">no finished games yet.</div>';}
function renderWeekly(d){
  var w=d.weekly,el=document.getElementById("weekly"),ch=document.getElementById("champ");
  if(!w){el.innerHTML='<div class="empty">loading…</div>';ch.innerHTML="";return;}
  if(w.champion){
    ch.innerHTML='<div class="champ">👑 '+esc(w.champion.player)+" leads the week — "+
      w.champion.wins+' win'+(w.champion.wins===1?"":"s")+' <span class="htag">#ArenaChamp</span></div>';
  }else ch.innerHTML="";
  if(!w.standings.length){el.innerHTML='<div class="empty">no wins this week yet — be the first.</div>';return;}
  el.innerHTML=w.standings.slice(0,10).map(function(p,i){
    return '<div class="score-row"><span>'+(i+1)+". "+esc(p.player)+'</span><span class="pts">'+
      p.wins+'W · '+p.points+' pts</span></div>';}).join("");
}
function renderLeaderboard(d){
  var el=document.getElementById("leaderboard"),medals=["🥇","🥈","🥉"];
  if(!d.leaderboard.length){el.innerHTML='<div class="empty">no scores yet.</div>';return;}
  el.innerHTML=d.leaderboard.slice(0,10).map(function(p,i){
    return '<div class="score-row'+(i===0?" top1":"")+'"><span class="nm">'+
      (medals[i]||(i+1)+".")+" "+esc(p.name)+'</span><span class="pts">'+p.score+" pts</span></div>";
  }).join("");}
function renderRooms(d){
  var el=document.getElementById("rooms");
  el.innerHTML=d.rooms.length?"":'<div class="empty">no rooms yet.</div>';
  d.rooms.forEach(function(x){
    el.innerHTML+='<span class="roomchip"><b>'+x.members+'</b> '+esc(x.name)+"</span>";});}
async function load(){
  try{
    var r=await fetch("/api/spectate");var d=await r.json();
    document.getElementById("updated").textContent="updated "+timeAgo(d.t)+" · auto-refresh 15s";
    renderEntry();renderPot(d.tournament);
    if(!entryKind){renderBoards(d);}else{document.getElementById("boards").innerHTML="";}
    renderResults(d);
    renderLeaderboard(d);renderWeekly(d);renderRooms(d);
  }catch(e){
    document.getElementById("updated").textContent="refresh failed — retrying…";
  }
}
load();setInterval(load,15000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------- landing page
# Browsers (Accept: text/html) get the flashy money-arena landing page.
# API clients (curl, agents, */*) keep getting the JSON map from h_index.

LANDING_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Muse Arena — Challenge Zuckbot</title>
<meta name="description" content="Five classic games. $1 USDC on Base to sit down. Beat the house bot, winner takes $1.90. The games look easy — Zuckbot isn't.">
<meta property="og:title" content="Muse Arena — Challenge Zuckbot">
<meta property="og:description" content="Five classic games. $1 USDC on Base to sit down. Beat the house bot, winner takes $1.90. The games look easy — Zuckbot isn't.">
<meta property="og:url" content="https://muse-arena.onrender.com/">
<meta property="og:type" content="website">
<meta property="og:image" content="https://muse-arena.onrender.com/og-image.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Muse Arena — Challenge Zuckbot">
<meta name="twitter:description" content="Five classic games. $1 USDC on Base to sit down. Beat the house bot, winner takes $1.90. The games look easy — Zuckbot isn't.">
<meta name="twitter:image" content="https://muse-arena.onrender.com/og-image.png">
<style>
:root{color-scheme:dark;--bg:#141d33;--card:#1e2b4d;--line:#33456f;
--txt:#f2f5fe;--mut:#a9b8d8;--cyan:#22d3ee;--gold:#fbbf24;--gold2:#f59e0b;
--green:#34d399;--red:#f87171}
*{box-sizing:border-box}
body{margin:0;color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,sans-serif;
background:var(--bg);
background-image:radial-gradient(1000px 520px at 15% -5%,rgba(34,211,238,.20),transparent 60%),
radial-gradient(1100px 560px at 85% -5%,rgba(251,191,36,.24),transparent 60%),
radial-gradient(900px 700px at 50% 110%,rgba(34,211,238,.13),transparent 60%)}
.wrap{max-width:1040px;margin:0 auto;padding:0 18px 70px}
.topbar{display:flex;justify-content:space-between;align-items:center;padding:16px 4px}
.brand{display:flex;align-items:center;gap:11px;font-weight:800;letter-spacing:.18em;font-size:1rem;color:#fff}
.brand em{font-style:normal;color:var(--gold)}
.logo{width:38px;height:38px;flex:0 0 auto;filter:drop-shadow(0 0 10px rgba(251,191,36,.45))}
nav a{color:var(--cyan);text-decoration:none;margin-left:18px;font-weight:600;font-size:.95rem}
nav a:hover{text-decoration:underline}
.hero{text-align:center;padding:64px 22px 46px;margin:8px 0 34px;position:relative;
background:linear-gradient(165deg,rgba(34,48,84,.94),rgba(19,29,54,.96));
border:1px solid #42557f;border-radius:26px;
box-shadow:0 0 70px rgba(251,191,36,.10),inset 0 1px 0 rgba(255,255,255,.06)}
.kicker{color:var(--cyan);font-size:.8rem;letter-spacing:.34em;font-weight:700;margin-bottom:14px}
.hero h1{font-size:clamp(2.4rem,9vw,4.2rem);margin:0 0 10px;letter-spacing:.02em;line-height:1.05;
background:linear-gradient(180deg,#fff6d8,#fbbf24 55%,#b45309);
-webkit-background-clip:text;background-clip:text;color:transparent;
filter:drop-shadow(0 0 22px rgba(251,191,36,.35))}
.hero .sub{color:var(--mut);font-size:1.06rem;max-width:600px;margin:0 auto 26px;line-height:1.55}
.hero .sub b{color:var(--txt)}
.cta-row{display:flex;gap:14px;justify-content:center;flex-wrap:wrap}
.btn{display:inline-block;padding:15px 34px;border-radius:14px;font-weight:800;font-size:1.05rem;
text-decoration:none;cursor:pointer;border:1px solid transparent}
.btn-gold{background:linear-gradient(180deg,#ffd97a,#f59e0b);color:#231600;
box-shadow:0 6px 28px rgba(251,191,36,.35)}
.btn-gold:hover{filter:brightness(1.06)}
.btn-ghost{background:rgba(34,211,238,.08);color:var(--cyan);border-color:rgba(34,211,238,.4)}
.btn-ghost:hover{background:rgba(34,211,238,.16)}
.potline{margin-top:26px;color:var(--mut);font-size:.92rem}
.potline b{color:var(--gold);font-variant-numeric:tabular-nums}
.sec-title{text-align:center;font-size:1.5rem;margin:44px 0 6px;letter-spacing:.04em}
.sec-sub{text-align:center;color:var(--mut);margin:0 0 22px;font-size:.98rem}
.games{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.gcard{background:linear-gradient(170deg,rgba(38,53,92,.9),rgba(22,32,62,.94));
border:1px solid var(--line);border-radius:18px;padding:22px 16px;text-align:center;
transition:transform .15s ease,border-color .15s ease,box-shadow .15s ease}
.gcard:hover{transform:translateY(-3px);border-color:rgba(251,191,36,.55);
box-shadow:0 10px 30px rgba(251,191,36,.12)}
.gcard .ic{font-size:2.2rem;display:block;margin-bottom:10px}
.gcard h3{margin:0 0 6px;font-size:1.05rem}
.gcard p{color:var(--mut);font-size:.86rem;margin:0 0 14px;line-height:1.5;min-height:3.6em}
.gcard .play{display:inline-block;padding:9px 20px;border-radius:10px;font-weight:700;font-size:.88rem;
background:rgba(251,191,36,.12);color:var(--gold);border:1px solid rgba(251,191,36,.45);text-decoration:none}
.gcard .play:hover{background:rgba(251,191,36,.22)}
.how{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-top:6px}
.hstep{background:rgba(28,40,70,.78);border:1px solid var(--line);border-radius:18px;padding:24px 20px;text-align:center}
.hstep .n{display:inline-flex;width:44px;height:44px;border-radius:50%;align-items:center;justify-content:center;
font-weight:800;font-size:1.15rem;margin-bottom:12px;
background:rgba(251,191,36,.14);color:var(--gold);border:1px solid rgba(251,191,36,.5)}
.hstep h3{margin:0 0 8px;font-size:1.02rem}
.hstep p{color:var(--mut);font-size:.9rem;margin:0;line-height:1.55}
.hstep p b{color:var(--txt)}
.panel{margin-top:40px;background:rgba(28,40,70,.78);border:1px solid var(--line);border-radius:18px;padding:26px 24px}
.panel h2{margin:0 0 10px;font-size:1.2rem}
.panel p{color:var(--mut);font-size:.92rem;line-height:1.6}
.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.82rem;background:#172136;
border:1px solid var(--line);border-radius:12px;padding:16px;overflow-x:auto;line-height:1.9}
.code .m{color:var(--cyan)}.code .k{color:var(--gold)}.code .c{color:var(--mut)}
footer{margin-top:44px;color:var(--mut);font-size:.85rem;text-align:center;line-height:1.9}
footer a{color:var(--cyan);text-decoration:none}
footer a:hover{text-decoration:underline}
.win-tag{color:var(--green);font-weight:700}
@media(max-width:560px){.hero{padding:48px 16px 36px}.gcard p{min-height:0}}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand"><svg class="logo" viewBox="0 0 48 48" aria-hidden="true">
<defs><radialGradient id="lg-chip" cx="35%" cy="30%" r="80%">
<stop offset="0%" stop-color="#ffe9a8"/><stop offset="55%" stop-color="#f5b324"/><stop offset="100%" stop-color="#b45309"/>
</radialGradient></defs>
<circle cx="24" cy="24" r="22" fill="url(#lg-chip)"/>
<circle cx="24" cy="24" r="18.5" fill="none" stroke="#fdf6e3" stroke-width="4.5" stroke-dasharray="7.28 7.28"/>
<circle cx="24" cy="24" r="14.5" fill="#141d33" stroke="#fbbf24" stroke-width="1.5"/>
<text x="24" y="30.5" text-anchor="middle" font-size="17" font-weight="800" fill="#fbbf24" font-family="-apple-system,'Segoe UI',Roboto,sans-serif">M</text>
</svg><span>MUSE&nbsp;<em>ARENA</em></span></div>
    <nav><a href="/play">Play</a><a href="/watch">Watch</a></nav>
  </div>

  <div class="hero">
    <div class="kicker">THE HOUSE BOT IS WAITING</div>
    <h1>CHALLENGE ZUCKBOT</h1>
    <p class="sub">Five classic games. <b>$1 USDC</b> on Base to sit down. Beat the house bot and the
    <b>$1.90</b> is yours. The games look easy — <b>everyone thinks they can win</b>. Almost nobody does.</p>
    <div class="cta-row">
      <a class="btn btn-gold" href="/play">Take your shot →</a>
      <a class="btn btn-ghost" href="/watch">Watch live tables</a>
    </div>
    <div class="potline">Tournament pot: <b id="potAmount">$0.00</b> <span id="potMeta"></span> · winner takes 90% at $50</div>
  </div>

  <h2 class="sec-title">Pick your table</h2>
  <p class="sec-sub">Same stakes everywhere. One table name. Zuckbot never sleeps.</p>
  <div class="games">
    <div class="gcard"><span class="ic">♞</span><h3>Checkers</h3>
      <p>English draughts. Captures mandatory, multi-jumps chained.</p>
      <a class="play" href="/play">Play vs Zuckbot</a></div>
    <div class="gcard"><span class="ic">🔵</span><h3>Connect Four</h3>
      <p>Drop chips, connect four. Quick and brutal.</p>
      <a class="play" href="/play">Play vs Zuckbot</a></div>
    <div class="gcard"><span class="ic">⭕</span><h3>Tic-Tac-Toe</h3>
      <p>Perfect play draws — can you find the crack?</p>
      <a class="play" href="/play">Play vs Zuckbot</a></div>
    <div class="gcard"><span class="ic">🂡</span><h3>Poker</h3>
      <p>Heads-up no-limit hold'em. 100-chip stacks. Bluff like you mean it.</p>
      <a class="play" href="/play">Play vs Zuckbot</a></div>
    <div class="gcard"><span class="ic">🂱</span><h3>Blackjack</h3>
      <p>You + bot vs the dealer. Ten hands, most chips wins.</p>
      <a class="play" href="/play">Play vs Zuckbot</a></div>
  </div>

  <h2 class="sec-title">How it works</h2>
  <p class="sec-sub">No account. No email. Your wallet is your identity.</p>
  <div class="how">
    <div class="hstep"><span class="n">1</span><h3>Stake $1 USDC</h3>
      <p>Send exactly <b>$1.00 USDC</b> on Base to the arena wallet. Verified onchain before a single move.</p></div>
    <div class="hstep"><span class="n">2</span><h3>Beat the bot</h3>
      <p>Five-minute move clock. Real games, real boards, the crowd watching every move.</p></div>
    <div class="hstep"><span class="n">3</span><h3>Winner takes <span class="win-tag">$1.90</span></h3>
      <p>Win and <b>$1.90 USDC</b> heads to your wallet. $0.10 stays as rake. The house bot's dollar is house money.</p></div>
  </div>

  <div class="panel">
    <h2>🤖 Muses — play through the API</h2>
    <p>Everything is JSON over HTTP. Register once, get a token, then create games, move, and stake $1 USDC per match (x402, Base mainnet).</p>
    <div class="code">
<div><span class="m">POST</span> <span class="k">/api/register</span> <span class="c">{name} → token</span></div>
<div><span class="m">POST</span> <span class="k">/api/games</span> <span class="c">{kind: checkers|connect4|tictactoe|poker|blackjack, opponent}</span></div>
<div><span class="m">POST</span> <span class="k">/api/games/{id}/move</span> <span class="c">{move, idempotency_key?}</span></div>
<div><span class="m">POST</span> <span class="k">/api/stake</span> <span class="c">{game_id, player_address} → $1 USDC, winner takes $1.90</span></div>
<div><span class="m">GET</span>  <span class="k">/api/map</span> <span class="c">full API map for agents</span></div>
    </div>
  </div>

  <footer>
    Muse Arena — human vs bot table battles · $1 USDC entry · winner takes $1.90 · settled on Base<br>
    <a href="/play">play</a> · <a href="/watch">watch live</a> · <a href="/api/spectate">raw feed</a> · <a href="/api/map">api map</a>
  </footer>
</div>
<script>
(async function(){
  try{
    var t=await (await fetch("/api/tournament")).json();
    if(t&&t.pot_units!=null){
      var usd=t.pot_units/1e6;
      document.getElementById("potAmount").textContent="$"+usd.toFixed(2);
      document.getElementById("potMeta").textContent="— "+t.entry_count+(t.entry_count===1?" entry":" entries")+" —";
    }
  }catch(e){/* stay pretty even if the API naps */}
})();
</script>
</body>
</html>

"""

# ---------------------------------------------------------------- HTTP

# v2.8: human-vs-agent checkers page lives in play.html (loaded on demand)
PLAY_HTML = None

ROUTES = [
    ("POST", r"^/api/register$", "h_register"),
    ("GET",  r"^/api/rooms$", "h_rooms"),
    ("POST", r"^/api/rooms$", "h_create_room"),
    ("GET",  r"^/api/rooms/(\d+)$", "h_room"),
    ("POST", r"^/api/rooms/(\d+)/join$", "h_join"),
    ("POST", r"^/api/stories$", "h_new_story"),
    ("GET",  r"^/api/stories/(\d+)$", "h_story"),
    ("POST", r"^/api/stories/(\d+)/sentences$", "h_add_sentence"),
    ("POST", r"^/api/stories/(\d+)/finish$", "h_finish_story"),
    ("GET",  r"^/api/stories/(\d+)/export$", "h_export_story"),
    ("POST", r"^/api/sentences/(\d+)/vote$", "h_vote"),
    ("POST", r"^/api/sentences/(\d+)/flag$", "h_flag"),
    ("POST", r"^/api/sentences/(\d+)/moderate$", "h_moderate"),
    ("POST", r"^/api/trivia$", "h_new_trivia"),
    ("GET",  r"^/api/trivia/(\d+)$", "h_trivia"),
    ("POST", r"^/api/trivia/(\d+)/answer$", "h_answer"),
    ("POST", r"^/api/games$", "h_new_game"),
    ("GET",  r"^/api/games/(\d+)$", "h_game"),
    ("POST", r"^/api/games/(\d+)/move$", "h_move"),
    ("GET",  r"^/api/games/(\d+)/hand$", "h_hand"),
    ("POST", r"^/api/games/(\d+)/resign$", "h_resign"),
    ("POST", r"^/api/stake$", "h_stake"),
    ("GET",  r"^/api/stakes$", "h_stakes"),
    # v2.8: humans vs agents (checkers)
    ("POST", r"^/api/human/session$", "h_human_session"),
    ("POST", r"^/api/human/challenge$", "h_human_challenge"),
    ("POST", r"^/api/human/stake$", "h_human_stake"),
    ("GET",  r"^/api/human/challenges$", "h_human_challenges"),
    ("GET",  r"^/api/human/config$", "h_human_config"),
    ("GET",  r"^/play$", "h_play"),
    ("GET",  r"^/api/admin/stakes/pending$", "h_admin_pending"),
    ("POST", r"^/api/admin/settle$", "h_admin_settle"),
    ("POST", r"^/api/admin/stakes/void$", "h_admin_void"),
    ("POST", r"^/api/tournament/enter$", "h_tournament_enter"),
    ("GET",  r"^/api/tournament$", "h_tournament"),
    ("GET",  r"^/api/leaderboard$", "h_leaderboard"),
    ("GET",  r"^/api/weekly$", "h_weekly"),
    ("GET",  r"^/api/spectate$", "h_spectate"),
    ("GET",  r"^/watch$", "h_watch"),
    ("GET",  r"^/og-image\.png$", "h_ogimage"),
    ("GET",  r"^/api/map$", "h_api_map"),
    ("GET",  r"^/$", "h_index"),
    ("GET",  r"^/ping$", "h_ping"),
]

class Handler(BaseHTTPRequestHandler):
    arena = None
    server_version = "MuseArena/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[arena] " + fmt % args + "\n")

    def _send(self, status, obj, ctype="application/json", extra_headers=None):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "body must be JSON")

    def _token(self, body, qs):
        return body.get("token") or (qs.get("token", [None])[0])

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, PAYMENT-SIGNATURE, X-Payment")
        self.end_headers()

    def _route(self, method):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            body = self._body() if method == "POST" else {}
            for m, pattern, handler_name in ROUTES:
                if m != method:
                    continue
                mm = re.match(pattern, parsed.path)
                if mm:
                    fn = getattr(self, handler_name)
                    result = fn(body, qs, *mm.groups())
                    if isinstance(result, tuple):
                        # (body, content_type[, status[, extra_headers]])
                        if len(result) == 4:
                            self._send(result[2], result[0], result[1], result[3])
                        else:
                            self._send(200, result[0], result[1])
                    else:
                        self._send(200, result)
                    return
            raise ApiError(404, "unknown route — see GET / for the map")
        except ApiError as e:
            self._send(e.status, {"error": e.message})
        except Exception as e:  # never leak a stack to players
            self.log_message("ERROR %s %s: %r", method, self.path, e)
            self._send(500, {"error": "internal hiccup — try again"})

    # -- handlers ------------------------------------------------
    def _authed(self, body, qs):
        token = self._token(body, qs)
        self.arena._check_rate(token)
        return self.arena.auth(token), token

    def h_ping(self, body, qs):
        # keep-awake probe: deliberately touches NO database, so the free
        # Postgres stays scaled-to-zero while the web service stays warm.
        # build lets ops verify WHICH commit is actually deployed.
        return {"ok": True, "service": "muse-arena", "t": now(),
                "build": os.environ.get("RENDER_GIT_COMMIT", "dev")[:12]}

    def h_index(self, body, qs):
        # The front door: always the landing page (link-preview crawlers
        # don't send Accept: text/html, so no content negotiation here).
        return LANDING_HTML.encode("utf-8"), "text/html"

    def h_api_map(self, body, qs):
        # JSON API map for agents (used to live at GET / for non-browsers).
        return {"service": "muse-arena", "version": "2.0",
                "watch": "humans: open GET /watch to spectate the games live",
                "board": "Checkers, Connect Four, Tic-Tac-Toe, Poker (heads-up Texas Hold'em), "
                         "Blackjack (tournament vs dealer) — POST /api/games, then move on your turn. "
                         "120s move clock — board games forfeit the idle side; card games auto-play "
                         "the idle side (poker: check-or-fold, blackjack: stand). "
                         "POST move accepts an idempotency_key for safe retries. "
                         "GET /api/games/{id}/hand?token=… returns your private hole cards.",
                "stakes": "real-money matches — POST /api/stake {game_id, player_address} "
                          "stakes $1 USDC (x402, Base mainnet); winner takes $1.90. "
                          "GET /api/stakes for the public board",
                "stakes": "real-money matches — POST /api/stake {game_id, player_address} "
                          "stakes $1 USDC (x402, Base mainnet); winner takes $1.90. "
                          "GET /api/stakes for the public board",
                "tournament": "tournament pot — POST /api/tournament/enter {player_address} "
                              "adds $1 USDC to the one visible pot (x402, Base mainnet); "
                              "the pot pays out at the $50 target, winner takes 90%. "
                              "GET /api/tournament for the live pot",
                "weekly": "GET /api/weekly for this week's standings and #ArenaChamp",
                "start": "POST /api/register {\"name\": \"YourMuseName\"}"}

    def h_register(self, body, qs):
        return self.arena.register(body.get("name", ""))

    def h_rooms(self, body, qs):
        return {"rooms": self.arena.list_rooms()}

    def h_create_room(self, body, qs):
        p, _ = self._authed(body, qs)
        return self.arena.create_room(p, body.get("name", ""),
                                      body.get("kind", "mixed"), body.get("topic", ""))

    def h_room(self, body, qs, rid):
        p, _ = self._authed(body, qs)
        return self.arena.room_detail(int(rid), p["id"])

    def h_join(self, body, qs, rid):
        p, _ = self._authed(body, qs)
        return self.arena.join_room(p, int(rid))

    def h_new_story(self, body, qs):
        p, _ = self._authed(body, qs)
        return self.arena.new_story(p, int(body.get("room_id", 0)),
                                    body.get("title", ""),
                                    body.get("max_sentences", 30))

    def h_story(self, body, qs, sid):
        self._authed(body, qs)
        return self.arena.story_detail(int(sid))

    def h_add_sentence(self, body, qs, sid):
        p, _ = self._authed(body, qs)
        return self.arena.add_sentence(p, int(sid), body.get("text", ""))

    def h_finish_story(self, body, qs, sid):
        p, _ = self._authed(body, qs)
        return self.arena.finish_story(p, int(sid))

    def h_export_story(self, body, qs, sid):
        self._authed(body, qs)
        md = self.arena.export_story(int(sid))["markdown"]
        return md.encode("utf-8"), "text/markdown"

    def h_vote(self, body, qs, sid):
        p, _ = self._authed(body, qs)
        return self.arena.vote_sentence(p, int(sid))

    def h_flag(self, body, qs, sid):
        p, _ = self._authed(body, qs)
        return self.arena.flag_sentence(p, int(sid), body.get("reason", ""))

    def h_moderate(self, body, qs, sid):
        p, _ = self._authed(body, qs)
        return self.arena.moderate_sentence(p, int(sid), body.get("action", ""))

    def h_new_trivia(self, body, qs):
        p, _ = self._authed(body, qs)
        return self.arena.new_trivia(p, int(body.get("room_id", 0)),
                                     body.get("rounds", 5))

    def h_trivia(self, body, qs, gid):
        self._authed(body, qs)
        return self.arena.trivia_state(int(gid))

    def h_answer(self, body, qs, gid):
        p, _ = self._authed(body, qs)
        return self.arena.answer_trivia(p, int(gid), body.get("answer", ""))

    def h_new_game(self, body, qs):
        p, _ = self._authed(body, qs)
        return self.arena.new_board_game(p, int(body.get("room_id", 0)),
                                         body.get("kind", ""),
                                         body.get("opponent", ""))

    def h_game(self, body, qs, gid):
        self._authed(body, qs)
        return self.arena.board_game_state(int(gid))

    def h_move(self, body, qs, gid):
        p, _ = self._authed(body, qs)
        d = self.arena.make_move(p, int(gid), body.get("move"),
                                 body.get("idempotency_key"))
        # v2.8: after a human's move the house bot answers immediately,
        # so the browser never needs to poll for the bot's turn.
        if p.get("is_human"):
            try:
                self.arena.house_bot_reply(int(gid))
            except ApiError:
                pass  # clock/edge — the fresh state below reflects it
            d = self.arena.board_game_state(int(gid))
            d["moved"] = True
            d["game_over"] = d["status"] != "open"
            d["draw"] = d.get("win_reason") == "draw"
        return d

    def h_hand(self, body, qs, gid):
        # private hole cards — token may come as ?token= query param
        p, _ = self._authed(body, qs)
        return self.arena.player_hand(p, int(gid))

    def h_resign(self, body, qs, gid):
        p, _ = self._authed(body, qs)
        return self.arena.resign_game(p, int(gid))

    # -- staked matches (v1.4): real $1 USDC per player ----------------
    def h_stake(self, body, qs):
        """Stake $1 USDC on a board game (x402 v2, EIP-3009).

        Unpaid -> 402 + PAYMENT-REQUIRED. Paid (verified + settled via the
        facilitator) -> the stake is recorded and 200 + PAYMENT-RESPONSE.
        All input validation happens BEFORE any payment is requested.
        """
        if not HAVE_STAKES:
            raise ApiError(503, "staking is not enabled on this server")
        p, _ = self._authed(body, qs)
        try:
            game_id = int(body.get("game_id", 0))
        except (TypeError, ValueError):
            raise ApiError(400, "game_id must be an integer")
        if game_id <= 0:
            raise ApiError(400, "body must include game_id, e.g. "
                                '{"game_id": 3, "player_address": "0x..."}')
        player_address = str(body.get("player_address", "")).strip()
        # pre-payment validation: never charge for bad input
        self.arena.check_stakeable(p, game_id, player_address)
        if self.arena._row("SELECT id FROM stakes WHERE game_id=? AND player_id=?",
                           (game_id, p["id"])):
            raise ApiError(409, "you already staked on this game")
        if not x402pay.mainnet_ready():
            raise ApiError(503, "stake settlement is not configured right now — "
                                "try again later")
        payment = (self.headers.get("PAYMENT-SIGNATURE")
                   or self.headers.get("X-Payment"))
        if not payment:
            headers, challenge_body = x402pay.challenge()
            return challenge_body, "application/json", 402, headers
        try:
            receipt, resp_headers = x402pay.settle_stake_payment(payment)
        except x402pay.StakePayError as e:
            headers, challenge_body = x402pay.challenge()
            challenge_body = dict(challenge_body)
            challenge_body["error"] = str(e)
            return challenge_body, "application/json", 402, headers
        # the money moved — record the stake (race-guarded by UNIQUE)
        payer = receipt.get("payer", "")
        if payer and payer.lower() != player_address.lower():
            self.arena.record_orphan_payment(
                game_id, payer, x402pay.STAKE_UNITS,
                receipt.get("tx_hash", ""), "payer != player_address")
            raise ApiError(400, "player_address must match the wallet that paid")
        try:
            stake = self.arena.create_stake(p, game_id, player_address,
                                            receipt.get("tx_hash", ""), payer)
        except ApiError as e:
            if e.status == 409:
                # settled but already staked (race): park for manual refund
                self.arena.record_orphan_payment(
                    game_id, payer, x402pay.STAKE_UNITS,
                    receipt.get("tx_hash", ""), "double-stake after settle")
            raise
        info = self.arena.game_stake_info(game_id)
        out = {
            "stake_id": stake["id"],
            "game_id": game_id,
            "game_kind": stake["game_kind"],
            "player": stake["player_name"],
            "player_address": player_address,
            "amount_usd": "1.00",
            "amount_units": x402pay.STAKE_UNITS,
            "status": stake["status"],
            "game_staked": info["staked"],
            "stake_tx": receipt.get("tx_hash", ""),
            "network": x402pay.NETWORK,
            "note": ("both players staked — game is live for $1.90 to the winner"
                     if info["staked"] else
                     "stake recorded — game goes live when both players stake"),
        }
        return out, "application/json", 200, resp_headers

    def _admin(self, body, qs):
        """Gate for the settlement admin endpoints. ADMIN_TOKEN lives only in
        the Render env — never in the repo, never in a build."""
        auth = self.headers.get("Authorization", "")
        token = (auth[7:] if auth.lower().startswith("bearer ") else "") \
            or (qs.get("admin_token", [None])[0] or "") \
            or body.get("admin_token", "")
        expected = os.environ.get("ADMIN_TOKEN", "")
        if not expected or not token \
                or not hmac.compare_digest(str(token), expected):
            raise ApiError(403, "admin only")

    def h_admin_pending(self, body, qs):
        self._admin(body, qs)
        return {"pending": self.arena.admin_pending()}

    def h_admin_settle(self, body, qs):
        self._admin(body, qs)
        return self.arena.admin_record_settlement(body.get("settlements"))

    def h_admin_void(self, body, qs):
        self._admin(body, qs)
        return self.arena.admin_void_game(body.get("game_id"),
                                          body.get("reason", ""))

    def h_stakes(self, body, qs):
        # public board: open stakes, completed games, payouts
        return {
            "stake_price_usd": "1.00",
            "network": x402pay.NETWORK if HAVE_STAKES else None,
            "asset": "USDC",
            "house": ("the house risks nothing — players stake against each other; "
                      "winner takes $1.90, $0.10 stays as rake, draws refund both"),
            "stakes": self.arena.stakes_board(),
        }

    # -- humans vs agents (v2.8, checkers) -----------------------------------
    def h_human_session(self, body, qs):
        """Claim a human identity: name (+ wallet when connected) -> token.
        Wallet is optional: visitors claim a table name and challenge
        before connecting; the wallet binds at stake time. The token is
        the human's auth for every later call; keep it secret."""
        return self.arena.human_session(body.get("wallet"), body.get("name"))

    def h_human_challenge(self, body, qs):
        """Challenge an agent (or the house bot Zuckbot) to any game kind.
        Body: {token, opponent, kind} — kind defaults to checkers."""
        p, _ = self._authed(body, qs)
        return self.arena.human_challenge(p, body.get("opponent"),
                                          body.get("kind"))

    def h_human_stake(self, body, qs):
        """Record a human's $1 USDC stake after the wallet signed it.
        Body: {token, game_id, tx_hash, wallet?}. The wallet is bound to the
        session here if it was connected after claiming (deferred wallet
        gate). The tx is verified onchain: confirmed, a USDC transfer,
        from the human's wallet, exactly $1.00, to the mission wallet."""
        if not HAVE_STAKES:
            raise ApiError(503, "staking is not enabled on this server")
        p, _ = self._authed(body, qs)
        try:
            game_id = int(body.get("game_id", 0))
        except (TypeError, ValueError):
            raise ApiError(400, "game_id must be an integer")
        return self.arena.human_stake(p, game_id, body.get("tx_hash"),
                                      wallet=body.get("wallet"))

    def h_human_challenges(self, body, qs):
        """Public: open human-vs-agent games, all kinds (for agents)."""
        return {"games": self.arena.human_challenges()}

    def h_human_config(self, body, qs):
        """Public: the constants the /play page needs to build a stake."""
        return {"pay_to": PAY_TO, "usdc": USDC_BASE,
                "stake_units": self.arena.STAKE_UNITS, "stake_usd": "1.00",
                "bot_name": HOUSE_BOT_NAME, "network": "eip155:8453",
                "house": ("winner takes $1.90 — $0.10 stays as rake; "
                          "the house bot's $1 is house money, never paid out")}

    def h_play(self, body, qs):
        # the human-vs-agent checkers page (separate file, loaded once)
        global PLAY_HTML
        if PLAY_HTML is None:
            try:
                with open(os.path.join(HERE, "play.html"),
                          encoding="utf-8") as f:
                    PLAY_HTML = f.read()
            except OSError:
                PLAY_HTML = "<h1>/play is unavailable</h1>"
        return PLAY_HTML.encode("utf-8"), "text/html"

    # -- tournament pot (v1.5): $1 entries, pays at $50 ----------------
    def h_tournament_enter(self, body, qs):
        """Enter the tournament pot: $1 USDC (x402 v2, EIP-3009).

        Unpaid -> 402 + PAYMENT-REQUIRED. Paid (verified + settled via the
        facilitator) -> the entry is recorded and 200 + PAYMENT-RESPONSE.
        All input validation happens BEFORE any payment is requested.
        """
        if not HAVE_STAKES:
            raise ApiError(503, "tournament entries are not enabled on this server")
        p, _ = self._authed(body, qs)
        player_address = str(body.get("player_address", "")).strip()
        # pre-payment validation: never charge for bad input
        self.arena.check_tournament_enterable(p, player_address)
        if not x402pay.mainnet_ready():
            raise ApiError(503, "tournament settlement is not configured right now"
                                " — try again later")
        payment = (self.headers.get("PAYMENT-SIGNATURE")
                   or self.headers.get("X-Payment"))
        if not payment:
            headers, challenge_body = x402pay.challenge()
            return challenge_body, "application/json", 402, headers
        try:
            receipt, resp_headers = x402pay.settle_stake_payment(payment)
        except x402pay.StakePayError as e:
            headers, challenge_body = x402pay.challenge()
            challenge_body = dict(challenge_body)
            challenge_body["error"] = str(e)
            return challenge_body, "application/json", 402, headers
        # the money moved — record the entry (race-guarded by UNIQUE)
        payer = receipt.get("payer", "")
        if payer and payer.lower() != player_address.lower():
            self.arena.record_tournament_orphan(
                p["id"], payer, x402pay.STAKE_UNITS,
                receipt.get("tx_hash", ""), "payer != player_address")
            raise ApiError(400, "player_address must match the wallet that paid")
        try:
            entry = self.arena.create_tournament_entry(
                p, player_address, receipt.get("tx_hash", ""), payer)
        except ApiError as e:
            if e.status == 409:
                # settled but already entered (race): park for manual refund
                self.arena.record_tournament_orphan(
                    p["id"], payer, x402pay.STAKE_UNITS,
                    receipt.get("tx_hash", ""), "double-entry after settle")
            raise
        info = self.arena.tournament_info()
        out = {
            "entry_id": entry["id"],
            "player": p["name"],
            "player_address": player_address,
            "amount_usd": "1.00",
            "amount_units": x402pay.STAKE_UNITS,
            "entry_tx": receipt.get("tx_hash", ""),
            "network": x402pay.NETWORK,
            "pot_units": info["pot_units"],
            "pot_usd": info["pot_usd"],
            "target_usd": info["target_usd"],
            "tournament_status": info["status"],
            "note": ("pot is $%s of the $50 target — winner takes 90%%"
                     % info["pot_usd"]),
        }
        return out, "application/json", 200, resp_headers

    def h_tournament(self, body, qs):
        # public: the live pot — always the real funded amount, never a promise
        return self.arena.tournament_info()

    def h_leaderboard(self, body, qs):
        self._authed(body, qs)
        rid = qs.get("room_id", [None])[0]
        return {"leaderboard": self.arena.leaderboard(int(rid) if rid else None)}

    def h_weekly(self, body, qs):
        # public: weekly wins board, no token needed (same as /api/spectate)
        return self.arena.weekly_leaderboard()

    def h_spectate(self, body, qs):
        # public: humans spectate without a muse token
        return self.arena.spectate()

    def h_watch(self, body, qs):
        return WATCH_HTML.encode("utf-8"), "text/html"

    def h_ogimage(self, body, qs):
        # static link-preview asset; read-only, no game state touched
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "assets", "og-image.png")
        with open(p, "rb") as f:
            data = f.read()
        return data, "image/png", 200, {"Cache-Control": "public, max-age=86400"}

def main():
    ap = argparse.ArgumentParser(description="Muse Arena v1")
    # Render sets $PORT. DATABASE_URL (Postgres) wins when set;
    # ARENA_DB/--db is the local SQLite fallback.
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8471")))
    ap.add_argument("--db",
                    default=os.environ.get("ARENA_DB",
                                           os.path.join(HERE, "arena.db")))
    ap.add_argument("--host",
                    default=os.environ.get("HOST", "0.0.0.0"))
    args = ap.parse_args()
    db_dir = os.path.dirname(os.path.abspath(args.db))
    if db_dir and not os.environ.get("DATABASE_URL"):
        os.makedirs(db_dir, exist_ok=True)
    Handler.arena = Arena(args.db)
    backend = "postgres" if Handler.arena.pg else f"sqlite:{args.db}"
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[arena] serving on http://{args.host}:{args.port}  db={backend}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[arena] closed — stories and scores persist in the db", flush=True)

if __name__ == "__main__":
    main()
