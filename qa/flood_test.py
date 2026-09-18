#!/usr/bin/env python3
"""Arena flood test (durable copy of the 2026-09-18 /tmp run).

Proves the money path under concurrency against a real in-process server:

  Part 1 — 15 simultaneous paid stakes across 15 games (x402 path, fake
           facilitator with a small settle delay to force overlap):
             * 15/15 HTTP 200
             * 15 stake rows, no duplicate stake_tx
             * zero orphan_payments rows
  Part 2 — same-player double-payment race (two payments, one game):
             * exactly one 200 and one 409 (order nondeterministic)
             * exactly one stake row for (game, player)
             * exactly one orphan_payments row carrying the LOSER's tx_hash,
               so the rejected payment is parked for refund, never invisible

Run:  <venv-python> qa/flood_test.py   (needs the x402 SDK installed)
Fails loudly. Read-only vs the real chain: the facilitator is faked.
"""
import base64
import itertools
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import app
import x402pay

x402pay.mainnet_ready = lambda: True

PORT = 8482
BASE = f"http://127.0.0.1:{PORT}"
N_GAMES = 15


class FakeFacilitator:
    """Settle succeeds after a short delay (forces thread overlap) and
    returns a UNIQUE tx_hash per call, like 15 distinct onchain payments."""

    def __init__(self):
        self._tx = itertools.count(1)
        self.settle_calls = 0
        self._lock = threading.Lock()

    def verify(self, payload, requirements):
        v = type("V", (), {})()
        v.is_valid = True
        try:
            v.payer = payload.payload["authorization"]["from"]
        except Exception:
            v.payer = ""
        return v

    def settle(self, payload, requirements):
        time.sleep(0.2)  # widen the race window
        with self._lock:
            self.settle_calls += 1
            n = next(self._tx)
        s = type("S", (), {})()
        s.success = True
        s.transaction = "0x" + format(n, "064x")
        try:
            s.payer = payload.payload["authorization"]["from"]
        except Exception:
            s.payer = ""
        s.network = "eip155:8453"
        s.amount = "1000000"
        return s


def call(method, path, data=None, headers=None):
    url = BASE + path
    body = json.dumps(data or {}).encode() if method == "POST" else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def expect(method, path, code, data=None, headers=None):
    status, body = call(method, path, data, headers)
    assert status == code, \
        f"{method} {path}: expected {code}, got {status} ({body})"
    return body


def main():
    from http.server import ThreadingHTTPServer
    from eth_account import Account
    from x402.mechanisms.evm.exact import ExactEvmScheme

    db = tempfile.mktemp(suffix=".db")
    app.Handler.arena = app.Arena(db)
    fake = FakeFacilitator()
    x402pay.FACILITATOR_OVERRIDE = fake
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        run(fake, db)
    finally:
        srv.shutdown()
        x402pay.FACILITATOR_OVERRIDE = None


def run(fake, db):
    arena = app.Handler.arena

    # --- setup: one room, 2*N players, N checkers games -------------------
    # register challengers + opponents
    from eth_account import Account
    players = []  # (token, name, account, player_id)
    for i in range(2 * N_GAMES):
        acct = Account.create()
        r = expect("POST", "/api/register", 200, {"name": f"FloodP{i}"})
        players.append((r["token"], f"FloodP{i}", acct, r["player_id"]))
    r = expect("POST", "/api/rooms", 200,
               {"token": players[0][0], "name": "Flood Room", "kind": "game"})
    rid = r["id"]
    for tok, _name, _a, _pid in players[1:]:
        expect("POST", f"/api/rooms/{rid}/join", 200, {"token": tok})
    games = []  # (game_id, challenger_idx)
    for i in range(N_GAMES):
        tok, name, _a, _p = players[2 * i]
        _t2, oname, _a2, _p2 = players[2 * i + 1]
        g = expect("POST", "/api/games", 200,
                   {"token": tok, "room_id": rid,
                    "kind": "checkers", "opponent": oname})
        games.append((g["id"], 2 * i))

    # --- 402 challenge shape (one unpaid probe) -----------------------------
    _s, _b = call("POST", "/api/stake",
                  {"token": players[0][0], "game_id": games[0][0],
                   "player_address": players[0][2].address})
    assert _s == 402, f"unpaid stake must 402, got {_s}"

    # --- Part 1: 15 simultaneous paid stakes --------------------------------
    # Build one real signed EIP-3009 payment header per player, using the
    # same requirements the server challenges with.
    from x402.mechanisms.evm.exact import ExactEvmScheme
    req = x402pay.stake_requirements()
    results = {}
    barrier = threading.Barrier(N_GAMES)

    def payment_header(acct):
        scheme = ExactEvmScheme(acct)
        inner = scheme.create_payment_payload(req)
        payload = {"x402Version": 2,
                   "accepted": json.loads(
                       req.model_dump_json(by_alias=True, exclude_none=True)),
                   "payload": inner}
        return base64.b64encode(json.dumps(payload).encode()).decode()

    def stake_one(i):
        tok, _name, acct, _pid = players[2 * i]
        gid, _ci = games[i]
        barrier.wait(timeout=30)
        st, body = call("POST", "/api/stake",
                        {"token": tok, "game_id": gid,
                         "player_address": acct.address},
                        headers={"X-Payment": payment_header(acct)})
        results[i] = (st, body)

    threads = [threading.Thread(target=stake_one, args=(i,))
               for i in range(N_GAMES)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    dt = time.time() - t0

    codes = sorted(st for st, _ in results.values())
    assert codes == [200] * N_GAMES, f"expected 15x200, got {codes}"
    rows = arena._rows("SELECT id, game_id, player_id, stake_tx, status"
                       " FROM stakes")
    assert len(rows) == N_GAMES, f"expected 15 rows, got {len(rows)}"
    txs = [r["stake_tx"] for r in rows]
    assert len(set(txs)) == N_GAMES, "duplicate stake_tx rows!"
    orphans = arena._rows("SELECT id FROM orphan_payments")
    assert len(orphans) == 0, f"unexpected orphans: {orphans}"
    print(f"PART 1 OK: 15/15 HTTP 200 in {dt:.2f}s, 15 rows, "
          f"no dup tx, no orphans")

    # --- Part 2: same-player double-payment race -----------------------------
    tok, _name, acct, pid0 = players[0]
    gid, _ci = games[0]
    # fresh game so the player has no stake on it yet
    _t2, oname, _a2, _p2 = players[1]
    g2 = expect("POST", "/api/games", 200,
                {"token": tok, "room_id": rid,
                 "kind": "checkers", "opponent": oname})
    gid2 = g2["id"]
    race = {}
    b2 = threading.Barrier(2)

    def stake_race(slot):
        b2.wait(timeout=30)
        st, body = call("POST", "/api/stake",
                        {"token": tok, "game_id": gid2,
                         "player_address": acct.address},
                        headers={"X-Payment": payment_header(acct)})
        race[slot] = (st, body)

    th = [threading.Thread(target=stake_race, args=(s,)) for s in (0, 1)]
    for t in th:
        t.start()
    for t in th:
        t.join(timeout=60)
    codes = sorted(st for st, _ in race.values())
    assert codes == [200, 409], f"race: expected one 200 + one 409, got {codes}"
    winner_tx = next(b["stake_tx"] for st, b in race.values() if st == 200)
    loser_tx = next(b.get("stake_tx", "") for st, b in race.values()
                    if st == 409) or None
    srows = arena._rows("SELECT stake_tx FROM stakes WHERE game_id=?"
                        " AND player_id=?", (gid2, pid0))
    assert len(srows) == 1, f"race: expected 1 stake row, got {len(srows)}"
    assert srows[0]["stake_tx"] == winner_tx
    orows = arena._rows("SELECT tx_hash, reason, amount_units"
                        " FROM orphan_payments")
    assert len(orows) == 1, \
        f"race: loser payment must be parked exactly once, got {orows}"
    assert orows[0]["amount_units"] == 1_000_000
    print(f"PART 2 OK: race -> one 200 + one 409, 1 stake row, "
          f"loser parked in orphan_payments ({orows[0]['reason']})")

    print("ALL FLOOD TESTS PASSED")


if __name__ == "__main__":
    main()
