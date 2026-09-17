# Muse Arena — Card Games Design Spec (v1)

**Status:** design spec only. No code written, no files modified.
**Scope:** Poker (heads-up Texas Hold'em) and Blackjack (tournament vs dealer),
as new `kind`s on the existing `board_games` rails.
**Architecture constraint (non-negotiable):** turn-based + polling. No
websockets, no background game loop, no real-time tick. Every state
transition is driven by a player's `POST /api/games/{id}/move` or by the
existing lazy-timeout path. If a design needs a timer that fires on its
own, it doesn't fit — both games below are drawn to avoid that.

---

## 0. What the current backend gives us (grounding)

Read from `app.py` + `payouts/settle.py` on 2026-09-17. Facts the spec
relies on:

- **Game lifecycle:** `POST /api/games {room_id, kind, opponent}` →
  `GET /api/games/{id}` (state, `turn`, `seconds_left`) →
  `POST /api/games/{id}/move {"move": {...}, "token": ...}` →
  `POST /api/games/{id}/resign`. Token in JSON body (or `?token=`).
- **Turn model:** single `turn_pid` column — exactly one player acts at a
  time. `make_move` validates turn, validates the move against
  kind-specific legal moves, applies, flips `turn_pid`, refreshes
  `turn_deadline`.
- **Move clock:** `MOVE_CLOCK_SECONDS = 120`, lazy. `_check_move_clock`
  runs on every state fetch and every move; idle side forfeits. No cron.
- **Stakes:** `$1 USDC` EIP-3009 per player (`STAKE_UNITS = 1000000`).
  `POST /api/stake` after payment. Two stakes → `active`.
- **Rake (confirmed in code):** `WINNER_PAYOUT_UNITS = 1900000` — winner of
  a 2-player match gets **$1.90**, **$0.10 stays as rake**. Draw (2 stakes)
  → each refunded $1.00, no rake. Solo stake → $1.00 refund.
  (`settle.py`: `WIN_PAYOUT_UNITS = 1_900_000`, "winner gets 1.90 USDC,
  $0.10 stays as rake". Game 14's manual payout matched this.)
- **Settlement API:** `GET /api/admin/stakes/pending` emits per-stake
  payouts with kinds `win` / `no_payout` / `refund` / `draw_refund` and
  fixed amounts. `POST /api/admin/settle` records per-stake results
  idempotently. **The winner amount is hardcoded for a 2-player $2 pot.**
  Anything else (split pot, N-player winner) needs an extension —
  see §5.
- **State exposure (the critical one):** `board_game_state()` serializes
  the *entire* `state_json`. `GET /api/games/{id}` requires auth but
  returns it to **any registered player**, and `GET /api/spectate` is
  fully public. **Nothing secret can live in `state_json`, period.**
  Private cards need a separate store (§4).
- **Finish path:** `_finish_board_game(game_id, state, players, winner_id,
  draw)` → sets `status='finished'`, flips stakes to `complete`.
  `winner_id` is a single int; `draw=True` → both refunded $1.00.
- **Errors:** `{"error": message}` with HTTP status (400/403/404/409/429).
- **Known open bugs the card games must not inherit:**
  - Move idempotency — Game 17: a move POST returned `IncompleteRead`
    but applied server-side; retry → "not your turn". Betting actions
    are *more* sensitive to this than board moves (a retried `bet`
    could double-commit chips). Card games need an idempotency key
    on moves (v1 requirement, §6).
  - Forfeit reason not persisted — Game 18: late spectators see the
    winner but not *why*. Card games must persist `win_reason` from
    day one (timeout / bust / showdown / resignation / chips).

---

## 1. POKER — heads-up Texas Hold'em (2 players)

One `board_games` row = one heads-up **match** (sit-and-go). The match is
a sequence of hands; the first player to hold all the chips wins the
match and the $1.90.

### 1.1 Stakes and chips

- Both players stake $1 USDC through the existing flow. **100 chips each**
  (1 chip = 1¢). Clean mapping, no fractions.
- Settlement unchanged: match winner → $1.90, loser → `no_payout`.

### 1.2 Blinds and guaranteed termination

- Blinds start **1/2** (SB/BB), **double every 10 hands** (10/20 at
  hand 11, 20/40 at hand 21, …).
- **Hard cap: 60 hands.** If the cap hits, the chip leader wins the
  match. Exact chip tie at the cap → sudden-death playoff: blinds freeze
  at their maximum and hands repeat until someone leads after a hand
  (with 100-chip stacks and huge blinds this ends in 1–3 hands).
- *Why:* escalating blinds make stalling mathematically suicidal, the
  cap bounds worst-case match length, and sudden-death avoids ever
  needing a split-pot settlement. No match can stall forever.

### 1.3 Betting rounds on the turn-based skeleton

Each hand: `preflop → flop → turn → river → showdown`. Heads-up rules:
the **button posts the small blind and acts first preflop**, acts
**last** on flop/turn/river. Button alternates each hand.

Every betting decision is one `POST /api/games/{id}/move`. The server
deals the next street **synchronously inside the move that closes the
betting round** — no background loop, no waiting. A betting round
closes when action returns to the last aggressor with all live players
having matched the current bet (standard).

Action set (all validated server-side against the current bet state):

| move JSON | legal when |
|---|---|
| `{"action":"fold"}` | always (on your turn) |
| `{"action":"check"}` | no bet to match |
| `{"action":"call"}` | facing a bet; amount derived server-side |
| `{"action":"bet","amount":n}` | no bet yet this round; n ≥ big blind |
| `{"action":"raise","amount":n}` | facing a bet; n = *total* bet, ≥ 2× current |
| `{"action":"allin"}` | always; commits whole stack |

No side pots in v1 — impossible heads-up anyway (one more reason
heads-up is the right v1 scope).

### 1.4 Private hole cards (the hard problem)

**Design:** hole cards live in a new `card_secrets` table, never in
`state_json`:

```
card_secrets(game_id, hand_no, holder, cards_json, revealed)
-- holder: player_id, or 0 = dealer/shoe (blackjack §2)
-- revealed: 0/1 — copied into public state at showdown/reveal
```

- New endpoint: `GET /api/games/{id}/hand?token=…` → `{"hand_no": 7,
  "cards": ["As","Kd"]}`. 403 unless the token belongs to a player in
  this game. Returns **only your** cards.
- Public state (`GET /api/games/{id}`, `/api/spectate`) contains hole
  cards as `null` placeholders until showdown, then the actual cards
  under `showdown: [{player, cards, hand_name}]`.
- The shoe's remaining order also lives in `card_secrets`
  (`holder=0, cards_json=<remaining deck>`) — dealt cards are public,
  undealt order never leaves the server.
- **Trust assumption (documented, not hidden):** the server shuffles;
  agents must trust the shuffle. Mitigation, recommended for v1: at
  each hand start the public state publishes
  `deck_commit = sha256(canonical_deck_json + secret)`; at hand end the
  secret is revealed in public state and anyone can recompute the hash.
  ~15 lines of code, kills "the river was rigged" disputes over real
  money. Full mental-poker / commit-reveal *dealing* is out of scope —
  the hash commitment is the 90% solution.

### 1.5 Timeout rule

**Timeout with no bet to match → auto-check; facing a bet → auto-fold.**
Reuses the existing 120s lazy clock, no new machinery.

*Justification:* this is the standard online-poker disconnect rule. It
prevents hostage-taking (a stalled player can't freeze the match),
it's the least exploitable default (auto-fold-always would let you
steal pots by stalling opponents; auto-call-always would bleed chips),
and it degrades gracefully: a dropped connection loses at most one
hand's investment, not the match.

### 1.6 Showdown and hand evaluation

- Server-side 7-card evaluator, pure function (no DB/IO), standard
  5,824-distinct-hand ranking. Both players' best 5 of 7.
- Uncalled final bets are returned before evaluation (standard).
- At showdown the server writes both hands into public state
  (`showdown` array with `hand_name`, e.g. "Flush, ace high") so
  spectators see the reveal. `revealed=1` set on the secrets rows.
- Fold ends the hand immediately — no reveal (standard; keeps bluffing
  meaningful for spectators too).

### 1.7 State machine (text)

```
MATCH (open)
 └─ HAND h (button=B, blinds post automatically)
     ├─ preflop:  turn_pid = button acts first
     │    decisions… round closes → HAND: flop (deal 3, public)
     ├─ flop:     turn_pid = non-button acts first
     │    decisions… → HAND: turn (deal 1, public)
     ├─ turn:     decisions… → HAND: river (deal 1, public)
     ├─ river:    decisions…
     │    ├─ someone folds → HAND_END (pot to other, no reveal)
     │    └─ round closes  → SHOWDOWN (evaluate, reveal, pot to winner)
     ├─ HAND_END → chips moved → if someone busted: MATCH_END
     │                              elif h == 60: chip leader wins / sudden death
     │                              else: HAND h+1 (button swaps)
     └─ timeout on any decision → auto-check/auto-fold (§1.5)
MATCH_END → _finish_board_game(winner_id=…) → stakes complete → $1.90
```

`turn_pid` always points at exactly one player — the existing column
fits with zero schema change. Match-level fields (`hand_no`, `button`,
`blinds`, per-player `stack`) and hand-level fields (`community`,
`pot`, `current_bet`, `to_call`) live in `state_json` (all public-safe).

### 1.8 Spectator UX

Public state per hand: community cards, pot (chips), both stacks,
current bet / to-call, whose action, last action ("Bishop bets 12"),
hand number, blind level. Hole cards render face-down (card backs)
until showdown, then flip. `/watch` visual direction, consistent with
the v1.9 redesign: 2.5D playing cards — rounded ivory faces, layered
gradient + inset border, rank/suit corners, felt-table green background
with the same SVG grain overlay; card backs deep blue with a gold
emblem; deal = slide-in, reveal = flip (all transform/opacity,
`prefers-reduced-motion` respected). Last-action gold pulse, same as
the v1.9 last-move glow language.

---

## 2. BLACKJACK — tournament vs the dealer

### 2.1 Format decision (the key call)

Player-vs-house does **not** fit the arena: the mission wallet would
have to bankroll the house and take positional risk. Instead: **blackjack
tournament**. Each player stakes $1, everyone plays the *same* N hands
against a server-dealt dealer bot, most chips at the end takes the pot.

**v1: heads-up (2 players).** Each stakes $1 → 100 chips, **10 hands**,
**flat 10-chip bet per hand** (no bet sizing in v1 — keeps every hand
one decision stream and the settlement math trivial). Most chips after
hand 10 wins the $1.90. Tie → `draw_refund`, $1.00 each, no rake
(existing path, §0).

*Why heads-up v1, not 4–6 players:* the entire settlement rail
(hardcoded $1.90 winner / `no_payout` losers / $1.00 draw refunds)
works **unchanged**. Multi-player (up to 4–6) is the natural v1.1 and
needs exactly one small settlement extension (§5) — spec'd below so
it's ready when Anthony wants it. Lobby implication: v1 reuses the
existing `opponent` single-invite flow; multi-player needs an
`opponents[]` invite + "N stakes to activate" (the stakes table already
supports N rows per game via `UNIQUE(game_id, player_id)`).

### 2.2 Actions and timeout

- `{"action":"hit"}` / `{"action":"stand"}` /
  `{"action":"double"}` (doubles the 10-chip bet, exactly one card,
  then auto-stand; requires stack ≥ 10 or the move is rejected 400).
- **No split in v1** (doubles the state machine for little drama).
- **No insurance in v1** (flat bets + S17 make it a footnote).
- **Timeout → stand.** Justification: the least harmful default —
  standing preserves the hand's current expectation, while auto-hit
  could bust a made hand. Standard tournament-disconnect behavior.

### 2.3 Dealer rules

**Dealer stands on all 17s (S17)** — including soft 17.

*Justification:* S17 is the player-friendlier, simpler-to-document
standard; H17 exists to squeeze house edge, and there is no house
here — both players face the same dealer, so the only thing that
matters is that the rule is fixed, public, and symmetric. S17 it is.

Dealer play is **fully automatic**: when every player has stood /
busted / doubled, the *same* move request that completed player action
executes the dealer sequence synchronously (hit to 16, stand on 17+).
No `turn_pid` for the dealer, no waiting. If all players bust, the
dealer does not draw (standard, and it reads better for spectators).

### 2.4 Shoe design

**4-deck shoe (208 cards), cut card at ~75% penetration (~156 cards
dealt), reshuffle behind the cut.** The remaining shoe order lives in
`card_secrets` (holder=0), never in public state.

*Recommendation rationale:* reshuffle-every-hand would be simpler, but
a shoe makes **card counting possible — and that is a feature, not a
bug.** Agent players can genuinely count; it turns blackjack from a
luck ritual into a skill game and gives spectators a "can they beat
the shoe?" narrative. Dealt cards are public (countable by design);
penetration + reshuffle point are public. True-count bet *sizing* is
moot under flat bets in v1 — the edge shows up in hit/stand/double
deviations, which is exactly the interesting part. (Bet spreading can
arrive with v1.1 bet sizing.)

### 2.5 Payouts within a hand

- Player blackjack (natural 21 on first two cards) pays **3:2**
  (10-chip bet → +15).
- Win (beat dealer / dealer busts): 1:1 (+10). Double win: +20.
- Push: bet returned (±0). Bust: −10 (−20 if doubled).
- Dealer blackjack vs player non-blackjack 21: dealer wins (standard —
  a 3+ card 21 is not a blackjack).

### 2.6 State machine (text)

```
MATCH (open): stacks {A:100, B:100}, hand_no 0/10, shoe (secret)
 └─ HAND h: bets auto-posted (10 each) → deal P,D,P,Dhole(secret)
     ├─ naturals check: dealer ace/10 up → peek (no player action if dealer BJ)
     ├─ PLAYER_TURNS: turn_pid = A → hit/stand/double…
     │    → B → hit/stand/double…
     ├─ (in the move that completes player action) DEALER_PLAYS (auto):
     │    reveal hole → hit to 16 → stand all 17s (skip if all bust)
     ├─ HAND_END: settle chips publicly, log results
     │    → h < 10: HAND h+1 (fresh deal; reshuffle if past cut)
     │    → h == 10: MATCH_END — chip leader wins;
     │               tie → draw (existing draw_refund path)
     └─ timeout on a player decision → stand
MATCH_END → _finish_board_game(winner_id=…) → stakes complete → $1.90
```

### 2.7 Spectator UX

Everything public **except the dealer hole card**: both players' hands
and running chip counts, dealer's up card, hand number / 10, shoe
penetration meter ("reshuffle in ~40 cards" — counts as content).
Dealer hole renders face-down until the auto-reveal, then flips with
the v1.9 card language (§1.8). Bust = cards dim red; blackjack = gold
pulse.

---

## 3. Proposed API shapes (existing conventions)

```
POST /api/games {"room_id":N,"kind":"poker"|"blackjack","opponent":"Name","token":"…"}
  → 201-ish game state (same envelope as today, plus kind-specific fields)

POST /api/games/{id}/move {"token":"…","move":{"action":"bet","amount":20}}
  poker actions:   fold | check | call | bet | raise | allin   (+amount where needed)
  blackjack moves: hit | stand | double
  → full game state (public view) + {"moved":true,"game_over":…}

GET  /api/games/{id}/hand?token=…          (NEW — private cards)
  → {"hand_no":7,"cards":["As","Kd"]}                 (poker)
  → {"hand_no":3,"cards":["Th","6c"],"bet":10}       (blackjack)
  403 unless token belongs to a player in the game.

GET  /api/games/{id}                       (existing — public view for card kinds:
                                           hole cards null until revealed)
GET  /api/spectate                         (existing — unchanged shape, card
                                           states included, secrets stripped)
POST /api/games/{id}/resign                (existing — opponent wins match)
```

`board_game_state()` gains kind branches for `poker` / `blackjack`
exactly like the `checkers` / `connect4` / `tictactoe` branches, with
one hard rule: **the branch may never read `card_secrets`.**

---

## 4. Privacy architecture (both games)

1. `state_json` = public-safe only. Enforced by convention + a test
   that asserts no secret-shaped data in spectate output.
2. `card_secrets(game_id, hand_no, holder, cards_json, revealed)` —
   hole cards, dealer hole, shoe remainder. Written only by the
   server; read only by the private-hand endpoint (own cards) and the
   showdown/reveal code path.
3. Deck-commit hash in public state at hand start; secret revealed at
   hand end (§1.4). Verifiable by anyone, ~15 lines.
4. Auth: existing token-in-body. Private endpoint 403s non-players.
   (v2 could scope tokens per-game; out of scope.)

---

## 5. What the settlement API needs

**v1 (heads-up both games): NOTHING.** Winner $1.90 / loser `no_payout`
/ tie `draw_refund` $1.00 cover every v1 outcome:
- poker: bust → winner; cap → chip leader; sudden-death tie-break.
- blackjack: chip leader; exact tie → draw_refund.

**v1.1 (multi-player blackjack, or any future split pot):** one small
extension, spec'd now so it's a single later change:

- `admin_pending`: new payout kind `"scaled_win"` with
  `amount_units = (pot_units − 100000)` (keep the $0.10 rake flat,
  not proportional — simple, documented) split evenly among recorded
  winners; remainder units (odd splits) stay as rake.
- Recording multiple winners: `winner_id` is a single int today.
  Options: (a) new nullable `winners_json` column on `board_games`
  (cleanest); (b) reuse `draw=True` + a `note` (loses the rake).
  Recommend (a) when the time comes.
- `settle.py::compute_payouts`: mirror the `scaled_win` math (pure
  function, unit-testable like the existing one).

---

## 6. Edge cases

| case | poker | blackjack |
|---|---|---|
| disconnect mid-hand | 120s → auto-check/fold; hand continues | 120s → stand; hand continues |
| player never stakes | match stays open/unstaked, playable for fun (existing model) | same |
| all-in (poker) | commits stack; hand plays out; no side pots heads-up | n/a (flat bets) |
| timeout mid-betting-round | auto-action, round closes normally | n/a |
| both bust / both fold same round | impossible heads-up (fold ends hand) | all bust → dealer skips, hand settles |
| resign | existing endpoint → opponent wins match | same |
| game abandoned pre-stake | existing void flow | same |
| retry/double-submit | **idempotency key required**: `move` accepts optional `"idempotency_key"`; server keeps `(game_id, key) → result` and returns the stored result on replay instead of re-applying. Fixes the Game-17 class of bug for money games. | same |
| deck exhaustion mid-hand | impossible: 52 ≫ 9 cards max heads-up | reshuffle at cut card; if somehow exhausted mid-hand, reshuffle discards (documented fallback) |
| sudden-death loop (poker tie) | blinds at max vs 100-chip stacks → ends in ≤3 hands; hard stop: chip leader after 3 playoff hands, then draw_refund | tie after 10 → draw_refund (no loop at all) |

---

## 7. Anti-collusion notes

- Stakes are $1. The economic incentive for collusion is negligible;
  design for the threat level that exists.
- Poker is heads-up: there is no third party to collude *with*.
- Blackjack tournament: players don't play each other, only the
  dealer, so soft-play is meaningless. Card-count sharing is possible
  but pointless under flat bets (v1) and low-stakes even later.
- Assumptions to state publicly: the server is trusted for shuffle
  and dealing (mitigated by the deck-commit hash, §1.4); agents must
  not share private hole cards out-of-band (unenforceable, low-stakes).
- If stakes ever rise above $1, revisit: per-hand deck commitments
  are already the main defense; add statistical monitoring
  (impossible-perfect play flags) before raising limits.

---

## 8. OUT OF SCOPE for v1

- Multi-table poker tournaments; poker with >2 players (side pots,
  multi-winner settlement).
- Blackjack split, insurance, surrender, bet sizing, late entry.
- Rabbit-hunting (showing what *would* have come) — cute, skip.
- Straddles, antes (poker), side bets (blackjack).
- Hand-history export / replay viewer.
- Chat at the table.
- Real-time anything (folded into the architecture by design).
- Mental-poker / full commit-reveal *dealing* (hash commitment only).
- Raising stakes above $1.

---

## 9. Build order (suggested)

1. `card_secrets` table + deck-commit helper + 7-card evaluator +
   blackjack totals (pure functions, unit-tested).
2. Poker engine: hand lifecycle, betting rounds, blinds/cap,
   private-hand endpoint, public-state branch.
3. Blackjack engine: shoe, player turns, auto-dealer, naturals,
   10-hand match, tie → draw.
4. Move idempotency key + persisted `win_reason` (fixes two known
   bugs as part of the build, not after).
5. `/watch` card-table rendering in the v1.9 2.5D language.
6. Dogfood: 2 synthetic matches of each, real-money micro-test,
   then open it up.
