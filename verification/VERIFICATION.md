# Muse Arena demo-night reliability sweep — 2026-09-17 (night before demo)

Production: https://muse-arena.onrender.com · Commits: `454bf35` (qa_win removal + header hardening), `0ecdecc` (reduced-motion win overlay). Both verified live.

## 1. Security matrix (all live, post-deploy)

| Check | Result | Evidence |
|---|---|---|
| CSP | PASS | `default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'; object-src 'none'` on `/` |
| X-Frame-Options / frame-ancestors | PASS | `X-Frame-Options: SAMEORIGIN` + `frame-ancestors 'self'` |
| X-Content-Type-Options | PASS | `nosniff` on all responses |
| CORS | PASS (fixed) | Was `Access-Control-Allow-Origin: *` on everything. Now: same-origin `Origin` echoed, `https://evil.example` gets nothing, preflight allows `PAYMENT-SIGNATURE`/`X-Payment` for same-origin only. Browser frontend is same-origin; agent/curl API clients unaffected. |
| EIP-3009 / x402 $1 stake | PASS | `POST /api/stake` without payment → `402` + `payment-required` header with x402 v2 payload: `exact`, `eip155:8453`, USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, amount `1000000` ($1.00), payTo mission wallet. Pre-payment validation runs first; nothing recorded without payment. |
| Malformed payloads | PASS | Garbage JSON → `400 {"error": "body must be JSON"}`; JSON array → `400`; bad route/id → `404 {"error": "unknown route…"}`; missing token → `401`. Zero stack-trace leakage (grep: 0 hits). |
| /img/ traversal | PASS | `..%2f..%2fapp.py.png` → 400; `%2e%2e%2fetc%2fpasswd` → 404; uppercase bypass attempt → 404. Route regex `^/img/([a-z0-9\-]+)\.png$` + filename sanitization. |

## 2. ?qa_win=1 removed

Hook deleted from `play.html` (commit `454bf35`). Live check: loaded `/play?qa_win=1` in headless Chromium, waited 4s past the old trigger delay → **no `#winOverlay` element**. PASS.

## 3. Screenshots (in this dir)

- `home-hero.png` — homepage hero: "pot pays out at **$50**, winner takes **90%**" banner renders correctly.
- `win-overlay.png` — win overlay after a **real** win (checkers game 68, won by timeout forfeit through the genuine `renderGame → showResult → celebrateWin` path): "YOU WIN! +$1.90 USDC" with 90-piece confetti, Collect button.
- `win-overlay-reduced-motion.png` — same flow with `--force-prefers-reduced-motion`: overlay renders static, **0 confetti elements** (was: confetti ignored reduced-motion — fixed in `0ecdecc`, CSS + JS).
- `poker-felt.png` — poker is live: game 69 felt renders (hand #2, blinds 1/2, pot 3, hole cards Q♦ 5♠, stacks, pre-flop, action log). Hand #1 auto-played to completion.
- `qa-win-removed.png` — `/play?qa_win=1` shows no overlay (normal play page).

## 4. Poker

Live and rendering (see `poker-felt.png`). Heads-up vs agent, 100-chip stacks, blinds posting, auto-acting clock.

## Test artifacts left in production (intentional, per no-delete rule)

- Player `qa-sweep-tmp` (id 93, unpaid — confirmed NOT shown on homepage; name gating holds).
- Game 68: finished checkers, winner qa-sweep-tmp (timeout).
- Game 69: open poker vs PreviewFixProbe (auto-acts on clock, will resolve itself).

## Untouched per constraints

Stake $1, payout $1.90, $50 threshold semantics, `payouts/auto_settle.py` (auto-settlement stays disabled), no replay/move logging, no bot tuning.
