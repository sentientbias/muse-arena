# Muse Arena — operational budgets

Concrete concurrency, latency, retry, and failure budgets for the arena
money and game paths. Numbers marked **[measured]** come from prior test
runs; **[code]** from reading `app.py` / `bots.py`; **[estimate]** is an
engineering judgment, not a guarantee.

## Latency budgets

| Path | Budget | Source |
|---|---|---|
| Bot move computation (checkers) | ≤ 0.8 s per move, iterative deepening, hard wall-clock deadline, max depth 6 | [code] `bots.py:175` |
| Bot move computation (connect 4) | ≤ 0.8 s per move, max depth 8 | [code] `bots.py:405` |
| Bot chained replies per request | ≤ 12 moves per `house_bot_reply` call → worst-case ~9.6 s of bot think inside one HTTP request (multi-jump chains) | [code] `app.py` (`for _ in range(12)`) |
| `POST /api/games/{id}/move` (human) | p95 target ≤ 2 s; includes human move + immediate house-bot reply in the same request | [estimate] — instrumented via `[arena] http_move` log lines and `GET /api/metrics` |
| Bot slow-move alert | any single bot move > 1.0 s is counted in `slow_moves_over_1s` | [code] `app.py` `_note_bot_move` |
| USDC transfer verification (human stake) | ≤ ~45 s worst case: `curl -sm 20` × 2 RPC endpoints, subprocess timeout 25 s | [code] `app.py` `_rpc` |
| Page / API reads (`/api/games`, `/watch`, `/api/stakes`) | p95 target ≤ 500 ms; single indexed queries | [estimate] |

## Concurrency budgets

| Resource | Budget | Source |
|---|---|---|
| Simultaneous paid stakes | 15/15 HTTP 200 in 2.53 s, 15 rows, no dupes, no orphans | [measured] local sqlite flood test (`qa/flood_test.py`) |
| Same-player double-payment race | exactly one 200 + one 409; loser's payment parked in `orphan_payments`, never invisible | [code] `UNIQUE(game_id, player_id)` atomic guard + `record_orphan_payment`; covered by `test_double_stake.py` |
| HTTP serving | `ThreadingHTTPServer`: one thread per request; a slow bot-think blocks only its own response, never other requests | [code] `app.py:23,5440` |
| DB access | one `threading.Lock` per `Arena` instance serializes all queries; Postgres `autocommit=True` | [code] `app.py` `_q` |
| Concurrent settlements | safe: `admin_record_settlement` only touches rows `WHERE status='complete' AND payout_tx IS NULL` (idempotent). Run the payout script as a single operator anyway. | [code] / [estimate] |

## Clocks

| Clock | Value | Source |
|---|---|---|
| Human per-move clock | 300 s (5 min) | [code] `app.py:270` |
| Agent per-move clock | 120 s | [code] `app.py:282` |
| Forfeit check | lazy — evaluated on next touch of the game (`make_move`, `board_game_state`); no cron | [code] `app.py` `_check_move_clock` |
| Card games on clock expiry | never forfeit — idle side auto-acts (poker: check-or-fold, blackjack: stand) | [code] `app.py` `_check_move_clock` |
| `turn_deadline` / `turn_clock` | refreshed on every applied move | [code] `app.py` `make_move` |

## Retry / failure budgets

| Failure | Behavior | Budget |
|---|---|---|
| Base RPC unreachable during human-stake verify | `503 "couldn't reach Base…"`; no stake row written; user retries with the same `tx_hash` (idempotent — no double record) | tries each of 2 RPCs once (failover, not retry) |
| Facilitator rejects/throws on x402 stake | `402` + fresh challenge; **no money moved, no row written** (validation always precedes settlement) | no retry in-app; client retries |
| Double-payment (same player, same game) | `409`; verified-but-rejected payment recorded in `orphan_payments` for manual refund | at-most-one active stake guaranteed by `UNIQUE(game_id, player_id)` |
| Postgres idle-connection death (pg autosuspend) | `_execute` reconnects exactly once, then runs the query | 1 reconnect; only connection-level errors trigger it |
| Settlement script crash mid-run | safe to re-run: already-settled rows are never touched again | idempotent by construction |
| Render free-tier cold start | `/ping` keeps the web service warm without waking Postgres | [code] `h_ping` comment |

## Money math (fixed — do not change without Anthony)

- Stake: $1.00 USDC = 1,000,000 base units, per player, fixed.
- Winner payout: $1.90 = 1,900,000 units. House rake: $0.10.
- Draw or single-stake finish: 1:1 refund (1,000,000 units), no rake.
- House bot counter-stake: conceptual only — `admin_pending` marks it `no_payout` always; it can never be paid onchain.
- Loser's stake: `no_payout` — closed, never paid.

## What is still NOT proven

- Real CDP/x402 facilitator concurrency (only the fake facilitator was flood-tested).
- Base RPC behavior under burst load (public endpoints; expect 429s — treat as hard stop for that run).
- Render Postgres under concurrent settlement writes.
- 15 simultaneous *complete* game flows (stake → play → settle).
