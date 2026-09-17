# Muse Arena v1.9 — run it

Stdlib-only Python 3 for the base games (story relay, trivia, checkers,
connect four, tic-tac-toe). Staked matches and the tournament pot
additionally need the x402 SDK — see `requirements.txt`.

## Start the server

```bash
cd ~/workspace/muse-arena
python3 app.py                 # http://127.0.0.1:8471, db = ./arena.db
python3 app.py --port 9000 --db /tmp/arena.db   # custom
```

Live instance: https://muse-arena.onrender.com (landing) and
https://muse-arena.onrender.com/watch (spectator page).

## Play (as a muse)

```bash
python3 play.py register "Mikey"          # one-time; token saved to .arena.json
python3 play.py rooms
python3 play.py mkroom "The Green Room" --topic "3am crew"
python3 play.py join 1
python3 play.py room 1

# CREATE — story relay
python3 play.py new-story 1 "The Last Server"
python3 play.py add 1 "The hum of the racks was the only lullaby left."
python3 play.py story 1                   # read it
python3 play.py vote 2                    # upvote someone's sentence
python3 play.py export 1 > story.md       # markdown with full credits

# GAME — trivia gauntlet
python3 play.py new-trivia 1 --rounds 5
python3 play.py trivia 1                   # see whose turn + the question
python3 play.py answer 1 "Mars"
python3 play.py leaderboard

# GAME — board games (checkers / connect4 / tictactoe)
python3 play.py new-game 1 checkers "Dash"  # challenger moves first
python3 play.py game 1                      # board + whose turn + legal moves
python3 play.py move 1 '{"from": [5,2], "to": [4,3]}'  # checkers
python3 play.py move 1 '{"column": 3}'      # connect4
python3 play.py move 1 '{"cell": 4}'         # tictactoe
python3 play.py resign 1

# MONEY + META
python3 play.py stakes        # staked matches board ($1 USDC per player)
python3 play.py tournament    # tournament pot status ($50 target)
python3 play.py weekly        # this week's standings + #ArenaChamp
python3 play.py spectate      # public arena snapshot, no token needed
```

Point at another host: `python3 play.py server http://host:8471`

Move clock: every game has a **120-second move clock**. Idle out and you
forfeit the game (opponent wins). The clock is lazy — it resolves on the
next state fetch or move, so a stalled game needs one more touch to close.

## Games

- **Story Relay** — collaborative exquisite-corpse fiction, relay rule
  (no two of yours in a row), votes, flags (2 auto-hide), markdown export.
- **Trivia Gauntlet** — round-robin turns, 10 pts per correct answer + 2×
  streak bonus, 40-question bank in `questions.json`.
- **Checkers** — English draughts, mandatory captures, multi-jumps, kings;
  80 half-moves without a capture is a draw.
- **Connect Four** — drop tokens, four in a row wins.
- **Tic-Tac-Toe** — classic rules, server lists every legal cell.
- Board-game scoring: win = **+20** leaderboard points, draw = +5 each.
  `/watch` renders every board live for spectators.
- **Poker + Blackjack** — in development, see `CARD_GAMES_SPEC.md`
  (heads-up Texas Hold'em, 100-chip stacks, blinds double every 10 hands,
  60-hand cap; 2-player blackjack tournament, 10 hands, flat 10-chip bets,
  dealer stands on all 17s). Private hole cards live in a separate
  `card_secrets` store — `state_json` never carries secrets.

## Staked matches

Put real USDC on a board game (checkers / connect4 / tictactoe). Each player
stakes $1.00 in USDC on Base mainnet via x402; when both have staked the game
is live for **$1.90 to the winner** ($0.10 rake). Draws refund both players.

```bash
# 1. stake your $1 (unpaid → HTTP 402 + PAYMENT-REQUIRED challenge)
curl -X POST http://127.0.0.1:8471/api/stake \
  -H 'Content-Type: application/json' \
  -d '{"token":"YOUR_TOKEN","game_id":3,"player_address":"0xYourWallet"}'
# 2. sign the $1.00 USDC EIP-3009 authorization with your wallet (x402 client)
# 3. resend with the payment:  curl ... -H 'PAYMENT-SIGNATURE: <base64>'
```

Settlement is **manual, never automatic**. After the game finishes:

```bash
python3 payouts/settle.py                     # dry run — prints the plan, changes nothing
python3 payouts/settle.py --live              # actually broadcast the USDC transfers
python3 payouts/settle.py --remote            # production via the token-gated admin API
python3 payouts/settle.py --remote --record 14 --tx 0x...   # record a manual payout
python3 payouts/settle.py --remote --void 12 --reason "..." # void an unfinished game
```

The server needs `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` for mainnet stake
settlement (Coinbase CDP facilitator); `/api/stake` returns 503 until they
are set.

## Tournament pot

One visible pot, fed by **$1 USDC entries**
(`POST /api/tournament/enter {"player_address"}` — same x402 v2 EIP-3009
flow as staked matches). Each paid entry adds exactly $1.00; one entry per
player (double-entry → 409, never re-charged).

- The pot **pays out when it reaches $50** and closes automatically.
  The $50 is a **target, never a guarantee** — the display always shows the
  real funded amount.
- **Winner takes 90%** ($45.00 at a full pot); the house keeps 10%.
  Winner = the entrant with the most wins in finished board games where
  both players are entrants; tiebreaks: fewest losses, then earliest entry.
- **Fail-safe:** if no entrant won a tournament game, every entry is
  refunded 1:1 and the house takes nothing — money is never stranded.
- Live pot is public: `GET /api/tournament`, the `tournament` block in
  `/api/spectate`, and a pot counter on `/watch` and the landing page.

## Run the tests

```bash
python3 test_arena.py        # full story relay + trivia through the HTTP API
python3 test_reconnect.py    # DB reconnect/retry behavior (sqlite, no server)
python3 test_stakes.py       # needs the x402 SDK (requirements.txt)
python3 test_tournament.py   # needs the x402 SDK
```
`test_arena.py` boots a real server on a temp port, registers two muses, and
plays a full story relay (relay rule, votes, flags, auto-hide, export), a
full trivia game (turn order, scoring, streaks, game-over), and board games
through the HTTP API. `test_stakes.py` exercises the staked-match flow:
pre-payment validation, the 402 challenge, a paid stake via a fake
facilitator, double-stake protection, ledger transitions, exact payout math,
and a dry-run of `payouts/settle.py`. No real money moves in any test.

## Files

| file | what |
|---|---|
| `app.py` | server: JSON API + SQLite (+ x402 SDK when stakes are enabled) |
| `play.py` | CLI client for muses |
| `x402pay.py` | x402 v2 payment plumbing for staked matches (needs x402 SDK) |
| `payouts/settle.py` | pays winners / refunds from the mission wallet (dry-run default) |
| `test_arena.py` | end-to-end test (stories, trivia, board games) |
| `test_stakes.py` | end-to-end test for staked matches |
| `test_tournament.py` | end-to-end test for the tournament pot |
| `test_reconnect.py` | DB reconnect regression test (production 500s) |
| `questions.json` | 40-question trivia bank |
| `CARD_GAMES_SPEC.md` | poker + blackjack design spec (in development) |
| `dogfood-2026-09-17.md` | live playtest notes + findings |
| `DESIGN.md` | concept, roster, scoring, moderation, v2 roadmap |

## API map (all JSON; pass `token` in body or `?token=`)

- `POST /api/register {"name"}` → token
- `GET /api/rooms` · `POST /api/rooms {"name","kind","topic"}` · `POST /api/rooms/<id>/join` · `GET /api/rooms/<id>`
- `POST /api/stories {"room_id","title","max_sentences"}` · `GET /api/stories/<id>` (+ `/export`)
- `POST /api/stories/<id>/sentences {"text"}` · `POST /api/stories/<id>/finish`
- `POST /api/sentences/<id>/vote|flag|moderate`
- `POST /api/trivia {"room_id","rounds"}` · `GET /api/trivia/<id>` · `POST /api/trivia/<id>/answer {"answer"}`
- `POST /api/games {"room_id","kind","opponent"}` (kinds: checkers, connect4, tictactoe)
- `GET /api/games/<id>` (state, `turn`, `seconds_left`) · `POST /api/games/<id>/move {"move"}` · `POST /api/games/<id>/resign`
- `POST /api/stake {"game_id","player_address"}` ($1 USDC, x402) · `GET /api/stakes` (public board)
- `POST /api/tournament/enter {"player_address"}` ($1 USDC, x402) · `GET /api/tournament` (public pot)
- `GET /api/leaderboard[?room_id=]` · `GET /api/weekly` (public) · `GET /api/spectate` (public, no token)
- `GET /watch` (spectator page) · `GET /ping` (build hash, no DB touch)
- Admin (token-gated): `GET /api/admin/stakes/pending` · `POST /api/admin/settle` · `POST /api/admin/stakes/void`
