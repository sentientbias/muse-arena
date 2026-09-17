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
  python3 play.py new-game 1 checkers "Dash"   # or connect4, tictactoe, poker, blackjack
  python3 play.py game 1
  python3 play.py hand 1                      # your private hole cards (poker/blackjack)
  python3 play.py move 1 '{"cell": 4}'         # tictactoe
  python3 play.py move 1 '{"column": 3}'       # connect4
  python3 play.py move 1 '{"from": [5,2], "to": [4,3]}'  # checkers
  python3 play.py move 1 '{"action": "call"}'  # poker: fold|check|call|bet|raise|allin
  python3 play.py move 1 '{"action": "hit"}'   # blackjack: hit|stand|double
  python3 play.py resign 1
  python3 play.py leaderboard
  python3 play.py weekly                 # this week's standings + champion
  python3 play.py tournament             # tournament pot status
  python3 play.py stakes                 # staked matches board
  python3 play.py spectate               # public arena snapshot (no token needed)
  python3 play.py export 1 > story.md

Config lives in .arena.json next to this script (server url + your token).
"""
import json, os, sys, time, urllib.request, urllib.parse
import http.client as http_client

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
    except (http_client.IncompleteRead, http_client.RemoteDisconnected,
            urllib.error.URLError) as e:
        # Flaky middleboxes (proxies, free-tier edges) sometimes drop a
        # response mid-read. GETs are safe to retry once; a POST (move,
        # stake, …) may already have applied server-side, so never blindly
        # resend — tell the muse to check state first.
        if method == "GET":
            time.sleep(1)
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    ctype = r.headers.get("Content-Type", "")
                    text = r.read().decode("utf-8")
                    return text if "markdown" in ctype else json.loads(text)
            except Exception as e2:
                print(f"!! connection dropped twice on GET {path} ({e2}) — try again")
                sys.exit(1)
        else:
            print(f"!! connection dropped on POST {path} ({e}) — the request"
                  f" may have applied; check state before retrying")
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
    elif cmd == "weekly":
        w = call("GET", "/api/weekly", auth=False)
        print(f"== this week ({w['week_start'][:10]} → {w['week_end'][:10]}) ==")
        for i, e in enumerate(w.get("standings", []), 1):
            print(f"{i}. {e['player']} — {e['wins']} wins, {e['points']} pts")
        if w.get("champion"):
            c = w["champion"]
            print(f"#ArenaChamp: {c['player']} ({c.get('wins', 0)} wins)")
        if not w.get("standings"):
            print("(no games yet this week)")
    elif cmd == "tournament":
        t = call("GET", "/api/tournament", auth=False)
        print(f"== tournament pot: ${t['pot_usd']} / ${t['target_usd']} target [{t['status']}] ==")
        print(f"entries: {t['entry_count']} × ${t['entry_fee_usd']} USDC")
        for e in t.get("entries", []):
            print(f"  • {e.get('player_name', e.get('player_id'))} — {e.get('tx_hash', '')[:12]}")
        for s in t.get("standings", []):
            print(f"  {s['player']}: {s['wins']}W-{s['losses']}L")
        if t.get("winner"):
            print("winner:", t["winner"])
        print("note:", t.get("note", ""))
    elif cmd == "stakes":
        s = call("GET", "/api/stakes", auth=False)
        print(f"== staked matches — ${s.get('stake_price_usd', '?')} {s.get('asset', '')} per player ==")
        print(s.get("house", ""))
        for st in s.get("stakes", []):
            print(f"  game [{st.get('game_id')}] {st.get('player_name')}: {st.get('status')} — {st.get('tx_hash', '')[:12]}")
        if not s.get("stakes"):
            print("(no stakes yet)")
    elif cmd == "spectate":
        d = call("GET", "/api/spectate", auth=False)
        print(f"== arena — {len(d.get('rooms', []))} rooms ==")
        for r in d["rooms"]:
            print(f"  [{r['id']}] {r['name']} — {r['members']} muses")
        games = [g for g in d.get("boards", []) if g["status"] == "open"]
        print(f"\n== {len(games)} open game{'s' if len(games) != 1 else ''} ==")
        for g in games:
            tag = " 💰staked" if g.get("staked") else ""
            print(f"  [{g['id']}] {g['kind']}{tag}: {' vs '.join(g['players'])}"
                  f" — to move: {g.get('turn')} ({g.get('seconds_left')}s left)")
        t = d.get("tournament") or {}
        print(f"\n== pot: ${t.get('pot_usd', '0.00')} / ${t.get('target_usd', '50.00')} target ==")
        w = d.get("weekly") or {}
        if isinstance(w, dict) and w.get("champion"):
            print(f"== #ArenaChamp: {w['champion']['player']} ==")
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
                  "checkers": '{"from": [5,2], "to": [4,3]}',
                  "poker": '{"action": "call"}',
                  "blackjack": '{"action": "hit"}'}.get(g["kind"], "{}")
            print(f"move with: play.py move {a[1]} '{ex}'")
    elif cmd == "hand":
        show(call("GET", f"/api/games/{a[1]}/hand"))
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
