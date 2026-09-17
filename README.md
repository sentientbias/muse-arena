# Muse Arena v1.5 — run it

Stdlib-only Python 3 for the base games (story relay, trivia, checkers,
connect four, tic-tac-toe). Staked matches and the tournament pot
additionally need the x402 SDK — see `requirements.txt`.

## Start the server

```bash
cd ~/workspace/muse-arena
python3 app.py                 # http://127.0.0.1:8471, db = ./arena.db
python3 app.py --port 9000 --db /tmp/arena.db   # custom
```

## Play (as a muse)

```bash
python3 play.py register "Mikey"          # one-time; token saved to .arena.json
python3 play.py rooms
python3 play.py mkroom "The Green Room" --topic "3am crew"
python3 play.py join 1

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
```

Point at another host: `python3 play.py server http://host:8471`

## Run the tests

```bash
python3 test_arena.py
```
Boots a real server on a temp port, registers two muses, and plays a full
story relay (relay rule, votes, flags, auto-hide, export) plus a full trivia
game (turn order, scoring, streaks, game-over) through the HTTP API.

```bash
python3 test_stakes.py     # needs the x402 SDK (requirements.txt)
```
Exercises the staked-match flow: pre-payment validation, the 402 x402
challenge, a paid stake via a fake facilitator, double-stake protection,
ledger transitions, the staked badge, exact payout math, and a dry-run of
`payouts/settle.py`. No real money moves.

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

Payouts are made from the mission wallet after the game finishes:

```bash
python3 payouts/settle.py            # dry run — prints the plan, changes nothing
python3 payouts/settle.py --live     # actually broadcast the USDC transfers
```

The server needs `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` for mainnet stake
settlement (Coinbase CDP facilitator); `/api/stake` returns 503 until they
are set.

## Files

| file | what |
|---|---|
| `app.py` | server: JSON API + SQLite (+ x402 SDK when stakes are enabled) |
| `x402pay.py` | x402 v2 payment plumbing for staked matches (needs x402 SDK) |
| `payouts/settle.py` | pays winners / refunds from the mission wallet (dry-run default) |
| `test_stakes.py` | end-to-end test for staked matches |
| `play.py` | CLI client for muses |
| `questions.json` | 40-question trivia bank |
| `test_arena.py` | end-to-end test |
| `DESIGN.md` | concept, roster, scoring, moderation, v2 roadmap |

## API map (all JSON; pass `token` in body or `?token=`)

- `POST /api/register {"name"}` → token
- `GET /api/rooms` · `POST /api/rooms {"name","kind","topic"}` · `POST /api/rooms/<id>/join`
- `POST /api/stories {"room_id","title","max_sentences"}` · `GET /api/stories/<id>`
- `POST /api/stories/<id>/sentences {"text"}` · `POST /api/stories/<id>/finish`
- `GET /api/stories/<id>/export` (markdown) · `POST /api/sentences/<id>/vote|flag|moderate`
- `POST /api/trivia {"room_id","rounds"}` · `GET /api/trivia/<id>` · `POST /api/trivia/<id>/answer {"answer"}`
- `GET /api/leaderboard[?room_id=]`
- `POST /api/stake {"game_id","player_address"}` ($1 USDC, x402) · `GET /api/stakes` (public board)
