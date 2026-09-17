#!/usr/bin/env python3
"""Settle completed staked games — pays winners / refunds from the house wallet.

Reads stakes with status='complete' from the arena DB, then:

  * winner (2 stakes):           winner gets 1.90 USDC, $0.10 stays as rake
  * draw (2 stakes):             each player refunded 1.00 USDC, no rake
  * single stake, game complete: that player refunded 1.00 USDC

Payouts are plain ERC20 transfer() calls on Base mainnet, signed locally
with the mission wallet key. The key is read from disk and NEVER printed,
logged, or committed.

DRY-RUN IS THE DEFAULT: with no flags the script only prints what it WOULD
do. Pass --live to actually broadcast transactions.

Run (dry run):
    python3 payouts/settle.py

Run (for real):
    python3 payouts/settle.py --live

The script needs the arena DB: --db path for SQLite, or DATABASE_URL env
for Postgres (production). It needs outbound HTTPS to a Base RPC.
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

DEFAULT_KEY_PATH = os.path.join(
    os.path.expanduser("~"),
    "workspace/goals/5k-online-income-project/x402-seller/"
    "hidden_files/hidden_mainnet_buyer.key",
)
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CHAIN_ID = 8453
STAKE_UNITS = 1_000_000      # $1.00
WIN_PAYOUT_UNITS = 1_900_000  # $1.90 (winner); $0.10 rake stays in the wallet
# v1.5 tournament pot: pays out at $50. Winner takes 90%, house keeps 10%.
# (50_000_000 units = $50.00; 90% = 45_000_000; 10% = 5_000_000.)
TOURNAMENT_TARGET_UNITS = 50_000_000
TOURNAMENT_WIN_BPS = 9000
RPC_URLS = [
    "https://mainnet.base.org",
    "https://base.llamarpc.com",
    "https://base.meowrpc.com",
]
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def fmt_usd(units):
    return f"${units / 1_000_000:,.2f}"


# ---------------------------------------------------------------- payouts
def compute_payouts(winner_id, stakes):
    """Pure payout math.

    stakes: list of dicts with player_id, player_address, amount_units.
    winner_id: arena player id of the winner, or None on a draw.
    Returns [(player_address, amount_units, kind)] where kind is
    'win' | 'draw_refund' | 'refund'. All math in integer base units.
    """
    # A single stake can never produce a winner payout: one player cannot
    # win a pot they alone funded, even if the board game itself has a
    # winner. Always a 1:1 refund.
    if len(stakes) == 1:
        s = stakes[0]
        return [(s["player_address"], s["amount_units"], "refund")]
    if winner_id is not None:
        addrs = {s["player_id"]: s["player_address"] for s in stakes}
        if winner_id not in addrs:
            raise ValueError(f"winner {winner_id} has no recorded stake")
        return [(addrs[winner_id], WIN_PAYOUT_UNITS, "win")]
    kind = "draw_refund" if len(stakes) > 1 else "refund"
    return [(s["player_address"], s["amount_units"], kind) for s in stakes]


def compute_tournament_payouts(entries, winner_id, pot_units):
    """Pure tournament payout math — all in integer base units.

    entries: list of dicts with player_id, player_address, amount_units.
    winner_id: arena player id of the tournament winner, or None when no
    entrant won a tournament game (degenerate case).
    Returns [(player_address, amount_units, kind)] where kind is
    'tournament_win' | 'tournament_refund'.
    Winner takes exactly 90% of the actual pot; the house keeps the rest.
    No winner -> every entry refunded 1:1, house takes nothing.
    """
    if winner_id is not None:
        addrs = {e["player_id"]: e["player_address"] for e in entries}
        if winner_id not in addrs:
            raise ValueError(f"tournament winner {winner_id} has no entry")
        win_units = pot_units * TOURNAMENT_WIN_BPS // 10000
        return [(addrs[winner_id], win_units, "tournament_win")]
    return [(e["player_address"], e["amount_units"], "tournament_refund")
            for e in entries]


# ---------------------------------------------------------------- db
def open_db(db_path):
    from app import Arena  # reuse the arena's storage layer (sqlite + pg)

    return Arena(db_path)


# Games whose payouts were executed MANUALLY (direct onchain transfer, not
# via this script), so their stakes rows still read status='complete' with
# payout_tx NULL. They must NEVER be paid again by --live.
# game_id -> (payout_tx, note)
MANUAL_SETTLEMENTS = {
    5: ("0x0000000000000000000000000000000000000000000000000000000000000000",
        "internal self-test; payout deliberately never broadcast; nobody owed"),
    8: ("0xce3c74c132000723e3f71b1013733912525928a7dc19e2aaa247894036b6d937",
        "single-stake $1.00 refund broadcast manually 2026-09-17"),
    14: ("0xd64255bc1e2642228dc06668e30f3eb4fac2bcb0203281e47b5c24a8652e3f2c",
        "two-player winner $1.90 payout broadcast manually 2026-09-17"),
}


def load_settlements(arena):
    """Group complete stakes by game. Returns (settlements, skipped) where
    skipped lists game_ids excluded via MANUAL_SETTLEMENTS."""
    rows = arena._rows(
        "SELECT s.game_id, s.player_id, s.player_address, s.amount_units,"
        " s.status, s.stake_tx, s.winner_id, b.winner_id AS game_winner,"
        " b.kind AS game_kind, b.status AS game_status"
        " FROM stakes s JOIN board_games b ON b.id=s.game_id"
        " WHERE s.status='complete' ORDER BY s.game_id, s.created_at"
    )
    games = {}
    for r in rows:
        r = dict(r)
        games.setdefault(r["game_id"], []).append(r)
    out, skipped = [], []
    for gid, stakes in games.items():
        if gid in MANUAL_SETTLEMENTS:
            skipped.append(gid)
            continue
        g = stakes[0]
        if g["game_status"] != "finished":
            continue  # shouldn't happen; payout only finished games
        payouts = compute_payouts(g["game_winner"], stakes)
        out.append({"game_id": gid, "game_kind": g["game_kind"],
                    "winner_id": g["game_winner"], "stakes": stakes,
                    "payouts": payouts})
    return out, skipped


def load_tournament_settlement(arena):
    """Load the closed tournament pot, if any. Returns None when the
    tournament is still open (or already settled)."""
    t = arena._row("SELECT * FROM tournament WHERE id=1")
    if not t or dict(t)["status"] != "closed":
        return None
    t = dict(t)
    entries = [dict(r) for r in arena._rows(
        "SELECT player_id, player_address, amount_units, status"
        " FROM tournament_entries WHERE status='closed'"
        " ORDER BY created_at, id")]
    if not entries:
        return None
    pot = sum(e["amount_units"] for e in entries)
    payouts = compute_tournament_payouts(entries, t["winner_id"], pot)
    return {"winner_id": t["winner_id"], "entries": entries,
            "pot_units": pot, "payouts": payouts}


# ---------------------------------------------------------------- chain
def rpc_call(payload, timeout=30):
    import httpx

    last = None
    for url in RPC_URLS:
        try:
            r = httpx.post(url, json=payload,
                           headers={"User-Agent": BROWSER_UA}, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                last = RuntimeError(f"{url}: {data['error']}")
                continue
            return data["result"]
        except Exception as e:
            last = e
            # httpx 0.28 chokes on some IPv6 resolutions
            # ("Invalid port: ':1]'"); curl handles them fine.
            try:
                import subprocess as _sp
                import json as _json
                out = _sp.run(
                    ["curl", "-s", "-m", str(timeout), "-X", "POST", url,
                     "-H", "Content-Type: application/json",
                     "-H", f"User-Agent: {BROWSER_UA}",
                     "-d", _json.dumps(payload)],
                    capture_output=True, text=True, timeout=timeout + 5)
                data = _json.loads(out.stdout)
                if "error" in data:
                    last = RuntimeError(f"{url}: {data['error']}")
                    continue
                return data["result"]
            except Exception as e2:
                last = e2
    raise RuntimeError(f"all Base RPCs failed: {last}")


def transfer_data(to_addr, amount_units):
    sel = "a9059cbb"  # transfer(address,uint256)
    return ("0x" + sel + to_addr[2:].lower().zfill(64)
            + format(amount_units, "064x"))


def send_usdc(key_hex, to_addr, amount_units, dry_run=True):
    """Build, sign and (unless dry_run) broadcast a USDC transfer."""
    from eth_account import Account

    acct = Account.from_key(key_hex)
    sender = acct.address
    gas_price = int(rpc_call({"jsonrpc": "2.0", "id": 1,
                              "method": "eth_gasPrice", "params": []}), 16)
    nonce = int(rpc_call({"jsonrpc": "2.0", "id": 2, "method":
                          "eth_getTransactionCount",
                          "params": [sender, "pending"]}), 16)
    tx = {
        "chainId": CHAIN_ID,
        "nonce": nonce,
        "to": USDC_BASE,
        "value": 0,
        "data": transfer_data(to_addr, amount_units),
        "gas": 70000,
        "maxFeePerGas": gas_price,
        "maxPriorityFeePerGas": min(gas_price, 1_500_000_000),
    }
    signed = acct.sign_transaction(tx)
    raw = "0x" + signed.raw_transaction.hex()
    desc = f"{fmt_usd(amount_units)} USDC -> {to_addr} (from {sender})"
    if dry_run:
        return {"dry_run": True, "desc": desc, "tx": tx}
    tx_hash = rpc_call({"jsonrpc": "2.0", "id": 3, "method":
                        "eth_sendRawTransaction", "params": [raw]})
    return {"dry_run": False, "desc": desc, "tx_hash": tx_hash}


def wait_receipt(tx_hash, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = rpc_call({"jsonrpc": "2.0", "id": 4, "method":
                      "eth_getTransactionReceipt", "params": [tx_hash]})
        if r:
            return r
        time.sleep(4)
    raise RuntimeError(f"no receipt for {tx_hash} after {timeout}s")


# ---------------------------------------------------------------- remote admin
# Production (Render free plan) has no shell, so --remote talks to the
# token-gated admin API in app.py instead of touching the DB directly.
# Money still moves ONLY from this machine: payouts are broadcast here with
# the local mission key, and the API merely records the mined tx hashes.
ADMIN_BASE_URL = os.environ.get("ARENA_BASE_URL",
                                "https://muse-arena.onrender.com")
ADMIN_TOKEN_PATH = os.path.join(REPO, "hidden_files", "admin_token.txt")


def resolve_admin_token():
    t = os.environ.get("ARENA_ADMIN_TOKEN", "").strip()
    if not t and os.path.exists(ADMIN_TOKEN_PATH):
        with open(ADMIN_TOKEN_PATH, encoding="utf-8") as f:
            t = f.read().strip()
    return t


def admin_call(method, path, token, payload=None):
    """HTTPS to the arena admin API via curl (browser UA — some networks
    403 bare-Python HTTP clients). Raises on any API error."""
    import subprocess
    import json as _json

    url = ADMIN_BASE_URL.rstrip("/") + path
    cmd = ["curl", "-s", "-m", "40", "-X", method, url,
           "-H", f"Authorization: Bearer {token}",
           "-H", "Content-Type: application/json",
           "-H", f"User-Agent: {BROWSER_UA}"]
    if payload is not None:
        cmd += ["-d", _json.dumps(payload)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        data = _json.loads(out.stdout or "{}")
    except _json.JSONDecodeError:
        raise RuntimeError(f"admin {path}: non-JSON response "
                           f"(HTTP via curl failed?)")
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"admin {path}: {data['error']}")
    return data


def remote_pending(token):
    return admin_call("GET", "/api/admin/stakes/pending", token)["pending"]


def remote_record(token, settlements):
    return admin_call("POST", "/api/admin/settle", token,
                      {"settlements": settlements})


def _record_or_warn(token, settlements, context):
    """Record settlements; a failure AFTER a broadcast is critical — the
    money moved but the ledger doesn't know. Never silently continue."""
    try:
        return remote_record(token, settlements)
    except RuntimeError as e:
        print(f"[settle] CRITICAL: {context} — payout state NOT recorded: "
              f"{e}", file=sys.stderr)
        print("[settle] the chain moved; re-run to reconcile (recorded rows "
              "are skipped, so nothing double-pays)", file=sys.stderr)
        return None


def run_remote(live, record_game=None, record_tx=None, void_game=None,
               void_reason=None):
    """Remote settlement against production via the admin API."""
    token = resolve_admin_token()
    if not token:
        print("[settle] ERROR: no admin token — set ARENA_ADMIN_TOKEN or "
              f"write {ADMIN_TOKEN_PATH}", file=sys.stderr)
        return 1
    if void_game is not None:
        if not void_reason:
            print("[settle] ERROR: --void needs --reason", file=sys.stderr)
            return 1
        try:
            res = admin_call("POST", "/api/admin/stakes/void", token,
                             {"game_id": void_game, "reason": void_reason})
        except RuntimeError as e:
            print(f"[settle] ERROR: {e}", file=sys.stderr)
            return 1
        print(f"[settle] voided game #{void_game}: "
              f"{res['stakes_voided']} stake(s) — {res['reason']}")
        return 0
    try:
        pending = remote_pending(token)
    except RuntimeError as e:
        print(f"[settle] ERROR: {e}", file=sys.stderr)
        print("[settle] (is the new code deployed AND ADMIN_TOKEN set on "
              "Render?)", file=sys.stderr)
        return 1

    if record_game is not None:
        # Record an already-broadcast manual payout (e.g. Game 14's $1.90).
        # Explicit: bypasses MANUAL_SETTLEMENTS, never broadcasts.
        game = next((g for g in pending if g["game_id"] == record_game), None)
        if game is None:
            print(f"[settle] game #{record_game} has no unsettled stakes — "
                  "already recorded?", file=sys.stderr)
            return 1
        payable = [p for p in game["payouts"] if p["kind"] != "no_payout"]
        if len(payable) != 1:
            print(f"[settle] ERROR: --record needs exactly one payout; "
                  f"game #{record_game} has {len(payable)}", file=sys.stderr)
            return 1
        p = payable[0]
        result = "paid" if p["kind"] == "win" else "refunded"
        settlements = [{"stake_id": p["stake_id"], "payout_tx": record_tx,
                        "result": result}]
        for q in game["payouts"]:
            if q["kind"] == "no_payout":
                settlements.append({"stake_id": q["stake_id"],
                                    "payout_tx": None, "result": "no_payout"})
        res = _record_or_warn(token, settlements,
                                f"game #{record_game} manual record")
        if res is None:
            return 1
        print(f"[settle] recorded game #{record_game}: "
              f"{res['stakes_settled']} stake(s) settled, tx={record_tx}")
        return 0

    games = [g for g in pending if g["game_id"] not in MANUAL_SETTLEMENTS]
    skipped = sorted(g["game_id"] for g in pending
                     if g["game_id"] in MANUAL_SETTLEMENTS)
    print(f"[settle] remote mode: {'DRY-RUN' if not live else 'LIVE'} "
          f"against {ADMIN_BASE_URL}")
    print(f"[settle] games to settle: {len(games)}")
    if skipped:
        print(f"[settle] skipped {len(skipped)} manually-settled game(s) "
              f"(never auto-pay): {skipped}")
    total_out = 0
    for g in games:
        print(f"\ngame #{g['game_id']} ({g['game_kind']}) "
              f"winner_id={g['game_winner']}")
        for p in g["payouts"]:
            if p["kind"] != "no_payout":
                total_out += p["amount_units"]
            print(f"  {p['kind']:11s} "
                  f"{('$%0.2f' % (p['amount_units']/1_000_000)):>8s} "
                  f"-> {p['player_name']} ({p['to']}) stake={p['stake_id']}")
    print(f"\ntotal outflow: {fmt_usd(total_out)} USDC "
          f"({len(games)} game(s))")
    if not games:
        print("[settle] nothing to do.")
        return 0
    if not live:
        print("\n[settle] dry run complete — no transactions broadcast, "
              "nothing recorded.")
        print("[settle] to execute for real, run with --live.")
        return 0

    print("\n[settle] LIVE — broadcasting payouts from the mission wallet.")
    key_path = os.environ.get("STAKE_PAYOUT_KEY", DEFAULT_KEY_PATH)
    with open(key_path, encoding="utf-8") as f:
        key_hex = f.read().strip()  # never printed or logged
    if not key_hex:
        print("[settle] ERROR: key file is empty", file=sys.stderr)
        return 1
    for g in games:
        # losers first (no chain interaction), then pay + record each
        # payout the moment it mines — a crash mid-game never double-pays
        # on retry because recorded stakes leave the pending list.
        for p in g["payouts"]:
            if p["kind"] == "no_payout":
                res = _record_or_warn(
                    token, [{"stake_id": p["stake_id"],
                             "payout_tx": None, "result": "no_payout"}],
                    f"game #{g['game_id']} loser close")
                if res is not None:
                    print(f"[settle] game #{g['game_id']} loser "
                          f"{p['player_name']} closed (no payout)")
        for p in g["payouts"]:
            if p["kind"] == "no_payout":
                continue
            res = send_usdc(key_hex, p["to"], p["amount_units"],
                            dry_run=False)
            print(f"[settle] broadcast {res['desc']} tx={res['tx_hash']}")
            receipt = wait_receipt(res["tx_hash"])
            ok = receipt.get("status") == "0x1"
            print(f"[settle]   mined: {res['tx_hash']} "
                  f"status={'1 ok' if ok else '0 FAILED'}")
            if not ok:
                print("[settle]   NOT recording — investigate before "
                      "retrying", file=sys.stderr)
                continue
            result = "paid" if p["kind"] == "win" else "refunded"
            r = _record_or_warn(
                token, [{"stake_id": p["stake_id"],
                         "payout_tx": res["tx_hash"], "result": result}],
                f"game #{g['game_id']} stake {p['stake_id']} ({res['tx_hash']})")
            if r is not None:
                print(f"[settle]   recorded: {r['stakes_settled']} stake(s) "
                      f"-> {result}")
    print("\n[settle] remote settlement run complete.")
    return 0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Settle completed staked arena games")
    ap.add_argument("--db", default=os.environ.get(
        "ARENA_DB", os.path.join(REPO, "arena.db")),
        help="SQLite path (ignored when DATABASE_URL is set)")
    ap.add_argument("--key", default=os.environ.get("STAKE_PAYOUT_KEY",
                                                    DEFAULT_KEY_PATH),
                    help="path to the mission wallet private key file")
    ap.add_argument("--live", action="store_true",
                    help="actually broadcast payout transactions "
                         "(default is --dry-run)")
    ap.add_argument("--remote", action="store_true",
                    help="settle PRODUCTION via the token-gated admin API "
                         "(no DB shell needed) instead of the local DB")
    ap.add_argument("--record", type=int, default=None, metavar="GAME_ID",
                    help="with --remote: only RECORD an already-broadcast "
                         "manual payout for GAME_ID (never broadcasts)")
    ap.add_argument("--tx", default=None,
                    help="tx hash for --record")
    ap.add_argument("--void", type=int, default=None, metavar="GAME_ID",
                    help="with --remote: VOID the pending/active stakes of an "
                         "unfinished GAME_ID (test artifacts, abandoned "
                         "matches — never pays)")
    ap.add_argument("--reason", default=None,
                    help="audit reason for --void")
    args = ap.parse_args()
    dry_run = not args.live

    if args.remote:
        return run_remote(live=args.live, record_game=args.record,
                          record_tx=args.tx, void_game=args.void,
                          void_reason=args.reason)

    arena = open_db(args.db)
    settlements, skipped = load_settlements(arena)
    tsettle = load_tournament_settlement(arena)
    orphans = [dict(r) for r in arena._rows(
        "SELECT * FROM orphan_payments ORDER BY created_at DESC LIMIT 50")]
    torphans = [dict(r) for r in arena._rows(
        "SELECT * FROM tournament_orphans ORDER BY created_at DESC LIMIT 50")]

    print(f"[settle] mode: {'DRY-RUN (no transactions broadcast)' if dry_run else 'LIVE'}")
    print(f"[settle] games to settle: {len(settlements)}")
    if skipped:
        print(f"[settle] skipped {len(skipped)} manually-settled game(s) "
              f"(never auto-pay): {sorted(skipped)}")
        for gid in sorted(skipped):
            tx, note = MANUAL_SETTLEMENTS[gid]
            print(f"  game #{gid}: {note} tx={tx}")
    total_out = 0
    for s in settlements:
        print(f"\ngame #{s['game_id']} ({s['game_kind']}) "
              f"winner_id={s['winner_id']}")
        for addr, units, kind in s["payouts"]:
            total_out += units
            print(f"  {kind:11s} {fmt_usd(units):>8s} -> {addr}")
    if tsettle:
        print(f"\n[settle] TOURNAMENT: pot {fmt_usd(tsettle['pot_units'])} "
              f"({len(tsettle['entries'])} entries) "
              f"winner_id={tsettle['winner_id']}")
        for addr, units, kind in tsettle["payouts"]:
            total_out += units
            print(f"  {kind:16s} {fmt_usd(units):>8s} -> {addr}")
    print(f"\ntotal outflow: {fmt_usd(total_out)} USDC "
          f"({len(settlements)} game(s)"
          f"{' + tournament' if tsettle else ''})")
    if orphans:
        print(f"\n[settle] WARNING: {len(orphans)} orphan payment(s) need MANUAL refund:")
        for o in orphans:
            print(f"  game #{o['game_id']} {fmt_usd(o['amount_units'])} "
                  f"payer={o['payer']} tx={o['tx_hash']} ({o['reason']})")
    if torphans:
        print(f"\n[settle] WARNING: {len(torphans)} tournament orphan(s) need MANUAL refund:")
        for o in torphans:
            print(f"  {fmt_usd(o['amount_units'])} "
                  f"payer={o['payer']} tx={o['tx_hash']} ({o['reason']})")

    if not settlements and not tsettle:
        print("[settle] nothing to do.")
        return 0

    if dry_run:
        print("\n[settle] dry run complete — no transactions broadcast, DB untouched.")
        print("[settle] to execute for real, run:")
        print(f"[settle]   python3 payouts/settle.py --live --db {args.db}")
        return 0

    print("\n[settle] LIVE — broadcasting payouts from the mission wallet.")
    with open(args.key, encoding="utf-8") as f:
        key_hex = f.read().strip()  # never printed or logged
    if not key_hex:
        print("[settle] ERROR: key file is empty", file=sys.stderr)
        return 1

    for s in settlements:
        for addr, units, kind in s["payouts"]:
            res = send_usdc(key_hex, addr, units, dry_run=False)
            print(f"[settle] broadcast {res['desc']} tx={res['tx_hash']}")
            receipt = wait_receipt(res["tx_hash"])
            ok = receipt.get("status") == "0x1"
            print(f"[settle]   mined: {res['tx_hash']} status={'1 ok' if ok else '0 FAILED'}")
            if not ok:
                print("[settle]   NOT marking paid — investigate before retrying",
                      file=sys.stderr)
                continue
            new_status = "paid" if kind == "win" else "refunded"
            arena._q("UPDATE stakes SET status=?, payout_tx=? "
                     "WHERE game_id=? AND status='complete'",
                     (new_status, res["tx_hash"], s["game_id"]))
            print(f"[settle]   game #{s['game_id']} stakes -> {new_status}")
    if tsettle:
        print("\n[settle] LIVE — broadcasting the tournament payout.")
        done = []  # (player_address, tx_hash, kind) — only mined-success
        for addr, units, kind in tsettle["payouts"]:
            res = send_usdc(key_hex, addr, units, dry_run=False)
            print(f"[settle] broadcast {res['desc']} tx={res['tx_hash']}")
            receipt = wait_receipt(res["tx_hash"])
            ok = receipt.get("status") == "0x1"
            print(f"[settle]   mined: {res['tx_hash']} status={'1 ok' if ok else '0 FAILED'}")
            if not ok:
                print("[settle]   NOT marking paid — investigate before retrying",
                      file=sys.stderr)
                continue
            done.append((addr, res["tx_hash"], kind))
        # ledger updates only for mined-success receipts, matched by address
        for addr, tx_hash, kind in done:
            new_status = "paid" if kind == "tournament_win" else "refunded"
            arena._q("UPDATE tournament_entries SET status=?, payout_tx=?"
                     " WHERE player_address=? AND status='closed'",
                     (new_status, tx_hash, addr))
            print(f"[settle]   tournament entry {addr} -> {new_status}")
        if len(done) == len(tsettle["payouts"]):
            arena._q("UPDATE tournament SET status='settled' "
                     "WHERE id=1 AND status='closed'")
            print("[settle]   tournament settled")
        else:
            print("[settle]   WARNING: some tournament payouts failed — "
                  "tournament left 'closed' for manual follow-up",
                  file=sys.stderr)
    print("[settle] done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
