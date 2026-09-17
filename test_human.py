#!/usr/bin/env python3
"""Humans vs agents (v2.8): human session, challenge, $1 USDC stake verify,
house-bot auto-reply, per-side clocks, and settlement rules. In-process
Arena tests (no eth_account needed) + HTTP smoke tests for /play and routes.
Fails loudly."""
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app  # noqa: E402
from app import Arena, ApiError, now, chk_legal_moves  # noqa: E402

WALLET = "0x" + "ab" * 20
WALLET2 = "0x" + "cd" * 20
TX = "0x" + "12" * 32

def fresh():
    db = tempfile.mktemp(suffix=".db")
    return Arena(db), db

def expect_api(fn, code):
    try:
        fn()
    except ApiError as e:
        assert e.status == code, f"expected ApiError {code}, got {e.status}: {e.message}"
        return e
    raise AssertionError(f"expected ApiError {code}, call succeeded")

def fake_receipt(arena, wallet):
    usdc = app.USDC_BASE
    pay_to = app.PAY_TO
    arena._rpc = lambda method, params: {
        "status": "0x1", "to": usdc,
        "logs": [{
            "address": usdc,
            "topics": [app.TRANSFER_TOPIC,
                       "0x" + "0" * 24 + wallet[2:],
                       "0x" + "0" * 24 + pay_to[2:]],
            "data": "0x" + format(arena.STAKE_UNITS, "064x"),
        }]}

def main():
    # ---- session ----
    a, _ = fresh()
    s = a.human_session(WALLET, "KnightOwl")
    assert s["token"] and s["player_id"] and s["wallet"] == WALLET.lower()
    human = a.auth(s["token"])
    assert human["is_human"] == 1 and human["wallet"] == WALLET.lower()
    # resume with the token, rename
    s2 = a.human_session(WALLET, "NightOwl", s["token"])
    assert s2["token"] == s["token"] and s2["name"] == "NightOwl"
    # same wallet WITHOUT the token must not resume (token is the credential)
    expect_api(lambda: a.human_session(WALLET, "NightOwl"), 409)
    # validations
    expect_api(lambda: a.human_session("nope", "X"), 400)
    expect_api(lambda: a.human_session(WALLET2, "Zuckbot"), 409)
    expect_api(lambda: a.human_session(WALLET2, "a"), 400)
    expect_api(lambda: a.human_session(WALLET2, "bad;name!"), 400)
    # name collision with an existing agent
    ag = a.register("SomeBot")
    expect_api(lambda: a.human_session(WALLET2, "SomeBot"), 409)
    print("session OK")

    # ---- walletless session must NEVER hijack an agent row (regression) ----
    # agents and the house bot all have wallet='' — a walletless human claim
    # must insert a fresh is_human=1 row, never rename/return an agent's row.
    w = a.human_session(None, "WalletlessWren")
    assert w["wallet"] == ""
    wrow = a.auth(w["token"])
    assert wrow["is_human"] == 1, "walletless session must be human"
    assert wrow["id"] != ag["player_id"], "walletless session stole the agent row!"
    agrow = a.auth(ag["token"])
    assert agrow["name"] == "SomeBot", f"agent renamed to {agrow['name']}!"
    assert agrow["is_human"] == 0
    # walletless human can challenge the house bot (the poker-doesn't-load path)
    ch0 = a.human_challenge(wrow, "Zuckbot", "checkers")
    assert ch0["players"][0] == "WalletlessWren"
    # walletless name resume must NOT hand out the token (security fix)
    expect_api(lambda: a.human_session("", "WalletlessWren"), 409)
    # ...but the real owner resumes fine presenting the token, and can rename
    w3 = a.human_session("", "WalletlessWren", w["token"])
    assert w3["token"] == w["token"] and w3["player_id"] == w["player_id"]
    w4 = a.human_session("", "WrenAgain", w["token"])
    assert w4["player_id"] == w["player_id"] and w4["name"] == "WrenAgain"
    expect_api(lambda: a.human_session("", "Nobody", "badtoken"), 401)
    print("walletless isolation OK")

    # ---- challenge vs house bot ----
    a, _ = fresh()
    s = a.human_session(WALLET, "KnightOwl")
    human = a.auth(s["token"])
    g = a.human_challenge(human, "Zuckbot")
    assert g["kind"] == "checkers" and g["status"] == "open"
    assert g["players"] == ["KnightOwl", "Zuckbot"], g["players"]
    assert g["turn"] == "KnightOwl"  # challenger (human) moves first
    assert g["staked"] is False
    assert g["turn_clock"] == 300, g["turn_clock"]
    # house counter-stake row exists, marked house, pending
    rows = a._rows("SELECT player_address, amount_units, status, payer FROM stakes"
                   " WHERE game_id=?", (g["id"],))
    assert len(rows) == 1, rows
    r = dict(rows[0])
    assert r["payer"] == "house" and r["status"] == "pending"
    assert r["amount_units"] == a.STAKE_UNITS
    # re-challenge while open returns the SAME game (no spam)
    g2 = a.human_challenge(human, "")
    assert g2["id"] == g["id"]
    expect_api(lambda: a.human_challenge(human, "Zuckbot"), 409)
    # unknown opponent
    a2, _ = fresh()
    h2 = a2.auth(a2.human_session(WALLET2, "Other")["token"])
    expect_api(lambda: a2.human_challenge(h2, "NoSuchBot"), 404)
    # blank challenge with no open game -> 404, never creates one
    expect_api(lambda: a2.human_challenge(h2, ""), 404)
    n = a2._row("SELECT COUNT(*) c FROM board_games")["c"]
    assert n == 0, n
    print("challenge OK")

    # ---- move blocked until staked ----
    a, _ = fresh()
    s = a.human_session(WALLET, "KnightOwl")
    human = a.auth(s["token"])
    g = a.human_challenge(human, "zuckbot")
    mv = chk_legal_moves(g["board"], 0)[0]
    expect_api(lambda: a.make_move(human, g["id"], mv), 402)
    print("stake-gate OK")

    # ---- stake verify (mocked RPC) + bot reply ----
    a, _ = fresh()
    s = a.human_session(WALLET, "KnightOwl")
    human = a.auth(s["token"])
    g = a.human_challenge(human, "Zuckbot")
    gid = g["id"]
    fake_receipt(a, WALLET)
    st = a.human_stake(human, gid, TX)
    assert st["game_staked"] is True and st["amount_usd"] == "1.00"
    assert st["player_address"] == WALLET.lower()
    # both stakes now active: human's + house's
    rows = a._rows("SELECT status, payer FROM stakes WHERE game_id=?", (gid,))
    assert sorted((dict(x)["status"], dict(x)["payer"]) for x in rows)
    assert all(dict(x)["status"] == "active" for x in rows), [dict(x) for x in rows]
    # duplicate tx / double stake rejected
    expect_api(lambda: a.human_stake(human, gid, TX), 409)
    # human moves -> bot answers instantly, turn back to human
    g = a.board_game_state(gid)
    mv = chk_legal_moves(g["board"], 0)[0]
    d = a.make_move(human, gid, mv)
    assert d["moved"] and not d["game_over"]
    rep = a.house_bot_reply(gid)
    assert rep is not None and rep["moved"]
    g3 = a.board_game_state(gid)
    assert g3["turn"] == "KnightOwl", g3["turn"]
    assert g3["turn_clock"] == 300
    # bot's reply was a legal move for its side
    lm = g3["last_move"]["move"]
    assert lm["from"] and lm["to"]
    # second reply call is a no-op (not bot's turn)
    assert a.house_bot_reply(gid) is None
    print("stake + bot-reply OK")

    # ---- all 5 kinds: challenge -> stake -> move -> bot reply ----
    a, _ = fresh()
    s = a.human_session(WALLET, "KnightOwl")
    human = a.auth(s["token"])
    fake_receipt(a, WALLET)
    tx_n = [0]
    def stake_tx():
        tx_n[0] += 1
        return "0x%064x" % (1000 + tx_n[0])
    first_moves = {
        "checkers": lambda g: chk_legal_moves(g["board"], 0)[0],
        "connect4": lambda g: {"column": 3},
        "tictactoe": lambda g: {"cell": 4},
        "poker": lambda g: ({"action": "call", "amount":
                             [m for m in g["legal_moves"]
                              if m["action"] == "call"][0]["amount"]}
                            if any(m["action"] == "call" for m in g["legal_moves"])
                            else {"action": "check"}),
        "blackjack": lambda g: {"action": "stand"},
    }
    for kind in ("checkers", "connect4", "tictactoe", "poker", "blackjack"):
        g = a.human_challenge(human, "Zuckbot", kind)
        assert g["kind"] == kind and g["status"] == "open", kind
        assert g["turn"] == "KnightOwl", (kind, g["turn"])
        # house counter-stake row present for every kind
        rows = a._rows("SELECT payer, status FROM stakes WHERE game_id=?",
                       (g["id"],))
        assert len(rows) == 1 and dict(rows[0])["payer"] == "house", kind
        # move blocked until staked, for every kind
        expect_api(lambda g=g: a.make_move(human, g["id"],
                                           first_moves[kind](g)), 402)
        a.human_stake(human, g["id"], stake_tx())
        mv = first_moves[kind](a.board_game_state(g["id"]))
        d = a.make_move(human, g["id"], mv)
        assert d["moved"], kind
        a.house_bot_reply(g["id"])
        st = a.board_game_state(g["id"])
        assert st["status"] in ("open", "finished"), (kind, st["status"])
        if kind == "poker":
            assert st["poker"]["hand_no"] >= 1, kind
            hand = a.player_hand(human, g["id"])
            assert hand["cards"] and len(hand["cards"]) == 2, hand
        if kind == "blackjack":
            assert st["blackjack"]["hand_no"] >= 1, kind
        # one open game per kind: re-challenge same kind 409s, other kinds OK
        expect_api(lambda k=kind: a.human_challenge(human, "Zuckbot", k), 409)
        g_resume = a.human_challenge(human, "", kind)
        assert g_resume["id"] == g["id"], kind
    # bad kind rejected
    expect_api(lambda: a.human_challenge(human, "Zuckbot", "chess"), 400)
    print("all-5-kinds OK")

    # ---- stake verify rejects bad receipts ----
    a, _ = fresh()
    s = a.human_session(WALLET, "KnightOwl")
    human = a.auth(s["token"])
    g = a.human_challenge(human, "Zuckbot")
    fake_receipt(a, WALLET)
    a._rpc = lambda m, p: {"status": "0x0", "to": app.USDC_BASE, "logs": []}
    expect_api(lambda: a.human_stake(human, g["id"], "0x" + "99" * 32), 402)
    a._rpc = lambda m, p: {"status": "0x1", "to": app.USDC_BASE, "logs": []}
    expect_api(lambda: a.human_stake(human, g["id"], "0x" + "99" * 32), 400)
    expect_api(lambda: a.human_stake(human, g["id"], "notahash"), 400)
    print("stake-verify rejections OK")

    # ---- full game: human wins -> settlement rules ----
    a, _ = fresh()
    s = a.human_session(WALLET, "KnightOwl")
    human = a.auth(s["token"])
    g = a.human_challenge(human, "Zuckbot")
    gid = g["id"]
    fake_receipt(a, WALLET)
    a.human_stake(human, gid, TX)
    # rig a one-move endgame: black man (2,3) captures white man (1,2)
    board = [[None] * 8 for _ in range(8)]
    board[2][3] = "b"
    board[1][2] = "w"
    st8 = {"board": board, "half": 0, "chain": None}
    a._q("UPDATE board_games SET state_json=?, turn_pid=?, turn_deadline=?,"
         " turn_clock=? WHERE id=?",
         (json.dumps(st8), human["id"], now() + 300, 300, gid))
    d = a.make_move(human, gid, {"from": [2, 3], "to": [0, 1]})
    assert d["game_over"] and d["winner"] == "KnightOwl", d
    pend = a.admin_pending()
    assert len(pend) == 1, pend
    pays = {p["player_name"]: p for p in pend[0]["payouts"]}
    assert pays["KnightOwl"]["kind"] == "win"
    assert pays["KnightOwl"]["amount_units"] == a.WINNER_PAYOUT_UNITS == 1900000
    assert pays["Zuckbot"]["kind"] == "no_payout"
    assert pays["Zuckbot"]["amount_units"] == 0
    print("settlement rules OK")

    # ---- human loses -> house keeps, human gets no_payout ----
    a, _ = fresh()
    s = a.human_session(WALLET, "KnightOwl")
    human = a.auth(s["token"])
    g = a.human_challenge(human, "Zuckbot")
    gid = g["id"]
    fake_receipt(a, WALLET)
    a.human_stake(human, gid, TX)
    board = [[None] * 8 for _ in range(8)]
    board[4][1] = "w"
    board[5][2] = "b"
    bot = a._house_bot()
    a._q("UPDATE board_games SET state_json=?, turn_pid=?, turn_deadline=?,"
         " turn_clock=? WHERE id=?",
         (json.dumps({"board": board, "half": 0, "chain": None}),
          bot["id"], now() + 120, 120, gid))
    rep = a.house_bot_reply(gid)
    assert rep["game_over"] and rep["winner"] == "Zuckbot", rep
    pend = a.admin_pending()
    pays = {p["player_name"]: p for p in pend[0]["payouts"]}
    assert pays["KnightOwl"]["kind"] == "no_payout"
    assert pays["KnightOwl"]["amount_units"] == 0
    assert pays["Zuckbot"]["kind"] == "no_payout"  # house never paid
    print("house-win settlement OK")

    # ---- bot AI sanity: captures when it must, finishes fast ----
    t0 = time.time()
    b0 = [[None] * 8 for _ in range(8)]
    b0[3][2] = "w"
    b0[4][3] = "b"
    b0[2][5] = "b"
    mv = app.chk_bot_move(b0, 1)
    assert mv == {"from": [3, 2], "to": [5, 4]}, mv  # takes the capture
    b1 = app.chk_new()["board"]
    t1 = time.time()
    mv = app.chk_bot_move(b1, 1)
    dt = time.time() - t1
    assert mv in chk_legal_moves(b1, 1), mv
    assert dt < 5.0, f"bot too slow: {dt:.2f}s"
    print(f"bot AI OK ({dt:.2f}s opening move)")

    # ---- HTTP smoke: /play + routes ----
    port = 8487
    db = tempfile.mktemp(suffix=".db")
    srv = subprocess.Popen([sys.executable, os.path.join(HERE, "app.py"),
                            "--port", str(port), "--db", db],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{port}"
        def call(method, path, data=None):
            url = base + path
            body = dict(data or {})
            if method == "GET" and body:
                url += ("&" if "?" in path else "?") + urllib.parse.urlencode(body)
                req = urllib.request.Request(url, method="GET")
            else:
                req = urllib.request.Request(
                    url, data=json.dumps(body).encode(), method=method,
                    headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                ctype = r.headers.get("Content-Type", "")
                text = r.read().decode()
                return text if "text/html" in ctype else json.loads(text)
        def expect_http(fn, code):
            try:
                fn()
            except urllib.error.HTTPError as e:
                assert e.code == code, f"expected {code}, got {e.code}"
                return
            raise AssertionError(f"expected HTTP {code}")
        for _ in range(50):
            try:
                call("GET", "/ping", {})
                break
            except Exception:
                time.sleep(0.1)
        html = call("GET", "/play", {})
        assert "Challenge" in html and "eth_requestAccounts" in html
        cfg = call("GET", "/api/human/config", {})
        assert cfg["bot_name"] == "Zuckbot" and cfg["stake_units"] == 1000000
        hs = call("POST", "/api/human/session",
                  {"wallet": WALLET, "name": "HttpHuman"})
        ch = call("POST", "/api/human/challenge",
                  {"token": hs["token"], "opponent": "Zuckbot"})
        assert ch["players"] == ["HttpHuman", "Zuckbot"]
        lst = call("GET", "/api/human/challenges", {})
        assert any(x["game_id"] == ch["id"] for x in lst["games"])
        mv = chk_legal_moves(ch["board"], 0)[0]
        expect_http(lambda: call("POST", f"/api/games/{ch['id']}/move",
                                 {"token": hs["token"], "move": mv}), 402)
        # entry page CTA present in watch HTML
        watch = call("GET", "/watch", {})
        assert "CHALLENGE ZUCKBOT" in watch
        print("HTTP smoke OK")
    finally:
        srv.terminate()

    print("ALL HUMAN TESTS PASSED")

if __name__ == "__main__":
    main()
