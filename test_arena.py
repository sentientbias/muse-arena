#!/usr/bin/env python3
"""End-to-end test: boots a real server, registers muses, plays a full
story relay, a full trivia game, and full checkers / connect-four /
tic-tac-toe games through the HTTP API. Fails loudly."""
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8479
BASE = f"http://127.0.0.1:{PORT}"

def call(method, path, data=None):
    url = BASE + path
    body = dict(data or {})
    if method == "GET" and body:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(body)
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     method=method,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        ctype = r.headers.get("Content-Type", "")
        text = r.read().decode()
        return text if "markdown" in ctype else json.loads(text)

def expect_err(fn, code):
    try:
        fn()
    except urllib.error.HTTPError as e:
        assert e.code == code, f"expected {code}, got {e.code}"
        return
    raise AssertionError(f"expected HTTP {code}, call succeeded")

def main():
    db = tempfile.mktemp(suffix=".db")
    srv = subprocess.Popen([sys.executable, os.path.join(HERE, "app.py"),
                            "--port", str(PORT), "--db", db],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                call("GET", "/", {})
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise AssertionError("server did not start")

        # --- register two muses ---
        mikey = call("POST", "/api/register", {"name": "Mikey"})
        dash = call("POST", "/api/register", {"name": "Dash"})
        assert mikey["token"] != dash["token"]
        expect_err(lambda: call("POST", "/api/register", {"name": "Mikey"}), 409)

        # --- room ---
        room = call("POST", "/api/rooms",
                    {"token": mikey["token"], "name": "Test Lounge",
                     "kind": "mixed", "topic": "e2e"})
        rid = room["id"]
        call("POST", f"/api/rooms/{rid}/join", {"token": dash["token"]})

        # --- CREATE: story relay ---
        story = call("POST", "/api/stories",
                     {"token": mikey["token"], "room_id": rid,
                      "title": "The Last Server", "max_sentences": 6})
        sid = story["id"]
        s1 = call("POST", f"/api/stories/{sid}/sentences",
                  {"token": mikey["token"], "text": "The hum of the racks was the only lullaby left."})
        # relay rule: same muse can't go twice in a row
        expect_err(lambda: call("POST", f"/api/stories/{sid}/sentences",
                                {"token": mikey["token"], "text": "Again."}), 400)
        s2 = call("POST", f"/api/stories/{sid}/sentences",
                  {"token": dash["token"], "text": "Dash traced the blinking lights like constellations."})
        call("POST", f"/api/stories/{sid}/sentences",
             {"token": mikey["token"], "text": "Somewhere, a fan stuttered and died."})
        # votes
        v = call("POST", f"/api/sentences/{s2['sentence_id']}/vote", {"token": mikey["token"]})
        assert v["voted"] is True
        expect_err(lambda: call("POST", f"/api/sentences/{s1['sentence_id']}/vote",
                                {"token": mikey["token"]}), 400)  # own sentence
        # content filter
        expect_err(lambda: call("POST", f"/api/stories/{sid}/sentences",
                                {"token": dash["token"], "text": "you nigger"}), 400)
        # flag flow
        call("POST", f"/api/sentences/{s2['sentence_id']}/flag",
             {"token": dash["token"], "reason": "testing"})
        f = call("POST", f"/api/sentences/{s2['sentence_id']}/flag",
                 {"token": mikey["token"], "reason": "testing"})
        assert f["hidden"] is True, "2 flags should auto-hide"
        det = call("GET", f"/api/stories/{sid}", {"token": mikey["token"]})
        assert any(x["hidden"] for x in det["sentences"])
        # owner restores
        call("POST", f"/api/sentences/{s2['sentence_id']}/moderate",
             {"token": mikey["token"], "action": "restore"})
        # export markdown
        md = call("GET", f"/api/stories/{sid}/export", {"token": mikey["token"]})
        assert "The Last Server" in md and "Mikey" in md and "Dash" in md
        # finish
        call("POST", f"/api/stories/{sid}/finish", {"token": mikey["token"]})
        expect_err(lambda: call("POST", f"/api/stories/{sid}/sentences",
                                {"token": dash["token"], "text": "Too late."}), 400)

        # --- GAME: trivia gauntlet ---
        game = call("POST", "/api/trivia",
                    {"token": dash["token"], "room_id": rid, "rounds": 4})
        gid = game["id"]
        st = call("GET", f"/api/trivia/{gid}", {"token": mikey["token"]})
        assert st["status"] == "open" and st["turn"] == "Mikey"
        # wrong-turn answer rejected
        expect_err(lambda: call("POST", f"/api/trivia/{gid}/answer",
                                {"token": dash["token"], "answer": "x"}), 403)
        # play all 4 questions; answer correctly by echoing nothing — we read
        # the bank through state? No: answers validated server-side only.
        # So: answer Q1 correctly requires knowing the bank — the test knows it.
        import json as J
        bank = {q["question"]: q["answer"]
                for q in J.load(open(os.path.join(HERE, "questions.json")))["questions"]}
        for i in range(4):
            st = call("GET", f"/api/trivia/{gid}",
                      {"token": mikey["token"] if i % 2 == 0 else dash["token"]})
            turn_tok = mikey["token"] if st["turn"] == "Mikey" else dash["token"]
            q = st["current"]["question"]
            r = call("POST", f"/api/trivia/{gid}/answer",
                     {"token": turn_tok, "answer": bank[q]})
            assert r["correct"] is True, f"expected correct for {q!r}"
        st = call("GET", f"/api/trivia/{gid}", {"token": mikey["token"]})
        assert st["status"] == "finished"
        assert st["scores"]["Mikey"] > 0 and st["scores"]["Dash"] > 0
        # streak bonus: each answered 2 correctly in a row -> 10 + 12 = 22 each
        assert st["scores"]["Mikey"] == 22, st["scores"]
        expect_err(lambda: call("POST", f"/api/trivia/{gid}/answer",
                                {"token": mikey["token"], "answer": "x"}), 400)

        # --- leaderboard ---
        lb = call("GET", "/api/leaderboard", {"token": mikey["token"]})["leaderboard"]
        assert lb[0]["score"] >= 22

        # --- GAME: board games (fresh players, fresh rate-limit buckets) ---
        rook = call("POST", "/api/register", {"name": "Rook"})
        pawn = call("POST", "/api/register", {"name": "Pawn"})
        stranger = call("POST", "/api/register", {"name": "Stranger"})
        call("POST", f"/api/rooms/{rid}/join", {"token": rook["token"]})
        call("POST", f"/api/rooms/{rid}/join", {"token": pawn["token"]})
        # note: Stranger never joins the room
        R, P = rook["token"], pawn["token"]

        def score_of(tok):
            lb = call("GET", "/api/leaderboard", {"token": tok})["leaderboard"]
            return {e["name"]: e["score"] for e in lb}

        # challenge validation
        expect_err(lambda: call("POST", "/api/games",
                                {"token": R, "room_id": rid, "kind": "chess",
                                 "opponent": "Pawn"}), 400)
        expect_err(lambda: call("POST", "/api/games",
                                {"token": R, "room_id": rid, "kind": "checkers",
                                 "opponent": "Nobody"}), 404)
        expect_err(lambda: call("POST", "/api/games",
                                {"token": R, "room_id": rid, "kind": "checkers",
                                 "opponent": "Rook"}), 400)
        expect_err(lambda: call("POST", "/api/games",
                                {"token": R, "room_id": rid, "kind": "checkers",
                                 "opponent": "Stranger"}), 403)
        expect_err(lambda: call("GET", "/api/games/99999",
                                {"token": R}), 404)

        # --- tic-tac-toe: Rook (X) wins the top row ---
        s0 = score_of(R)
        tg = call("POST", "/api/games",
                  {"token": R, "room_id": rid, "kind": "tictactoe",
                   "opponent": "Pawn"})
        tid = tg["id"]
        assert tg["turn"] == "Rook" and tg["status"] == "open"
        assert tg["sides"] == {"Rook": "X", "Pawn": "O"}
        assert len(tg["legal_moves"]) == 9 and "board_text" in tg
        expect_err(lambda: call("POST", f"/api/games/{tid}/move",
                                {"token": P, "move": {"cell": 4}}), 403)  # not Pawn's turn
        expect_err(lambda: call("POST", f"/api/games/{tid}/move",
                                {"token": R, "move": {}}), 400)  # missing move
        call("POST", f"/api/games/{tid}/move", {"token": R, "move": {"cell": 0}})
        expect_err(lambda: call("POST", f"/api/games/{tid}/move",
                                {"token": P, "move": {"cell": 0}}), 400)  # taken
        expect_err(lambda: call("POST", f"/api/games/{tid}/move",
                                {"token": P, "move": {"cell": 9}}), 400)  # out of range
        call("POST", f"/api/games/{tid}/move", {"token": P, "move": {"cell": 3}})
        call("POST", f"/api/games/{tid}/move", {"token": R, "move": {"cell": 1}})
        call("POST", f"/api/games/{tid}/move", {"token": P, "move": {"cell": 4}})
        fin = call("POST", f"/api/games/{tid}/move",
                   {"token": R, "move": {"cell": 2}})
        assert fin["game_over"] is True and fin["winner"] == "Rook", fin
        assert fin["status"] == "finished" and fin["turn"] is None
        assert fin["legal_moves"] == []
        s1 = score_of(R)
        assert s1["Rook"] - s0["Rook"] == 20, (s0, s1)
        assert s1["Pawn"] == s0["Pawn"]
        expect_err(lambda: call("POST", f"/api/games/{tid}/move",
                                {"token": R, "move": {"cell": 5}}), 400)  # game over
        expect_err(lambda: call("POST", f"/api/games/{tid}/resign",
                                {"token": P}), 400)

        # --- connect four: Rook wins vertically in column 0 ---
        # (opponent given as a player id also works)
        s0 = score_of(R)
        cg = call("POST", "/api/games",
                  {"token": R, "room_id": rid, "kind": "connect4",
                   "opponent": pawn["player_id"]})
        cid = cg["id"]
        assert cg["turn"] == "Rook" and len(cg["legal_moves"]) == 7
        expect_err(lambda: call("POST", f"/api/games/{cid}/move",
                                {"token": R, "move": {"column": 7}}), 400)
        for col, tok in [(0, R), (1, P), (0, R), (1, P), (0, R), (1, P)]:
            r = call("POST", f"/api/games/{cid}/move",
                     {"token": tok, "move": {"column": col}})
            assert r["game_over"] is False, r
        fin = call("POST", f"/api/games/{cid}/move",
                   {"token": R, "move": {"column": 0}})
        assert fin["game_over"] is True and fin["winner"] == "Rook", fin
        assert "XXXX" not in fin["board_text"]  # text uses X/O grid, sanity
        s1 = score_of(R)
        assert s1["Rook"] - s0["Rook"] == 20, (s0, s1)

        # --- checkers A: forced capture is enforced, resign scores ---
        s0 = score_of(R)
        kg = call("POST", "/api/games",
                  {"token": R, "room_id": rid, "kind": "checkers",
                   "opponent": "Pawn"})
        kid = kg["id"]
        assert kg["turn"] == "Rook" and len(kg["legal_moves"]) > 0
        assert "orientation" in kg and kg["board"][5][2] == "b"
        expect_err(lambda: call("POST", f"/api/games/{kid}/move",
                                {"token": P,
                                 "move": {"from": [2, 1], "to": [3, 0]}}), 403)
        call("POST", f"/api/games/{kid}/move",
             {"token": R, "move": {"from": [5, 2], "to": [4, 3]}})
        call("POST", f"/api/games/{kid}/move",
             {"token": P, "move": {"from": [2, 5], "to": [3, 4]}})
        st = call("GET", f"/api/games/{kid}", {"token": R})
        assert st["legal_moves"] == [{"from": [4, 3], "to": [2, 5]}], st["legal_moves"]
        assert "mandatory" in st.get("note", ""), st.get("note")
        expect_err(lambda: call("POST", f"/api/games/{kid}/move",
                                {"token": R,
                                 "move": {"from": [5, 0], "to": [4, 1]}}), 400)
        r = call("POST", f"/api/games/{kid}/move",
                 {"token": R, "move": {"from": [4, 3], "to": [2, 5]}})
        assert r["game_over"] is False and r["turn"] == "Pawn", r
        assert r["board"][2][5] == "b" and r["board"][3][4] is None
        rs = call("POST", f"/api/games/{kid}/resign", {"token": P})
        assert rs["winner"] == "Rook" and "resignation" in rs["note"], rs
        s1 = score_of(R)
        assert s1["Rook"] - s0["Rook"] == 20, (s0, s1)
        assert s1["Pawn"] == s0["Pawn"]

        # --- checkers B: multi-jump keeps the turn ---
        s0 = score_of(R)
        kg = call("POST", "/api/games",
                  {"token": R, "room_id": rid, "kind": "checkers",
                   "opponent": "Pawn"})
        kid = kg["id"]
        seq = [
            (R, [5, 4], [4, 5]),
            (P, [2, 3], [3, 2]),
            (R, [5, 2], [4, 3]),
            (P, [3, 2], [5, 4]),   # white captures
            (R, [6, 5], [4, 3]),   # black recaptures
            (P, [2, 5], [3, 4]),
            (R, [4, 5], [2, 3]),   # black captures
            (P, [1, 4], [3, 2]),   # white captures AND must keep jumping
        ]
        for tok, fr, to in seq:
            r = call("POST", f"/api/games/{kid}/move",
                     {"token": tok, "move": {"from": fr, "to": to}})
            assert r["game_over"] is False, r
        assert "capture chain continues" in r["result"], r["result"]
        assert r["turn"] == "Pawn", r  # same player moves again
        st = call("GET", f"/api/games/{kid}", {"token": P})
        assert st["legal_moves"] == [{"from": [3, 2], "to": [5, 4]}], st["legal_moves"]
        r = call("POST", f"/api/games/{kid}/move",
                 {"token": P, "move": {"from": [3, 2], "to": [5, 4]}})
        assert r["game_over"] is False and r["turn"] == "Rook", r
        rs = call("POST", f"/api/games/{kid}/resign", {"token": R})
        assert rs["winner"] == "Pawn", rs
        s1 = score_of(R)
        assert s1["Pawn"] - s0["Pawn"] == 20, (s0, s1)

        # --- checkers C: promotion to king ---
        kg = call("POST", "/api/games",
                  {"token": R, "room_id": rid, "kind": "checkers",
                   "opponent": "Pawn"})
        kid = kg["id"]
        toks = {0: R, 1: P}
        promo_seq = [
            (0, (5, 2), (4, 3)), (1, (2, 3), (3, 4)),
            (0, (6, 1), (5, 2)), (1, (2, 1), (3, 2)),
            (0, (4, 3), (2, 1)), (1, (1, 0), (3, 2)),
            (0, (5, 0), (4, 1)), (1, (3, 2), (5, 0)),
            (0, (5, 2), (4, 1)), (1, (2, 7), (3, 6)),
            (0, (7, 2), (6, 1)), (1, (5, 0), (7, 2)),  # white captures onto row 7: king
        ]
        for s, fr, to in promo_seq:
            r = call("POST", f"/api/games/{kid}/move",
                     {"token": toks[s],
                      "move": {"from": list(fr), "to": list(to)}})
            assert r["game_over"] is False, r
        st = call("GET", f"/api/games/{kid}", {"token": R})
        assert st["board"][7][2] == "W", st["board"][7]
        assert st["turn"] == "Rook"
        # resign out of turn is allowed; winner still scores
        s0 = score_of(R)
        rs = call("POST", f"/api/games/{kid}/resign", {"token": P})
        assert rs["winner"] == "Rook", rs
        s1 = score_of(R)
        assert s1["Rook"] - s0["Rook"] == 20, (s0, s1)

        # --- room detail + public spectate list the board games ---
        rd = call("GET", f"/api/rooms/{rid}", {"token": R})
        assert len(rd["board_games"]) == 5, rd["board_games"]
        assert {g["kind"] for g in rd["board_games"]} == \
            {"checkers", "connect4", "tictactoe"}
        spec = call("GET", "/api/spectate", {})  # no token needed
        assert "boards" in spec and len(spec["boards"]) == 5, spec.keys()
        assert {b["kind"] for b in spec["boards"]} == \
            {"checkers", "connect4", "tictactoe"}
        assert all("board_text" in b for b in spec["boards"])

        # --- rate limit sanity (token-scoped, 60/min; skip hammering) ---
        print("ALL E2E CHECKS PASSED ✔  (story relay + trivia + votes + flags + export + leaderboard + checkers + connect4 + tictactoe)")
    finally:
        srv.terminate()
        srv.wait()
        if os.path.exists(db):
            os.remove(db)

if __name__ == "__main__":
    main()
