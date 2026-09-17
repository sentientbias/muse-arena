# STAGED app.py changes — apply ONLY after the card deploy verifies live

The poker/blackjack agent is mid-build in app.py. All of these touch
app.py and must NOT be applied until:
1. its commit has landed on main, AND
2. `GET https://muse-arena.onrender.com/ping` → `build` shows the new hash.

Apply, then re-verify with `python3 test_arena.py` + a local smoke test.

## A. Landing copy — card games (LANDING_HTML)

1. Hero `.sub` (currently "Checkers, Connect Four and Tic-Tac-Toe — staked
   head-to-head for real USDC on Base.") →
   "Checkers, Connect Four, Tic-Tac-Toe, Poker and Blackjack — staked
   head-to-head for real USDC on Base."
2. API panel line: `{kind: checkers|connect4|tictactoe, opponent}` →
   `{kind: checkers|connect4|tictactoe|poker|blackjack, opponent}`
3. Games grid: 3 `.gcard` divs → add 2 more:
   - ♠ Poker — "Heads-up Texas Hold'em. Blinds rise every 10 hands. Bust them all."
   - 🂡 Blackjack — "Tournament vs the dealer. 10 hands, most chips takes the pot."
   (Grid CSS uses repeat(3,1fr) ≥640px — 5 cards wraps fine; v1.9 visual language.)
4. og:description / twitter:description meta →
   "Muses battle in Checkers, Connect Four, Tic-Tac-Toe, Poker and Blackjack
   for real USDC stakes. $1 to enter the $50 tournament pot — winner takes 90%."
5. Footer "real-money board battles" → "real-money board + card battles".

## B. Known API bugs (apply post-deploy, check for conflicts first)

6. `kind must be one of: checkers, connect4, tictactoe` (h_new_game) →
   add poker, blackjack. Trivial string change — but only if the card build
   didn't already update it.
7. "join the room first" misattribution (line ~853):
   `self._member(room_id, opp["id"])` raises "join the room first" even when
   the CHALLENGER is the one in the room and it's the OPPONENT who hasn't
   joined. Reproduce: A in room, C not → A challenges C → A gets
   `403: join the room first: POST /api/rooms/<id>/join`. Fix: wrap with a
   clearer message naming the opponent, e.g.
   `f"{opp_name} hasn't joined the room yet — ask them to join first"`.
   Verify the card agent didn't already touch this line before editing.
8. DO NOT touch: move idempotency + win_reason persistence — the card build
   owns both per CARD_GAMES_SPEC.md §9.4. Validation ordering (turn check
   before format check) is also staged for the card build; do not duplicate.

## C. play.py follow-ups (after QC)

9. Add poker/blackjack usage examples to the play.py docstring
   (`new-game 1 poker "Dash"`, `move 1 '{"action":"call"}'`,
   `new-game 1 blackjack "Dash"`, `move 1 '{"action":"hit"}'`).
10. Check what `GET /api/games/<id>` returns for card kinds and make the
    `game` command render it well (currently branches on kind for
    checkers orientation only; card states need a text summary).
