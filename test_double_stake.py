#!/usr/bin/env python3
"""Double-payment race tests for the HUMAN stake path (/api/human/stake).

Covers the hole where a verified-but-rejected second payment went invisible
(no row anywhere -> manual chain-scanning to refund):

  A. concurrent double-payment race -> exactly one stake row, one success +
     one 409, and the loser's payment parked in orphan_payments (never lost)
  B. sequential second payment        -> 409 + orphan recorded (refundable)
  C. same-tx resubmission             -> 409, NO orphan (no new money moved)
  D. record_orphan_payment            -> idempotent per tx_hash (one tx =
     one refund row, never double-listed)

In-process Arena, mocked _rpc (no network). Fails loudly.
"""
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import app  # noqa: E402
from app import Arena, ApiError, now  # noqa: E402

WALLET = "0x" + "ab" * 20
TX1 = "0x" + "11" * 32
TX2 = "0x" + "22" * 32
TX3 = "0x" + "33" * 32
TX9 = "0x" + "99" * 32


def fresh():
    db = tempfile.mktemp(suffix=".db")
    return Arena(db)


def fake_receipt(arena, wallet, delay=0.0):
    """Mock _rpc: every tx_hash verifies as a real $1 USDC transfer."""
    usdc, pay_to = app.USDC_BASE, app.PAY_TO

    def _fake(method, params):
        if delay:
            time.sleep(delay)
        return {
            "status": "0x1", "to": usdc,
            "logs": [{
                "address": usdc,
                "topics": [app.TRANSFER_TOPIC,
                           "0x" + "0" * 24 + wallet[2:],
                           "0x" + "0" * 24 + pay_to[2:]],
                "data": "0x" + format(arena.STAKE_UNITS, "064x"),
            }],
        }

    arena._rpc = _fake


def new_game(a):
    s = a.human_session(WALLET, "RaceRae")
    g = a.human_challenge(a.auth(s["token"]), "zuckbot")
    return s, g["id"]


def expect_409(fn):
    try:
        fn()
    except ApiError as e:
        assert e.status == 409, f"expected 409, got {e.status}: {e.message}"
        return e
    raise AssertionError("expected ApiError 409, call succeeded")


def orphans(a):
    return [dict(r) for r in a._rows(
        "SELECT game_id, payer, amount_units, tx_hash, reason"
        " FROM orphan_payments ORDER BY id")]


def main():
    # ---- A. concurrent double-payment race -------------------------------
    a = fresh()
    s, gid = new_game(a)
    fake_receipt(a, WALLET, delay=0.3)  # widen the race window
    h1, h2 = a.auth(s["token"]), a.auth(s["token"])
    out, barrier = {}, threading.Barrier(2)

    def racer(slot, human, tx):
        barrier.wait(timeout=30)
        try:
            out[slot] = ("ok", a.human_stake(human, gid, tx))
        except ApiError as e:
            out[slot] = ("api", e.status)

    th = [threading.Thread(target=racer, args=(0, h1, TX1)),
          threading.Thread(target=racer, args=(1, h2, TX2))]
    for t in th:
        t.start()
    for t in th:
        t.join(timeout=60)
    got = sorted(v[1] if v[0] == "api" else 200 for v in out.values())
    assert got == [200, 409], f"race: expected one 200 + one 409, got {got}"
    winner_tx = TX1 if out[0][0] == "ok" else TX2
    loser_tx = TX2 if out[0][0] == "ok" else TX1
    hid = h1["id"]
    srows = a._rows("SELECT stake_tx, status FROM stakes WHERE game_id=?"
                    " AND player_id=?", (gid, hid))
    assert len(srows) == 1, f"race: exactly one stake row, got {srows}"
    assert srows[0]["stake_tx"] == winner_tx
    orows = orphans(a)
    assert len(orows) == 1, \
        f"race: loser payment must be parked exactly once, got {orows}"
    assert orows[0]["tx_hash"] == loser_tx, orows
    assert orows[0]["amount_units"] == 1_000_000, orows
    assert orows[0]["payer"] == WALLET.lower(), orows
    print("A. race -> one 200 + one 409, 1 stake row, loser parked: OK")

    # ---- B. sequential second payment -> 409 + orphan -----------------------
    a = fresh()
    s, gid = new_game(a)
    fake_receipt(a, WALLET)
    h = a.auth(s["token"])
    st = a.human_stake(h, gid, TX1)
    assert st["status"] in ("pending", "active"), st
    e = expect_409(lambda: a.human_stake(a.auth(s["token"]), gid, TX2))
    assert "already staked" in e.message, e.message
    orows = orphans(a)
    assert len(orows) == 1 and orows[0]["tx_hash"] == TX2, orows
    assert orows[0]["amount_units"] == 1_000_000
    # the original stake is untouched
    srows = a._rows("SELECT stake_tx FROM stakes WHERE game_id=?"
                    " AND player_id=?", (gid, h["id"]))
    assert [r["stake_tx"] for r in srows] == [TX1]
    print("B. sequential double-pay -> 409 + orphan recorded: OK")

    # ---- C. same-tx resubmission -> 409, NO orphan ---------------------------
    before = len(orphans(a))
    e = expect_409(lambda: a.human_stake(a.auth(s["token"]), gid, TX1))
    assert "already staked a game" in e.message, e.message
    assert len(orphans(a)) == before, "same-tx resubmit must not park an orphan"
    print("C. same-tx resubmission -> 409, no orphan: OK")

    # ---- D. record_orphan_payment idempotent per tx_hash ---------------------
    a = fresh()
    a.record_orphan_payment(7, WALLET, 1_000_000, TX9, "test")
    a.record_orphan_payment(7, WALLET, 1_000_000, TX9, "test retry")
    rows = a._rows("SELECT COUNT(*) c FROM orphan_payments WHERE tx_hash=?",
                   (TX9,))
    assert rows[0]["c"] == 1, rows
    print("D. orphan recording idempotent per tx_hash: OK")

    print("ALL DOUBLE-STAKE TESTS PASSED")


if __name__ == "__main__":
    main()
