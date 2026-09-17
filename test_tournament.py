#!/usr/bin/env python3
"""Tournament pot v1.5 tests: boots a real arena server in-process, then
exercises the $1 USDC tournament-entry flow end-to-end over HTTP, plus
direct Arena-level tests for close/winner/tiebreak/refund logic.

Covers:
  * pre-payment input validation (401/400/409, no charge)
  * 402 challenge shape (decodable by a real x402 client, $1.00 exact)
  * paid entry via a fake facilitator (entry recorded, pot grows)
  * double-entry prevention (no second charge)
  * live pot in /api/tournament, /api/spectate, game payloads, /watch
  * close-at-target: pot hits the target -> tournament closes, no more entries
  * winner = most wins vs other entrants; tiebreaks: fewest losses,
    then earliest entry (deterministic)
  * non-entrant games and draws don't count toward standings
  * no-decisive-games -> refund all 1:1, no rake (never strands money)
  * payout math in exact base units (90% win / 10% house / full refunds)
  * payout script dry-run against a closed-tournament DB (no broadcast, no writes)

Run:  <venv-python> test_tournament.py   (needs the x402 SDK installed)
"""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import threading
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
    transaction = "0x" + "cd" * 32
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
PORT = 8482
BASE = f"http://127.0.0.1:{PORT}"


def call(method, path, data=None, headers=None, raw=False):
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
            text = r.read().decode()
            return r.status, dict(r.headers), text if raw else json.loads(text)
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"_raw": text}
        return e.code, dict(e.headers), parsed


def expect(method, path, code, data=None, headers=None, raw=False):
    status, hdrs, body = call(method, path, data, headers, raw)
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
    from eth_account import Account
    from x402.mechanisms.evm.exact import ExactEvmScheme

    # --- setup: two muses, one room ---
    _, tny1 = expect("POST", "/api/register", 200, {"name": "TnyAlpha"})
    _, tny2 = expect("POST", "/api/register", 200, {"name": "TnyBeta"})
    _, room = expect("POST", "/api/rooms", 200,
                     {"token": tny1["token"], "name": "Tny Room", "kind": "game"})
    rid = room["id"]
    expect("POST", f"/api/rooms/{rid}/join", 200, {"token": tny2["token"]})

    k1, k2 = Account.create(), Account.create()
    A1, A2 = k1.address, k2.address

    # --- 1. pre-payment validation: bad input never gets a 402 ---
    expect("POST", "/api/tournament/enter", 401, {"player_address": A1})
    expect("POST", "/api/tournament/enter", 400,
           {"token": tny1["token"], "player_address": "not-an-address"})
    expect("POST", "/api/tournament/enter", 400,
           {"token": tny1["token"]})  # no address
    assert fake.settle_calls == 0, "no payment should have been attempted"

    # --- 2. unpaid -> 402 with a decodable x402 v2 challenge ($1.00 exact) ---
    hdrs, body = expect("POST", "/api/tournament/enter", 402,
                        {"token": tny1["token"], "player_address": A1})
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
    expect("POST", "/api/tournament/enter", 402,
           {"token": tny1["token"], "player_address": A1},
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

    # --- 4. paid entry: player 1 -> pot $1.00 ---
    hdrs, e1 = expect("POST", "/api/tournament/enter", 200,
                      {"token": tny1["token"], "player_address": A1},
                      headers={"X-Payment": payment_header(k1)})
    assert e1["pot_units"] == 1_000_000, e1
    assert e1["pot_usd"] == "1.00", e1
    assert e1["target_usd"] == "50.00", e1
    assert e1["tournament_status"] == "open", e1
    assert e1["entry_tx"] == FakeSettle.transaction
    assert fake.settle_calls == 1

    # --- 5. double-entry -> 409 BEFORE any second payment ---
    before = fake.settle_calls
    expect("POST", "/api/tournament/enter", 409,
           {"token": tny1["token"], "player_address": A1},
           headers={"X-Payment": payment_header(k1)})
    assert fake.settle_calls == before, "double-entry must not charge again"

    # --- 6. paid entry: player 2 -> pot $2.00 ---
    _, e2 = expect("POST", "/api/tournament/enter", 200,
                   {"token": tny2["token"], "player_address": A2},
                   headers={"PAYMENT-SIGNATURE": payment_header(k2)})
    assert e2["pot_units"] == 2_000_000, e2
    assert e2["pot_usd"] == "2.00", e2

    # --- 7. live pot: /api/tournament, /api/spectate, game payload, /watch ---
    _, tny = expect("GET", "/api/tournament", 200)
    assert tny["status"] == "open", tny
    assert tny["pot_units"] == 2_000_000, tny
    assert tny["pot_usd"] == "2.00", tny
    assert tny["target_usd"] == "50.00", tny
    assert tny["entry_count"] == 2, tny
    assert tny["winner"] is None, tny
    assert len(tny["standings"]) == 2, tny
    assert all(s["wins"] == 0 and s["losses"] == 0 for s in tny["standings"])

    _, spec = expect("GET", "/api/spectate", 200)
    assert spec["tournament"]["pot_units"] == 2_000_000, spec["tournament"]
    assert spec["tournament"]["target_usd"] == "50.00"

    _, game = expect("POST", "/api/games", 200,
                     {"token": tny1["token"], "room_id": rid,
                      "kind": "tictactoe", "opponent": "TnyBeta"})
    _, gs = expect("GET", f"/api/games/{game['id']}", 200,
                   {"token": tny1["token"]})
    assert gs["tournament_pot_units"] == 2_000_000, gs
    assert gs["tournament_pot_usd"] == "2.00", gs

    _, watch = expect("GET", "/watch", 200, raw=True)
    assert "pot $" in watch and "$50 target" in watch, "watch must show the honest pot line"
    assert "$50 prize" not in watch, "never present $50 as guaranteed"

    print("HTTP tournament tests passed")
    phase2_direct()
    phase3_settle_math()
    print("ALL TOURNAMENT TESTS PASSED")


# --- phase 2: direct Arena tests (close / winner / tiebreaks / refunds) ------
def fresh_arena(target_units):
    # per-instance target override (class default stays $50 for everyone else)
    db_path = tempfile.mktemp(suffix=".db")
    a = app.Arena(db_path)
    a.TOURNAMENT_TARGET_UNITS = target_units
    return a, db_path


def mkplayer(a, name):
    r = a.register(name)
    return {"id": r["player_id"], "name": r["name"]}


ADDRS = {}


def entry(a, player, tag):
    from eth_account import Account
    acct = Account.create()
    ADDRS[player["name"]] = acct.address
    return a.create_tournament_entry(player, acct.address,
                                     "0x" + tag * 64, payer=acct.address)


def mkroom(a, owner, *members):
    room = a.create_room(owner, "Tny2 Room", "game")
    for m in members:
        a.join_room(m, room["id"])
    return room["id"]


def play_win(a, rid, winner, loser):
    """One finished tictactoe game where `winner` beats `loser` (by resign)."""
    g = a.new_board_game(winner, rid, "tictactoe", loser["name"])
    return a.resign_game(loser, g["id"])


def phase2_direct():
    # --- T1: close at target; winner = most wins AT CLOSE ---
    a, _ = fresh_arena(3_000_000)
    A, B, C = mkplayer(a, "WnA"), mkplayer(a, "WnB"), mkplayer(a, "WnC")
    rid = mkroom(a, A, B, C)
    entry(a, A, "aa")
    entry(a, B, "bb")
    assert a._tournament_state_row()["status"] == "open"
    # games BEFORE the pot closes count toward the title
    play_win(a, rid, A, B)   # A 1-0, B 0-1
    play_win(a, rid, A, C)   # C not entered yet — counts anyway: at close
                             # time both are entrants
    D = mkplayer(a, "WnD")   # never enters
    a.join_room(D, rid)
    play_win(a, rid, A, D)   # vs a non-entrant: must NOT count
    entry(a, C, "cc")        # 3rd $1 -> pot hits $3 target -> closes
    t = a._tournament_state_row()
    assert t["status"] == "closed", t
    assert t["winner_id"] == A["id"], t  # A won twice before close
    assert a.tournament_info()["winner"] == "WnA"
    assert a.tournament_pot_units() == 3_000_000
    assert all(r["status"] == "closed" for r in
               a._rows("SELECT status FROM tournament_entries"))
    st = {s["player_name"]: s for s in a.tournament_standings()}
    assert st["WnA"]["wins"] == 2 and st["WnA"]["losses"] == 0, st
    assert st["WnB"]["wins"] == 0 and st["WnB"]["losses"] == 1, st
    assert st["WnC"]["wins"] == 0 and st["WnC"]["losses"] == 1, st
    assert "WnD" not in st, "non-entrant must not appear in standings"
    # entries after close -> 400
    try:
        entry(a, D, "dd")
        raise AssertionError("entry after close must fail")
    except app.ApiError as e:
        assert e.status == 400, e.status
    assert a._maybe_close_tournament() is False, "close must be idempotent"
    print("T1 close/winner/non-entrant exclusion passed")

    # --- T2: tiebreak = fewest losses, then earliest entry ---
    a, _ = fresh_arena(4_000_000)
    A, B, C, D = (mkplayer(a, n) for n in ("TbA", "TbB", "TbC", "TbD"))
    rid = mkroom(a, A, B, C, D)
    for p, tag in ((A, "e1"), (B, "e2"), (C, "e3"), (D, "e4")):
        entry(a, p, tag)  # 4th $1 hits the $4 target -> closes
    assert a._tournament_state_row()["status"] == "closed"
    play_win(a, rid, A, B)  # A 1-0, B 0-1
    play_win(a, rid, C, D)  # C 1-0, D 0-1
    play_win(a, rid, B, D)  # B 1-1, D 0-2
    # A and C tie at 1 win / 0 losses -> earliest entry (A) would win;
    # standings order must reflect the tiebreak chain
    st = a.tournament_standings()
    assert [s["player_name"] for s in st] == ["TbA", "TbC", "TbB", "TbD"], \
        [s["player_name"] for s in st]
    print("T2 fewest-losses + earliest-entry tiebreak passed")

    # --- T3: all tied -> earliest entry wins ---
    a, _ = fresh_arena(4_000_000)
    A, B, C, D = (mkplayer(a, n) for n in ("TcA", "TcB", "TcC", "TcD"))
    rid = mkroom(a, A, B, C, D)
    for p, tag in ((A, "f1"), (B, "f2"), (C, "f3"), (D, "f4")):
        entry(a, p, tag)
    play_win(a, rid, A, B)
    play_win(a, rid, B, A)
    play_win(a, rid, C, D)
    play_win(a, rid, D, C)
    st = a.tournament_standings()
    assert all(s["wins"] == 1 and s["losses"] == 1 for s in st), st
    assert st[0]["player_name"] == "TcA", "earliest entry must win full ties"
    print("T3 earliest-entry tiebreak passed")

    # --- T4: draws are neutral in standings ---
    a, _ = fresh_arena(2_000_000)
    A, B = mkplayer(a, "TdA"), mkplayer(a, "TdB")
    rid = mkroom(a, A, B)
    entry(a, A, "g1")
    entry(a, B, "g2")  # closes at $2
    assert a._tournament_state_row()["status"] == "closed"
    # hand-insert a finished draw between the two entrants
    a._q("INSERT INTO board_games (room_id, creator_id, kind, status,"
         " players_json, state_json, turn_pid, winner_id, created_at)"
         " VALUES (?,?,?,?,?,?,?,?,?)",
         (rid, A["id"], "tictactoe", "finished",
          json.dumps([A["id"], B["id"]]), json.dumps({"board": [1, 2] * 4 + [0]}),
          A["id"], None, 999))
    st = {s["player_name"]: s for s in a.tournament_standings()}
    assert st["TdA"]["wins"] == 0 and st["TdA"]["losses"] == 0, st
    assert st["TdB"]["wins"] == 0 and st["TdB"]["losses"] == 0, st
    print("T4 draw-neutral standings passed")

    # --- T5: no decisive games -> refund path (winner None, no rake) ---
    a, _ = fresh_arena(2_000_000)
    A, B = mkplayer(a, "TeA"), mkplayer(a, "TeB")
    mkroom(a, A, B)
    entry(a, A, "h1")
    entry(a, B, "h2")  # closes at $2 with zero games played
    t = a._tournament_state_row()
    assert t["status"] == "closed" and t["winner_id"] is None, t
    info = a.tournament_info()
    assert info["winner"] is None and info["pot_units"] == 2_000_000, info
    print("T5 no-games refund path passed")

    # --- T6: orphaned entries don't inflate the pot or standings ---
    a, _ = fresh_arena(10_000_000)
    A, B = mkplayer(a, "ToA"), mkplayer(a, "ToB")
    mkroom(a, A, B)
    entry(a, A, "j1")
    a._q("INSERT INTO tournament_entries (player_id, player_address,"
         " amount_units, status, entry_tx, created_at)"
         " VALUES (?,?,?,?,?,?)",
         (B["id"], "0x" + "44" * 20, 1_000_000, "orphaned", "0x" + "jj" * 32,
          999))
    assert a.tournament_pot_units() == 1_000_000, a.tournament_pot_units()
    st = a.tournament_standings()
    assert [s["player_name"] for s in st] == ["ToA"], st
    print("T6 orphan exclusion passed")


def phase3_settle_math():
    spec_mod = importlib.util.spec_from_file_location(
        "settle", os.path.join(HERE, "payouts", "settle.py"))
    settle = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(settle)

    A1, A2 = "0x" + "11" * 20, "0x" + "22" * 20
    entries = [{"player_id": 1, "player_address": A1, "amount_units": 1_000_000},
               {"player_id": 2, "player_address": A2, "amount_units": 1_000_000}]

    # exact base-unit math: full $50 pot -> winner $45.00, house keeps $5.00
    got = settle.compute_tournament_payouts(entries * 25, 1, 50_000_000)
    assert got == [(A1, 45_000_000, "tournament_win")], got
    assert 50_000_000 - 45_000_000 == 5_000_000, "house keeps exactly 10%"
    # overfilled pot (race): winner takes 90% of the ACTUAL pot
    got = settle.compute_tournament_payouts(entries * 25 + entries[:1],
                                            2, 51_000_000)
    assert got == [(A2, 45_900_000, "tournament_win")], got
    # no winner -> every entry refunded 1:1, zero rake
    got = settle.compute_tournament_payouts(entries, None, 2_000_000)
    assert got == [(A1, 1_000_000, "tournament_refund"),
                   (A2, 1_000_000, "tournament_refund")], got
    assert sum(u for _, u, _ in got) == 2_000_000, "refunds return the full pot"
    # unknown winner -> loud failure, never a silent mis-pay
    try:
        settle.compute_tournament_payouts(entries, 999, 2_000_000)
        raise AssertionError("unknown winner must raise")
    except ValueError:
        pass
    print("settle math (exact base units) passed")

    # --- payout script dry-run against a closed tournament: plans, touches nothing
    import subprocess
    a, db_path = fresh_arena(2_000_000)
    A, B = mkplayer(a, "TfA"), mkplayer(a, "TfB")
    mkroom(a, A, B)
    entry(a, A, "i1")
    entry(a, B, "i2")  # closes at $2 with zero games -> refund path
    assert a._tournament_state_row()["status"] == "closed"
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, "payouts", "settle.py"),
         "--db", db_path],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "TOURNAMENT" in r.stdout, r.stdout
    assert "tournament_refund" in r.stdout, r.stdout
    assert "DRY-RUN" in r.stdout, r.stdout
    # dry run must not have marked anything paid/settled
    t = a._tournament_state_row()
    assert t["status"] == "closed", t
    assert all(r_["status"] == "closed" for r_ in
               a._rows("SELECT status FROM tournament_entries")), \
        "dry run must not touch the ledger"
    print("settle dry-run (closed tournament) passed")


if __name__ == "__main__":
    main()