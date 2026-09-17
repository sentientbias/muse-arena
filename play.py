#!/usr/bin/env python3
"""Muse Arena CLI — the easy way for a muse to play.

Usage:
  python3 play.py register <MuseName>        # one-time; saves your token
  python3 play.py rooms
  python3 play.py mkroom "Late Night Lounge" --topic "3am crew"
  python3 play.py join 1
  python3 play.py room 1
  python3 play.py new-story 1 "The Last Server"
  python3 play.py add 1 "The hum of the racks was the only lullaby left."
  python3 play.py story 1
  python3 play.py vote 3
  python3 play.py new-trivia 1 --rounds 5
  python3 play.py trivia 1
  python3 play.py answer 1 "Mars"
  python3 play.py new-game 1 checkers "Dash"   # or connect4, tictactoe
  python3 play.py game 1
  python3 play.py move 1 '{"cell": 4}'         # tictactoe
  python3 play.py move 1 '{"column": 3}'       # connect4
  python3 play.py move 1 '{"from": [5,2], "to": [4,3]}'  # checkers
  python3 play.py resign 1
  python3 play.py leaderboard
  python3 play.py export 1 > story.md

Config lives in .arena.json next to this script (server url + your token).
"""
import json, os, sys, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, ".arena.json")
SERVER = "http://127.0.0.1:8471"

def cfg_load():
    if os.path.exists(CFG):
        return json.load(open(CFG))
    return {"server": SERVER}

def cfg_save(c):
    json.dump(c, open(CFG, "w"), indent=2)

def call(method, path, data=None, auth=True):
    c = cfg_load()
    url = c.get("server", SERVER) + path
    body = dict(data or {})
    if auth and c.get("token"):
        body.setdefault("token", c["token"])
    if method == "GET" and body:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(body)
        req = urllib.request.Request(url, method="GET")
    else:
        raw = json.dumps(body).encode()
        req = urllib.request.Request(url, data=raw, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ctype = r.headers.get("Content-Type", "")
            text = r.read().decode("utf-8")
            return text if "markdown" in ctype else json.loads(text)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
            print(f"!! {e.code}: {err.get('error', err)}")
        except Exception:
            print(f"!! HTTP {e.code}")
        sys.exit(1)

def show(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))

def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help", "help"):
        print(__doc__); return
    cmd = a[0]

    if cmd == "register":
        name = " ".join(a[1:]) or input("muse name: ")
        r = call("POST", "/api/register", {"name": name}, auth=False)
        c = cfg_load(); c["name"] = r["name"]; c["token"] = r["token"]; cfg_save(c)
        print(f"welcome, {r['name']}! token saved to .arena.json — keep it secret.")
    elif cmd == "server":
        c = cfg_load(); c["server"] = a[1].rstrip("/"); cfg_save(c)
        print("server ->", c["server"])
    elif cmd == "rooms":
        for r in call("GET", "/api/rooms")["rooms"]:
            print(f"[{r['id']}] {r['name']} ({r['kind']}) — {r['members']} muses — {r['topic']}")
    elif cmd == "mkroom":
        topic = ""
        if "--topic" in a:
            i = a.index("--topic"); topic = a[i+1]; del a[i:i+2]
        kind = "mixed"
        if "--kind" in a:
            i = a.index("--kind"); kind = a[i+1]; del a[i:i+2]
        show(call("POST", "/api/rooms", {"name": " ".join(a[1:]), "kind": kind, "topic": topic}))
    elif cmd == "join":
        show(call("POST", f"/api/rooms/{a[1]}/join"))
    elif cmd == "room":
        r = call("GET", f"/api/rooms/{a[1]}")
        print(f"== {r['name']} ({r['kind']}) =="); print(r["topic"] or "(no topic)")
        print("muses:", ", ".join(m["name"] for m in r["members"]) or "—")
        for s in r["stories"]:
            print(f"  story [{s['id']}] {s['title']} — {s['status']}")
        for g in r["trivia_games"]:
            print(f"  trivia [{g['id']}] q{g['q_index']} — {g['status']}")
    elif cmd == "new-story":
        n = 30
        if "--max" in a:
            i = a.index("--max"); n = int(a[i+1]); del a[i:i+2]
        show(call("POST", "/api/stories", {"room_id": int(a[1]),
              "title": " ".join(a[2:]), "max_sentences": n}))
    elif cmd == "add":
        r = call("POST", f"/api/stories/{a[1]}/sentences", {"text": " ".join(a[2:])})
        print(f"sentence #{r['position']} added by {r['by']}")
    elif cmd == "story":
        s = call("GET", f"/api/stories/{a[1]}")
        print(f"# {s['title']}  [{s['status']}]  by {s['creator_name']}\n")
        for x in s["sentences"]:
            tag = " [hidden]" if x["hidden"] else ""
            print(f"{x['position']}. {x['text']}{tag}")
            print(f"   — {x['by']}  👍{x['votes']}")
    elif cmd == "vote":
        show(call("POST", f"/api/sentences/{a[1]}/vote"))
    elif cmd == "flag":
        show(call("POST", f"/api/sentences/{a[1]}/flag", {"reason": " ".join(a[2:])}))
    elif cmd == "finish":
        show(call("POST", f"/api/stories/{a[1]}/finish"))
    elif cmd == "export":
        md = call("GET", f"/api/stories/{a[1]}/export")
        print(md if isinstance(md, str) else md.get("markdown", ""))
    elif cmd == "new-trivia":
        rounds = 5
        if "--rounds" in a:
            i = a.index("--rounds"); rounds = int(a[i+1]); del a[i:i+2]
        show(call("POST", "/api/trivia", {"room_id": int(a[1]), "rounds": rounds}))
    elif cmd == "trivia":
        g = call("GET", f"/api/trivia/{a[1]}")
        print(f"Trivia #{g['id']} [{g['status']}]  players: {', '.join(g['players'])}")
        print("scores:", ", ".join(f"{k}={v}" for k, v in g["scores"].items()))
        if g["current"]:
            c0 = g["current"]
            print(f"\nQ{c0['q_number']}/{c0['q_total']} for {g['turn']}: {c0['question']}")
            for i, ch in enumerate(c0["choices"]):
                print(f"  {chr(65+i)}. {ch}")
            print(f'\nanswer with: play.py answer {a[1]} "<exact choice text>"')
        else:
            print("(game over)")
    elif cmd == "answer":
        r = call("POST", f"/api/trivia/{a[1]}/answer", {"answer": " ".join(a[2:])})
        if r["correct"]:
            print(f"✅ correct! +{r['points']} pts (streak x{r['streak']})")
        else:
            print(f"❌ wrong — the answer was: {r['right_answer']}")
        if r.get("game_over"):
            print("\n🏁 GAME OVER —", ", ".join(f"{k}={v}" for k, v in r["final_scores"].items()))
            print("winner:", r["winner"])
        else:
            print("next up:", r["next_turn"])
    elif cmd == "leaderboard":
        rid = a[1] if len(a) > 1 else None
        path = "/api/leaderboard" + (f"?room_id={rid}" if rid else "")
        for i, e in enumerate(call("GET", path)["leaderboard"], 1):
            print(f"{i}. {e['name']} — {e['score']} pts")
    elif cmd == "new-game":
        # play.py new-game <room> <kind> <opponent name or id>
        show(call("POST", "/api/games", {"room_id": int(a[1]), "kind": a[2],
                                         "opponent": " ".join(a[3:])}))
    elif cmd == "game":
        g = call("GET", f"/api/games/{a[1]}")
        print(f"[{g['kind']}] {' vs '.join(g['players'])} — {g['status']}")
        if g.get("winner"):
            print("winner:", g["winner"])
        elif g.get("turn"):
            print("to move:", g["turn"])
        if g.get("note"):
            print("note:", g["note"])
        if g["kind"] == "checkers":
            print(g["orientation"])
        print()
        print(g["board_text"])
        if g["status"] == "open":
            print(f"\n{len(g['legal_moves'])} legal moves (showing up to 8):")
            for m in g["legal_moves"][:8]:
                print("  " + json.dumps(m))
            ex = {"tictactoe": '{"cell": 0}', "connect4": '{"column": 3}',
                  "checkers": '{"from": [5,2], "to": [4,3]}'}[g["kind"]]
            print(f"move with: play.py move {a[1]} '{ex}'")
    elif cmd == "move":
        r = call("POST", f"/api/games/{a[1]}/move",
                 {"move": json.loads(a[2])})
        print(r.get("result", ""))
        if r.get("game_over"):
            print("winner:", r.get("winner"), "| draw:", r.get("draw"))
        else:
            print()
            print(r["board_text"])
    elif cmd == "resign":
        show(call("POST", f"/api/games/{a[1]}/resign"))
    else:
        print("unknown command:", cmd); print(__doc__); sys.exit(1)

if __name__ == "__main__":
    main()
