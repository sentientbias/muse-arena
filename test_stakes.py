#!/usr/bin/env python3
"""Staked matches v1.4 tests: boots a real arena server in-process, then
exercises the $1 USDC stake flow end-to-end over HTTP.

Covers:
  * pre-payment input validation (400/401/403/404/409, no charge)
  * 402 challenge shape (decodable by a real x402 client)
  * paid stake via a fake facilitator (verify+settle path, stake recorded)
  * double-stake prevention (no second charge)
  * ledger transitions: pending -> active -> complete (winner recorded)
  * staked badge in /api/games, /api/spectate, /watch data
  * payout math: win / draw / single-stake refund (exact base units)
  * payout script dry-run against the test DB (no broadcast, no writes)

Run:  <venv-python> test_stakes.py   (needs the x402 SDK installed)
"""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app
import x402pay

# --- test doubles -----------------------------------------------------------
# Never let tests fail closed on payment-backend config.
x402pay.mainnet_ready = lambda: True


class FakeVerify:
    is_valid = True
    invalid_reason = None
    invalid_message = None
    payer = ""


class FakeSettle:
    success = True
    error_reason = None
    error_message = None
    transaction = "0x" + "ab" * 32
    payer = ""
    network = "eip155:8453"
    amount = "1000000"


class FakeFacilitator:
    def __init__(self):
        self.verify_calls = 0
        self.settle_calls = 0

    def verify(self, payload, requirements):
        self.verify_calls += 1
        v = FakeVerify()
        try:
            v.payer = payload.payload["authorization"]["from"]
        except Exception:
            v.payer = ""
        return v

    def settle(self, payload, requirements):
        self.settle_calls += 1
        s = FakeSettle()
        try:
            s.payer = payload.payload["authorization"]["from"]
        except Exception:
            s.payer = ""
        return s


# --- http helpers ------------------------------------------------------------
PORT = 8481
BASE = f"http://127.0.0.1:{PORT}"


def call(method, path, data=None, headers=None):
    url = BASE + path
    body = None
    if method == "GET" and data:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(data)
    elif method == "POST":
        body = json.dumps(data or {}).encode()
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read().decode())


def expect(method, path, code, data=None, headers=None):
    status, hdrs, body = call(method, path, data, headers)
    assert status == code, f"{method} {path}: expected {code}, got {status} ({body})"
    return hdrs, body


def main():
    from http.server import ThreadingHTTPServer

    global TEST_DB
    TEST_DB = tempfile.mktemp(suffix=".db")
    app.Handler.arena = app.Arena(TEST_DB)
    fake = FakeFacilitator()
    x402pay.FACILITATOR_OVERRIDE = fake
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), app.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        return run_tests(fake)
    finally:
        srv.shutdown()
        x402pay.FACILITATOR_OVERRIDE = None


TEST_DB = None


def run_tests(fake):
    # --- setup: two muses, one room, one checkers game ---
    _, mikey = expect("POST", "/api/register", 200, {"name": "StakeMikey"})
    _, dash = expect("POST", "/api/register", 200, {"name": "StakeDash"})
    _, kwanza = expect("POST", "/api/register", 200, {"name": "StakeKwanza"})
    _, room = expect("POST", "/api/rooms", 200,
                     {"token": mikey["token"], "name": "Stake Room", "kind": "game"})
    rid = room["id"]
    expect("POST", f"/api/rooms/{rid}/join", 200, {"token": dash["token"]})
    _, game = expect("POST", "/api/games", 200,
                     {"token": mikey["token"], "room_id": rid,
                      "kind": "checkers", "opponent": "StakeDash"})
    gid = game["id"]

    # real throwaway keys: each player's stake address is a wallet they sign with
    from eth_account import Account
    from x402.mechanisms.evm.exact import ExactEvmScheme
    k1, k2 = Account.create(), Account.create()
    A1, A2 = k1.address, k2.address

    # --- 1. pre-payment validation: bad input never gets a 402 ---
    expect("POST", "/api/stake", 401, {"game_id": gid, "player_address": A1})
    expect("POST", "/api/stake", 400,
           {"token": mikey["token"], "player_address": A1})  # no game_id
    expect("POST", "/api/stake", 400,
           {"token": mikey["token"], "game_id": gid,
            "player_address": "not-an-address"})
    expect("POST", "/api/stake", 404,
           {"token": mikey["token"], "game_id": 999999, "player_address": A1})
    expect("POST", "/api/stake", 403,  # kwanza is not in this game
           {"token": kwanza["token"], "game_id": gid, "player_address": A1})
    assert fake.settle_calls == 0, "no payment should have been attempted"

    # --- 2. unpaid -> 402 with a decodable x402 v2 challenge ---
    hdrs, body = expect("POST", "/api/stake", 402,
                        {"token": mikey["token"], "game_id": gid,
                         "player_address": A1})
    assert "PAYMENT-REQUIRED" in hdrs, f"missing 402 header: {hdrs.keys()}"
    pr = x402pay.decode_payment_required_header(hdrs["PAYMENT-REQUIRED"])
    req = pr.accepts[0]
    assert req.scheme == "exact", req.scheme
    assert req.network == "eip155:8453", req.network
    assert req.amount == "1000000", req.amount
    assert req.asset.lower() == x402pay.USDC_BASE.lower(), req.asset
    assert req.pay_to.lower() == x402pay.PAY_TO.lower(), req.pay_to
    assert body["price_units"] == 1_000_000

    # --- 3. garbage payment header -> 402, not 500 ---
    expect("POST", "/api/stake", 402,
           {"token": mikey["token"], "game_id": gid, "player_address": A1},
           headers={"X-Payment": "garbage!!!"})
    assert fake.settle_calls == 0

    # --- helper: build a real signed EIP-3009 payment header ---
    def payment_header(acct):
        scheme = ExactEvmScheme(acct)
        inner = scheme.create_payment_payload(req)
        payload = {"x402Version": 2,
                   "accepted": json.loads(
                       req.model_dump_json(by_alias=True, exclude_none=True)),
                   "payload": inner}
        return base64.b64encode(json.dumps(payload).encode()).decode()

    # --- 4. paid stake: player 1 ---
    hdrs, stake1 = expect("POST", "/api/stake", 200,
                          {"token": mikey["token"], "game_id": gid,
                           "player_address": A1},
                          headers={"X-Payment": payment_header(k1)})
    assert stake1["status"] == "pending", stake1
    assert stake1["game_staked"] is False
    assert stake1["stake_tx"] == FakeSettle.transaction
    assert fake.settle_calls == 1

    # --- 5. double-stake -> 409 BEFORE any second payment ---
    before = fake.settle_calls
    expect("POST", "/api/stake", 409,
           {"token": mikey["token"], "game_id": gid, "player_address": A1},
           headers={"X-Payment": payment_header(k1)})
    assert fake.settle_calls == before, "double-stake must not charge again"

    # --- 6. paid stake: player 2 -> game becomes staked ---
    _, stake2 = expect("POST", "/api/stake", 200,
                       {"token": dash["token"], "game_id": gid,
                        "player_address": A2},
                       headers={"PAYMENT-SIGNATURE": payment_header(k2)})
    assert stake2["status"] == "active", stake2
    assert stake2["game_staked"] is True

    # --- 7. badges: game state, spectate, public board ---
    _, gs = expect("GET", f"/api/games/{gid}", 200, {"token": mikey["token"]})
    assert gs["staked"] is True and gs["stake_pot_units"] == 2_000_000, gs
    _, spec = expect("GET", "/api/spectate", 200)
    board = next(b for b in spec["boards"] if b["id"] == gid)
    assert board["staked"] is True and board["stake_pot_units"] == 2_000_000
    _, stakes = expect("GET", "/api/stakes", 200)
    assert len(stakes["stakes"]) == 2
    assert all(s["status"] == "active" for s in stakes["stakes"])
    assert all(s["amount_units"] == 1_000_000 for s in stakes["stakes"])

    # --- 8. finish the game -> stakes complete with winner ---
    _, res = expect("POST", f"/api/games/{gid}/resign", 200,
                    {"token": dash["token"]})
    assert res["winner"] == "StakeMikey", res
    _, stakes = expect("GET", "/api/stakes", 200)
    assert all(s["status"] == "complete" for s in stakes["stakes"])
    winner_id = next(s["player_id"] for s in stakes["stakes"]
                     if s["player_name"] == "StakeMikey")
    assert all(s["winner_id"] == winner_id for s in stakes["stakes"])

    # --- 9. staking a finished game -> 400 (no charge) ---
    before = fake.settle_calls
    expect("POST", "/api/stake", 400,
           {"token": mikey["token"], "game_id": gid, "player_address": A1},
           headers={"X-Payment": payment_header(k1)})
    assert fake.settle_calls == before

    # --- 10. payout math (exact base units, no floats) ---
    spec_mod = importlib.util.spec_from_file_location(
        "settle", os.path.join(HERE, "payouts", "settle.py"))
    settle = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(settle)
    srows = [{"player_id": 1, "player_address": A1, "amount_units": 1_000_000},
             {"player_id": 2, "player_address": A2, "amount_units": 1_000_000}]
    # win: winner takes $1.90, $0.10 rake stays
    assert settle.compute_payouts(1, srows) == [(A1, 1_900_000, "win")]
    # draw: both refunded $1.00, no rake
    assert settle.compute_payouts(None, srows) == [
        (A1, 1_000_000, "draw_refund"), (A2, 1_000_000, "draw_refund")]
    # single stake on a finished game: refund
    assert settle.compute_payouts(None, srows[:1]) == [(A1, 1_000_000, "refund")]
    # single stake + board-game winner: STILL a 1:1 refund, never a $1.90 win
    assert settle.compute_payouts(1, srows[:1]) == [(A1, 1_000_000, "refund")]
    # totals balance: win case pays 1.9M of a 2.0M pot
    assert sum(u for _, u, _ in settle.compute_payouts(2, srows)) == 1_900_000
    assert sum(u for _, u, _ in settle.compute_payouts(None, srows)) == 2_000_000

    # --- 11. payout script dry-run: plans the $1.90 payout, touches nothing ---
    import subprocess
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, "payouts", "settle.py"),
         "--db", TEST_DB],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "$1.90" in r.stdout, r.stdout
    assert A1 in r.stdout, r.stdout
    assert "DRY-RUN" in r.stdout
    # dry run must not have marked anything paid
    _, stakes = expect("GET", "/api/stakes", 200)
    assert all(s["status"] == "complete" for s in stakes["stakes"])
    assert all(not s["payout_tx"] for s in stakes["stakes"])

    print("ALL STAKE TESTS PASSED")


if __name__ == "__main__":
    main()
