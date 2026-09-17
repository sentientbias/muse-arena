#!/usr/bin/env python3
"""End-to-end test: boots a real server, registers two muses, plays a full
story relay AND a full trivia game through the HTTP API. Fails loudly."""
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

        # --- rate limit sanity (token-scoped, 60/min; skip hammering) ---
        print("ALL E2E CHECKS PASSED ✔  (story relay + trivia + votes + flags + export + leaderboard)")
    finally:
        srv.terminate()
        srv.wait()
        if os.path.exists(db):
            os.remove(db)

if __name__ == "__main__":
    main()
