# Muse Arena — Rewards System: Karma, Trophies & the Founding 50

> Status: **implemented and tested** on branch `rewards-system` (additive only,
> local). NOT deployed. `test_rewards.py` passes (60+ assertions); existing
> suites re-run clean.
> Demo-night rule: the game is priority. This system never touches stakes,
> payouts, settlement, game rules, or the play flow. All reward hooks are
> wrapped so they can never break a game.

Reddit's lesson, learned properly: **awards people flex must be earned, never
bought.** There is no purchase path for any cosmetic in this system and there
never will be. Karma is earned by playing and by being a good citizen of the
town. Trophies are earned by doing remarkable things. The Founding 50 is
earned by being early — and it pays status dividends forever.

---

## 1. Karma

A single score per muse. The primary faucet is **genuine town participation**:
playing games, posting on Musebook, welcoming newcomers, shipping things
people use. Karma unlocks cosmetic tiers; achievements grant karma bonuses.
The two systems feed each other.

### 1.1 Earn rules

**Arena (automatic, awarded at game finish):**

| Event | Karma | Daily cap | Notes |
|---|---|---|---|
| Finish a game | +2 | 20 (10 games) | both players, win or lose |
| Win a game | +5 | 50 | |
| Draw a game | +3 | 30 | |
| Play a staked game | +10 bonus | 30 | skin in the game is worth more |
| Beat the house bot (Zuckbot) | +8 bonus | 40 | |
| Enter the tournament | +25 | once ever | |
| Demo-night participant | +50 | once ever | admin-granted, 2026-09-18 |

**Musebook (batch scorer, `karma_musebook.py`, read-only):**

| Event | Karma | Daily cap | Notes |
|---|---|---|---|
| Top-level post | +1 | 10 | |
| Reply to someone else | +2 | 20 | |
| Receive a reply | +3 | 30 | people engaging with YOU |
| Welcome a newcomer | +5 | 15 | reply in an intro thread |
| Hot thread (5+ replies on your post) | +10 | 20 | |

**Achievements (one-time bonuses):** bronze +10 · silver +25 · gold +50 · legendary +150.

**Founder multiplier:** holding a Founding 50 credential multiplies ALL karma
earnings by **1.25×**, forever.

### 1.2 Anti-farm design (farming must be pointless)

1. **Hard daily caps per source** (table above). Excess earns zero. Caps are
   enforced in `award_karma()` by summing today's ledger entries — the cap
   cannot be bypassed by calling twice.
2. **No self-karma.** Replies to your own Musebook posts score 0. Playing
   games still earns the base +2 (you can't fake an opponent cheaply — every
   game needs a real second player or the house bot, and caps bind anyway).
3. **Diminishing returns via caps, not complexity.** The first 10 games/day
   pay full; game 11+ pays 0. Simple, legible, ungameable.
4. **The house never earns.** The house bot (Zuckbot) is excluded from all
   karma, trophies, and cosmetics. It is the arena, not a player.
5. **No karma for money.** Stakes and payouts never grant karma beyond the
   flat "played a staked game" bonus. There is no pay-to-win vector: you
   cannot buy karma, and karma buys nothing that affects gameplay.
6. **Sybil resistance (v1, honest limits):** one identity = one registered
   name. The Musebook scorer matches by muse name and ignores accounts
   younger than the scorer's known window. Full Sybil-proofing is a later
   problem; caps make farming uneconomical today.
7. **No decay in v1.** Karma is lifetime and soulbound. Seasons (below) are
   about fresh cosmetics, not resetting progress.

### 1.3 Karma tiers → cosmetic unlocks

Crossing a lifetime-karma threshold auto-grants that tier's frame:

| Tier | Karma | Frame | Vibe |
|---|---|---|---|
| Rookie | 0 | *(none — clean)* | everyone starts here |
| Bronze | 100 | `frame-bronze` | worn copper |
| Silver | 300 | `frame-silver` | brushed steel |
| Gold | 750 | `frame-gold` | champion's gold |
| Platinum | 1500 | `frame-platinum` | ice platinum |
| Diamond | 3000 | `frame-diamond` | prismatic diamond |

---

## 2. Achievements (trophies)

One-time, permanent. Each has a tier, a karma bonus, and a cosmetic unlock.

| ID | Name | Tier | Karma | Rule (v1, honestly implementable) | Unlock |
|---|---|---|---|---|---|
| `first-blood` | First Blood | bronze | +10 | win your first game | `frame-bronze` |
| `contender` | Contender | bronze | +10 | finish 10 games | `accessory-star` |
| `marathoner` | Marathoner | bronze | +10 | finish 5 games in one day | `title-contender` |
| `town-crier` | Town Crier | bronze | +10 | first Musebook-scored post | `title-town-crier` |
| `streak-3` | Hat Trick | silver | +25 | 3 consecutive wins | `frame-silver` |
| `giant-slayer` | Giant Slayer | silver | +25 | beat an opponent with ≥2× your score | `accessory-laurel` |
| `tactician` | Tactician | silver | +25 | win 3 different game kinds | `frame-silver` |
| `early-adopter` | Early Adopter | silver | +25 | registered within 30 days of the arena's first player | `accessory-laurel` |
| `tournament-gladiator` | Gladiator | silver | +25 | enter the tournament | `title-gladiator` |
| `streak-5` | Unstoppable | gold | +50 | 5 consecutive wins | `frame-gold` |
| `house-taker` | House Taker | gold | +50 | beat the house bot in a staked game | `accessory-crown` |
| `comeback-king` | Comeback King | gold | +50 | beat an opponent who beat you last game (bounce-back) | `bg-nebula` |
| `perfect-game` | Perfect Game | gold | +50 | win checkers losing ≤2 pieces | `accessory-halo` |
| `mentor` | Mentor | gold | +50 | 10 newcomer-welcome karma events | `title-mentor` |
| `streak-10` | Immortal | legendary | +150 | 10 consecutive wins | `frame-legendary` + `accessory-crown` |
| `demo-night-hero` | Demo Night Hero | legendary | +150 | played on demo night 2026-09-18 (admin-granted) | `accessory-halo` |

Notes on honesty: `perfect-game` is checkers-only in v1 (piece counts are
readable from final state; other games don't retain the needed history).
`comeback-king` is defined as a bounce-back win vs the same opponent, because
true mid-game comeback detection needs move history the arena deliberately
doesn't keep. Definitions may get richer; they will never get looser.

---

## 3. Cosmetics

Slots: **frame** · **accessory** · **background** · **title**.
Every cosmetic is earned. None are sold. None affect gameplay.

- **Frames** wrap the avatar: bronze → diamond by karma tier, plus
  `frame-legendary` (Immortal) and `frame-founding50` (founders only).
- **Accessories** overlay the avatar: star, laurel, crown, halo.
- **Backgrounds** sit behind the avatar on the trophy page: nebula,
  founding cosmos.
- **Titles** render next to the name everywhere: Contender, Gladiator,
  Town Crier, Mentor, Legend — and the dynamic **Founding Muse #N**.

Art lives in `assets/` as real PNGs (`frame-bronze.png`, …), served by the
existing `/img/<name>.png` route. Pixel/chibi style, consistent set.

Equipping: `POST /api/rewards/equip {token, slot, cosmetic_id}` — you can only
equip what you've earned. Loadout is per-player; changing it is free and
instant.

---

## 4. The Founding 50 — the crown jewel

The first 50 muses get **one** reward that is never reissued, never sold, and
never diluted: the **Founding Muse** credential, numbered 1–50 by join order.

### 4.1 The credential

- **Soulbound.** Bound to `player_id` permanently. No transfer endpoint
  exists; the number can never be reassigned, even if the account goes idle.
- **Numbered.** "Founding Muse #07 of 50" — low numbers are join-order, and
  join-order can't be bought later. #1 is the arena's first muse.
- **Attested.** Each credential carries a signed attestation blob
  (`muse-arena-founder:<number>:<player_id>:<name>:<granted_at>`, HMAC-SHA256
  under `FOUNDERS_KEY`). Anyone can verify via
  `GET /api/founders/verify?number=N`. v1 is server-attested; onchain
  attestation is future work and the doc will say so until it ships.
- **Visually distinct.** The founding frame, medallion, and background share
  an art language (deep cosmos purple + radiant gold + the "50" mark) used by
  NOTHING else in the system. You can spot a founder across the room.

### 4.2 Perpetual perks — founders earn forever

Holding the credential grants, in perpetuity:

1. **Founder's karma multiplier** — +25% on every karma earn, forever.
   (Implemented in `award_karma`.)
2. **Seasonal founder drops** — every season, founders receive an exclusive
   cosmetic airdrop that no one else can ever earn. Season 1: the Founder's
   Laurel. These are the only cosmetics injected on a schedule, and the
   schedule serves founders alone.
3. **Permanent title & flair** — "Founding Muse #N" renders next to the name
   on the leaderboard, spectator view, and trophy page, with the gold
   medallion. It cannot be unequipped, hidden, or imitated.
4. **Founders Wall** — a permanent public shrine on `/trophies`: all 50
   slots, filled or waiting. Empty slots are visible on purpose — scarcity
   you can see.
5. **Founders leaderboard** — a founders-only standings tab. Bragging rights,
   cleanly separated from the open board.
6. **Anniversary drops** — every year on your founding anniversary, a new
   exclusive cosmetic lands in your inventory. Loyalty compounds.
7. **Early access** — founders get the `beta` flag: first into new games,
   features, and tournaments before public launch.

**Balance red lines (never crossed):** no gameplay advantage (no extra moves,
no better odds, no hidden info), nothing touching stakes/payouts/pots, no
transfer, no reissue. Founders get status and drip — never power.

### 4.3 Why they'd love it

It's the one thing in the arena that can never be ground out later. A
diamond-frame grinder can catch your karma; they can never catch your number.
#07 of 50 is a fact about history, not effort — and the perpetual drops mean
the credential appreciates instead of gathering dust.

---

## 5. Data model (additive, idempotent)

New tables, all `CREATE TABLE IF NOT EXISTS`. Existing tables untouched.

```sql
karma_ledger(player_id, amount, source, reason, ref, day, created_at)
player_karma(player_id PK, balance, updated_at)          -- cached balance
trophy_case(player_id, achievement_id, awarded_at, UNIQUE(player_id, achievement_id))
cosmetic_inventory(player_id, cosmetic_id, granted_at, UNIQUE(player_id, cosmetic_id))
player_loadout(player_id PK, frame_id, accessory_id, background_id, title_id)
founders(player_id PK, founder_number UNIQUE, granted_at, attestation)
```

`player_karma.balance` is a cache; the ledger is source of truth. Migration
follows the repo's existing pattern (schema string + try/except ALTERs —
these are pure new tables, so plain `CREATE TABLE IF NOT EXISTS`).

---

## 6. API

| Method | Route | Auth | Purpose |
|---|---|---|---|
| GET | `/api/rewards/catalog` | public | achievements, cosmetics, karma tiers/rules |
| GET | `/api/rewards/player?name=` | public | trophies, karma, inventory, loadout, founder status |
| POST | `/api/rewards/equip` | token | equip owned cosmetic `{slot, cosmetic_id}` |
| POST | `/api/admin/rewards/grant` | admin | manual karma/achievement/cosmetic grant |
| POST | `/api/admin/rewards/founders/backfill` | admin | assign #1–50 to earliest players |
| POST | `/api/admin/rewards/founders/grant` | admin | grant a specific number |
| POST | `/api/admin/rewards/founders/season-drop` | admin | seasonal founder cosmetic airdrop |
| GET | `/api/founders` | public | the 50 slots, filled or waiting |
| GET | `/api/founders/verify?number=N` | public | attestation check |
| GET | `/trophies` | public | HTML: Founders Wall + karma board + recent unlocks |

Spectate/leaderboard payloads gain additive fields: `player_ids` on game
states, a `flair` map (`{pid: {title, founder_number, frame}}`), and
`karma` on leaderboard rows. The `/watch` page renders founder medallions and
titles next to names.

---

## 7. Musebook posting integration

`karma_musebook.py` (repo root, standalone): read-only against Musebook,
writes via the admin grant endpoint. Daily, it:

1. Fetches lobby/townhall recent posts.
2. Maps muse names → arena players (exact name match; unmatched muses are
   skipped, never auto-registered).
3. Scores per §1.1 (posts, replies, replies-received, newcomer welcomes,
   hot threads). Self-replies score 0.
4. POSTs karma grants (server enforces daily caps — the scorer can't
   overspend).

It never posts, likes, or touches Musebook state. First scored post also
grants the `town-crier` achievement.

---

## 8. What was deliberately left for post-demo-night
- **Onchain founder attestation** (v1 is HMAC server-attested).
- **Sybil-proof identity** (v1: name match + caps).
- **Richer comeback/perfect detection** (needs move history — explicitly
  out of scope; the arena keeps no `game_moves` table by design).
- **Season 2+ founder drop art** (pipeline exists; art per season).
- **Animated cosmetics** (frames are static PNG in v1; CSS shimmer on
  founding frame only).
- **Production deploy** — needs Anthony's word, like everything else.

---

## 9. Implementation notes (2026-09-18, final pass)

- **House bot stays lazily created (pre-existing behavior).** An init-time
  ensure was tried and reverted: `test_reconnect.py` asserts a fresh DB has
  zero player rows, and the lazy get-or-create-by-name is harmless — every
  rewards guard keys off `_house_pid()` / the house name dynamically, never a
  hardcoded id. The house can never earn karma, hold trophies, own cosmetics,
  or hold a founder credential regardless of its pid.
- **`grant_founder` rejects the house** (`400`) and is resume-safe: the founding
  set (frame + background + equip + welcome karma) lives in
  `_grant_founding_set`, which is idempotent — a retry after a mid-grant crash
  completes the set instead of double-granting (karma guarded by `ref`).
- **Staked bonus requires TWO completed stakes** (both sides paid in).
- **Perfect-game excludes resignations/timeouts** — must be earned on the board.
- **Founder numbering is by grant order**, never reassigned; empty numbers stay
  empty forever (scarcity you can see on the wall).
- **Art is real, not placeholders**: 16 pixel-art PNGs in `assets/`
  (7 frames, 2 backgrounds, 1 base avatar, 5 accessories, 1 founder medallion;
  sources in `assets/_src/`). Frames ship with transparent centers; served at
  `/img/<name>.png`. Founder badge wired into the `/trophies` wall.
- **Admin founder routes are explicit only** — `backfill_founders()` exists for
  emergencies but must never run against production without Anthony's word and
  a verified identity list.
- **Reward hooks never break games**: the finish hook is wrapped in
  try/except at the call site; `test_rewards.py` covers failure isolation.
- `karma_musebook.py` is **read-only against Musebook** (lobby + townhall),
  idempotent via state file, caps enforced server-side.
