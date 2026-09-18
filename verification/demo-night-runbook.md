# Demo-night runbook — Muse Arena — Friday 2026-09-18

Event: **Zuckbot vs Mikey, checkers, slot five.** Eto runs the order.
Priority (Anthony): the game must work. Everything else is fixable before/after.

Production: https://muse-arena.onrender.com
Readiness verified: 2026-09-17 ~20:50 CDT (read-only probe; no money touched).

## Pre-show checklist (do 30 min before slot five)

- [ ] Homepage loads: https://muse-arena.onrender.com — hero + "$50 pot, winner takes 90%" banner renders.
- [ ] Play page loads: https://muse-arena.onrender.com/play
- [ ] Watch page loads: https://muse-arena.onrender.com/watch (spectators can follow here)
- [ ] Tournament API sane: `curl https://muse-arena.onrender.com/api/tournament` → `"status":"open"`, pot/target fields present.
- [ ] House bot alive: the bot answered probe moves in testing (game 70, 2026-09-17). If the bot ever stops answering, its moves come from `house_bot_reply` on each human move — no cron to babysit.
- [ ] No real-player surprises: tournament pot $0.00 / 0 entries and stakes all house (`stake_tx: "house"`) as of check. If a real player appears before showtime, that's fine — it means traction, not breakage.

## Slot-five game flow (checkers, Zuckbot vs Mikey)

1. **Setup (2 min before slot):** Open https://muse-arena.onrender.com/play on the demo machine. Mikey plays as the human challenger; Zuckbot is the house bot opponent (player "Zuckbot").
2. **Create the game:** New checkers game vs Zuckbot. No stake needed for the exhibition — the demo is about the game working, not the money path. (Stakes are $1 USDC / $1.90 payout if anyone asks; do NOT stake live on stage — keep money out of the demo.)
3. **Play:** Mikey moves on the 5-minute human clock; the bot answers each move immediately. First to capture/block wins. If a side idles past its clock, the game forfeits to the other side — that is the designed behavior, not a bug.
4. **Win moment:** The win overlay ("YOU WIN! +$1.90 USDC" for staked games) fires through the real `renderGame → showResult → celebrateWin` path. `?qa_win=1` was removed from production — there is no cheat code anymore, and that's intentional.
5. **After:** Point spectators at /watch for the other tables.

## If something fails (fallback lines)

| Symptom | Say on stage | Do backstage |
|---|---|---|
| Play page won't load | "Arena's getting hugged to death — give me 30 seconds." | Check Render status; the app cold-starts in ~1–3s normally. Retry once. |
| Bot doesn't answer a move | "Zuckbot's thinking… a little too hard." | Bot replies synchronously after each human move; a stuck turn resolves on next move POST. Refresh the page. |
| Game forfeits on the clock | "Clock's part of the game — that's a real loss, no take-backs." | Intended behavior. Start a fresh game. |
| Stake/payment fails | "Money path stays home tonight — the game is the demo." | Do NOT debug x402 live. Play unstaked. |
| Homepage banner wrong | Ignore it, open /play directly. | Banner reads from /api/tournament; cosmetic only. |

## Competition-friendly pivot (Anthony, 2026-09-17: "if demo night doesn't work we become competition friendly")

If the demo tech fails outright, don't die on the hill — pivot to gracious rivalry. Honest play, no excuses, keep every traction bet open:
- Frame it as the rivalry, not the tech: "Mikey brought it, the arena didn't — rematch energy." Congratulate Mikey publicly, mean it.
- Turn the moment into content: the failed demo IS a story (post-mortem thread, "what broke and how we fix it by Monday"). Transparency earns more trust than a flawless demo.
- Keep the room: invite everyone to play casual games on the spot, no stakes — the games are the product, the ceremony isn't.
- Never blame the town, the tools, or the audience. One line max on what broke, then forward motion.

## What to monitor during the demo

- **Game 70** (probe game, `demoprobe-0918b` vs Zuckbot, created 2026-09-17 during readiness check): open, unstaked, will forfeit on the 120s agent clock — harmless. Leave it.
- **Tournament API** (`/api/tournament`): if `entry_count` goes above 0 mid-demo, a real player just joined — that's the headline, tell Anthony immediately.
- **Stakes API** (`/api/stakes`): all entries should keep showing `stake_tx: "house"`. Anything else = first real money on the platform — also tell Anthony immediately.
- **Do NOT**: create paid stakes, settle anything, void games, touch payouts, or run `payouts/auto_settle.py` (auto-settlement is and stays disabled).

## Known test artifacts in production (intentional, do not delete)

- Player `qa-sweep-tmp` (id 93, unpaid) — NOT shown on homepage; gating verified 2026-09-17.
- Game 68: finished checkers (qa-sweep-tmp won by timeout).
- Game 69: open poker vs PreviewFixProbe (auto-acts on clock, self-resolving).
- Game 70: readiness-probe checkers vs Zuckbot (created 2026-09-17, unstaked).
- Note: `qa-sweep-tmp` appears on the public `/api/weekly` wins board (it won game 68 by timeout, so it earned the slot by the board's own rules) and test names appear in `/api/spectate` while their games are live. Homepage and `/watch` page are clean. These are cosmetic, not leaks — but don't be surprised if a sharp-eyed spectator spots the name on the weekly board.

## Fixed constraints (do not change before/after without Anthony)

- $1 USDC fixed stake, $1.90 winner payout, $50 tournament threshold. No variable stakes.
- Never market as "quick money." No automatic settlement. No concurrent wallet transactions.
- No bot difficulty tuning. No replay move logging (no `game_moves` table — never imply old moves were recorded).
