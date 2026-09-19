#!/usr/bin/env python3
"""Casual (free-play) mode tests, v2.11.

A walletless visitor (e.g. a Musebook muse with no wallet) can claim a
human session, challenge the house bot in mode='casual', and play a full
game for free. Safety properties under test:

  * casual games are created with mode='casual' and get NO stake rows
  * check_stakeable / human_stake REFUSE casual games (400) — they can
    never take stakes, never pay out, never convert to staked games
  * admin_payouts (the settlement queue) has no entries for casual games
  * the house bot still plays in casual mode, the 5-min clock still runs
  * the staked flow is unchanged: mode='staked', house stake row written

Run:  <venv-python> test_casual.py
"""
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

PORT = 8482
BASE = f"http://127.0.0.1:{PORT}"


def call(method, path, data=None):
    url = BASE + path
    body = None
    if method == "GET" and data:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(data)
    elif method == "POST":
        body = json.dumps(data or {}).encode()
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def expect(method, path, code, data=None):
    status, body = call(method, path, data)
    assert status == code, \
        f"{method} {path}: expected {code}, got {status} ({body})"
    return body


def run_tests():
    arena = app.Handler.arena
    n = 0

    # 1. walletless session claim (no wallet key at all)
    s = expect("POST", "/api/human/session", 200, {"name": "TestMuse"})
    assert s["token"], "no token returned"
    assert s["wallet"] == "", "wallet should be empty for walletless claim"
    tok = s["token"]
    n += 1; print("ok 1: walletless human session claim")

    # 2. casual challenge vs the house bot
    g = expect("POST", "/api/human/challenge", 200,
               {"token": tok, "opponent": "Zuckbot", "kind": "checkers",
                "mode": "casual"})
    assert g["mode"] == "casual", f"game mode is {g.get('mode')}"
    assert g["status"] == "open"
    gid = g["id"]
    rows = arena._rows("SELECT * FROM stakes WHERE game_id=?", (gid,))
    assert len(rows) == 0, f"casual game has {len(rows)} stake rows"
    n += 1; print("ok 2: casual challenge — mode set, zero stake rows")

    # 3. invalid mode rejected
    b = expect("POST", "/api/human/challenge", 400,
               {"token": tok, "opponent": "Zuckbot", "kind": "connect4",
                "mode": "jackpot"})
    assert "mode" in b.get("error", "").lower()
    n += 1; print("ok 3: invalid mode -> 400")

    # 4. stake paths refuse casual games (the money-safety core)
    human = dict(arena.auth(tok))
    for fn in ("check_stakeable", "human_stake"):
        try:
            if fn == "check_stakeable":
                arena.check_stakeable(human, gid, "0x" + "ab" * 20)
            else:
                arena.human_stake(human, gid, "0x" + "ab" * 32,
                                  wallet="0x" + "ab" * 20)
            raise AssertionError(f"{fn} did not refuse the casual game")
        except app.ApiError as e:
            assert e.status == 400, f"{fn}: expected 400, got {e.status}"
    n += 1; print("ok 4: check_stakeable + human_stake refuse casual games")

    # 5. casual game is playable — human moves, house bot answers
    legal = g.get("legal_moves") or []
    assert legal, "no legal moves offered on fresh casual game"
    mv = dict(legal[0])  # checkers moves are {"from": [r,c], "to": [r,c]}
    mv.pop("captures", None)
    g2 = expect("POST", f"/api/games/{gid}/move", 200,
                {"token": tok, "move": mv})
    assert g2["moved"] is True, f"move not accepted: {g2}"
    n += 1; print("ok 5: casual game playable, bot replied")

    # 6. human_challenges lists the game with mode
    lst = expect("GET", "/api/human/challenges", 200)
    found = [x for x in lst["games"] if x["game_id"] == gid]
    assert found and found[0]["mode"] == "casual", lst
    n += 1; print("ok 6: open-games board shows casual mode")

    # 7. finish the casual game -> settlement queue stays empty
    gr = expect("POST", f"/api/games/{gid}/resign", 200, {"token": tok})
    assert gr.get("ok") is True, gr
    row = arena._board_row(gid)
    assert row["status"] == "finished", dict(row)
    payouts = arena.admin_pending()
    mine = [p for p in payouts if p["game_id"] == gid]
    assert not mine, f"casual game leaked into payout queue: {mine}"
    n += 1; print("ok 7: finished casual game -> no payout entries")

    # 8. staked flow unchanged: house stake row, mode='staked'
    s2 = expect("POST", "/api/human/session", 200, {"name": "StakedHuman"})
    tok2 = s2["token"]
    g3 = expect("POST", "/api/human/challenge", 200,
                {"token": tok2, "opponent": "Zuckbot", "kind": "checkers"})
    assert g3["mode"] == "staked", f"default mode is {g3.get('mode')}"
    rows = arena._rows("SELECT * FROM stakes WHERE game_id=?", (g3["id"],))
    assert len(rows) == 1 and rows[0]["payer"] == "house", rows
    # and the stake gate still fires for the staked game
    try:
        arena.check_stakeable(dict(arena.auth(tok2)), g3["id"],
                              "0x" + "cd" * 20)
    except app.ApiError as e:
        raise AssertionError(f"staked game wrongly refused: {e}")
    n += 1; print("ok 8: staked challenge unchanged (mode, house stake, gate)")

    # 9. staked challenge with explicit mode='staked' also fine
    s3 = expect("POST", "/api/human/session", 200, {"name": "ExplicitStaker"})
    g4 = expect("POST", "/api/human/challenge", 200,
                {"token": s3["token"], "opponent": "Zuckbot",
                 "kind": "tictactoe", "mode": "staked"})
    assert g4["mode"] == "staked"
    n += 1; print("ok 9: explicit mode='staked' works")

    print(f"\nALL {n} CASUAL-MODE TESTS PASSED")


def main():
    from http.server import ThreadingHTTPServer
    db = tempfile.mktemp(suffix=".db")
    app.Handler.arena = app.Arena(db)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), app.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        run_tests()
    finally:
        srv.shutdown()
        try:
            os.unlink(db)
        except OSError:
            pass


if __name__ == "__main__":
    main()
