# Muse Arena Poker QA — 2026-09-17 (static + server-side, no live browser)

Subagent could NOT drive a live browser (no browser access at this depth) — so no
screenshots exist yet. This file documents everything verified without one, plus the
exact live-browser steps still needed.

## 1) Poker "wouldn't load" — findings

**Verdict: poker is NOT broken at the code level.** The most likely explanation is the
stake-gate UX (see below), not a crash.

Verified 2026-09-17 ~14:40 CDT against production build (local repo at commit
577dd68 = production /ping build):

- `/play` JS parses cleanly (`node --check` passes, 1 inline script, 23.7KB).
- Full walletless server flow works end-to-end locally (mirrors production code):
  - `POST /api/human/session` {"name":"QAPoker1"} → token issued
  - `POST /api/human/challenge` {token, opponent:"Zuckbot", kind:"poker"} → Game 13,
    status open, turn=QAPoker1, hand #1 preflop, blinds 1/2, stacks {QAPoker1:99,
    Zuckbot:98}, pot 3, legal_moves=[fold, allin, call 1, raise min_total 4]
  - `GET /api/games/13/hand?token=…` → hole cards ["8s","3h"] (private endpoint OK)
  - `POST /api/games/13/move` {call 1} → correctly refused pre-stake:
    "stake your $1 USDC first — then you can move"
- `renderPoker` executed in Node against the real challenge payload with a DOM stub:
  completed without throwing; rendered hole cards 8♠/3♥ and Fold / Call 1 / Raise-to
  buttons with correct onclick wiring. All referenced symbols ($, api, fetchHand,
  cardHTML, SUITMAP, doMove, showErr, myTurn) are defined.
- All poker CSS classes exist in served play.html:
  felt, potline, commrow, handrow, cardc, actions, abtn, amtrow, stacks, streetlbl,
  youlbl, lastaction, results.

**So why did Anthony see "poker wouldn't load"?** Most plausible cause — the post-
challenge flow:

1. Claim seat → pick Poker → "Challenge to Poker" → `challenge()` succeeds →
   `renderGame(d)` → player not staked → shows **step3 stake gate**, NOT a poker table.
2. The stake gate (step3) is generic: "4 · Stake $1 USDC", "Almost there — connect
   your wallet… Open this page in a wallet browser (MetaMask, Coinbase Wallet, …)".
   It never mentions **Poker**, shows no felt/table preview, no loading indicator.
3. A walletless tester is stuck there: "Connect wallet" is the only path forward,
   there is **no way back** to the game picker (btnAgain only exists in the game view),
   and nothing explains why the game is "waiting". It reads as "poker didn't load".
4. Same flow applies to all 5 games — poker-specific code is fine.

Secondary candidate: Anthony already had open QA Game 51 (poker, from duplicate-
challenge repro). Re-challenging poker → 409 → client falls back to resuming the open
game — works, but if that game's state was mid-hand with a stuck "waiting on Zuckbot…"
label and no 3s poll visible, it can look frozen. (Polling: `S.pollT=setTimeout(
refreshGame,3000)` only when it's NOT the human's turn.)

**Recommended fixes (reversible, frontend-only):**
- Show the game kind on the stake gate: "Stake $1 USDC to play **Poker** vs Zuckbot".
- Add a "← pick a different game" back link on step3.
- For walletless/no-ethereum users, replace the dead-end with clearer copy:
  "You'll need a wallet on Base to stake — the table is ready when you are."
- Consider rendering a dimmed poker-table preview behind/below the stake gate so the
  game feels "loaded" (addresses the exact complaint).
- The 409-resume path should `showOk("Resumed your open Poker game")` (it does) —
  but also verify Game 51's state server-side (still open at time of writing).

**Still needed (live browser, delegated upward):** claim seat → pick Poker →
screenshot each step; capture console errors; stake (needs real wallet) or verify
the frozen/confusing waiting state; full move → bot response round-trip in browser.

## 2) $50 banner — DOM/styles verified (no screenshot possible)

Landing `/` hero contains exactly the spec'd markup:
- `#heroTourney` > `.t-label` "pot pays out at" (small, letterspaced, muted)
- `.t-amount` big (3.4rem, 800 weight): `<span class="cash">$</span>50`
  - `.cash` = green #35d07f with glow `text-shadow:0 0 18px rgba(53,208,127,.45)`
- `.t-bar` (10px rounded track) > `#tourneyFill` (green gradient, width set by JS:
  `Math.min(100,(usd/50)*100)%`) + `.t-sheen` animated sheen
  (`@keyframes sheen`, 2.8s linear infinite, white 30% sweep)
- `.t-sub` "winner takes **90%**" (honest: never says $50 is deposited)

Game preview thumbnails all return HTTP 200 on production:
prev-checkers.png (3.3KB), prev-connect4.png (3.5KB), prev-tictactoe.png (3.7KB),
prev-poker.png (14.9KB), prev-blackjack.png (12.2KB).

## 3) Win celebration overlay — code verified (no live win triggered)

`celebrateWin` in served play.html matches spec exactly:
- `#winOverlay`: fixed, inset 0, z-index 9999, rgba(5,8,20,.78) + blur(6px), click-to-
  dismiss, auto-dismiss 6s, entrance fade
- `.win-title` "YOU WIN!" — gold gradient text (44–84px, 900 weight)
- `.win-amount` "+$1.90 USDC" — #35d07f with pulsing glow animation
- `.win-sub` "$1.90 USDC heads to your wallet shortly"
- "Collect 🎉" button + 90 confetti pieces (5 colors, random durations/delays)
- Guard `S._winFx===g.id` prevents replay for the same game
- Result card has entrance animation (cardIn keyframes)

**Caveats (from code, unverified visually):**
- No `prefers-reduced-motion` handling anywhere in play.html — motion-sensitive users
  get full confetti + pulse. Should add a media-query kill-switch.
- Overlay covers the whole screen for up to 6s; click-anywhere dismisses.

## Files saved
- ~/workspace/muse-arena/qa/play.html — served /play source (2026-09-17)
- ~/workspace/muse-arena/qa/index.html — served / source
- ~/workspace/muse-arena/qa/play_script_0.js — extracted inline JS (node --check OK)
- ~/workspace/muse-arena/qa/poker-visual-qa-notes.md — this file

## Test artifacts (local dev DB only, not production)
- Local server on :8471 used ~/workspace/muse-arena/arena.db; created player
  "QAPoker1" and local Game 13 (poker). Server shut down. Production untouched
  (no production writes were made by this QA pass).
