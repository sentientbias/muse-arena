#!/usr/bin/env python3
"""Battleship tests (app.py engine + bots.py + human flow).

Covers: fleet validation (overlap/bounds/touch/bent/duplicates),
random fleet legality, full sink-all-ships game, fog-of-war views,
bot move shape/speed/targeting, deploy endpoint flow, stake gate,
and the HTTP-visible state contract the play.html frontend relies on.
Fails loudly.
"""
import sys, tempfile, time

import app
import bots
from app import Arena, ApiError, bs_new, bs_validate_fleet, bs_random_fleet, \
    bs_legal, bs_apply, bs_public

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def expect_api(name, fn, code):
    try:
        fn()
    except ApiError as e:
        check(name + " -> %d" % code, e.status == code)
        return
    check(name + " -> %d" % code, False)


# ---------- engine: validation ----------
good = bs_random_fleet()
check("random fleet validates", bs_validate_fleet(good) == good)

bad = [dict(s) for s in good]
bad[1]["cells"] = [list(c) for c in good[0]["cells"]]  # overlap carrier
expect_api("overlap rejected", lambda: bs_validate_fleet(bad), 400)

bad = [dict(s) for s in good]
bad[0]["cells"] = [[r, c] for r, c in good[0]["cells"]]
bad[0]["cells"][0] = [10, 0]  # out of bounds
expect_api("out-of-bounds rejected", lambda: bs_validate_fleet(bad), 400)

# diagonal touch: destroyer kitty-corner to carrier start
bad = [dict(s) for s in good]
cr, cc = good[0]["cells"][0]
bad[4]["cells"] = [[cr + 1, cc + 1], [cr + 1, cc + 2]]
expect_api("diagonal touch rejected", lambda: bs_validate_fleet(bad), 400)

bad = [dict(s) for s in good]
bad[2]["cells"] = [[0, 0], [0, 1], [1, 1]]  # bent
expect_api("bent ship rejected", lambda: bs_validate_fleet(bad), 400)

bad = [dict(s) for s in good]
bad[3] = dict(good[2])  # duplicate cruiser, missing submarine
bad[3]["name"] = "Cruiser"
expect_api("duplicate/missing rejected", lambda: bs_validate_fleet(bad), 400)

for seed in range(30):
    import random
    random.seed(seed)
    check("seeded fleet %d valid" % seed,
          bs_validate_fleet(bs_random_fleet()) is not None)

# ---------- engine: sinking ----------
st = bs_new()
st["fleets"]["0"]["ships"] = bs_random_fleet()
st["fleets"]["1"]["ships"] = bs_random_fleet()
st["phase"] = "battle"
# sink every enemy cell of side 1
won = False
for s in st["fleets"]["1"]["ships"]:
    for cell in s["cells"]:
        if [cell[0], cell[1]] in [[sh["r"], sh["c"]] for sh in st["fleets"]["0"]["shots"]]:
            continue
        st, res = bs_apply(st, 0, {"fire": [cell[0], cell[1]]})
        if res["won"]:
            won = True
check("sink-all wins", won and res["enemy_left"] == 0)
check("all hits on sunk ships labeled",
      all(sh["sunk"] for s in st["fleets"]["1"]["ships"]
          for cell in s["cells"]
          for sh in st["fleets"]["0"]["shots"]
          if [sh["r"], sh["c"]] == [cell[0], cell[1]]))

# bs_apply is pure (validation lives in make_move via bs_legal):
# a repeat cell is simply no longer legal
fired_cell = list(st["fleets"]["1"]["ships"][0]["cells"][0])
check("repeat fire not legal",
      {"fire": fired_cell} not in bs_legal(st, 0))
check("legal excludes fired cells",
      all({"fire": [sh["r"], sh["c"]]} not in bs_legal(st, 0)
          for sh in st["fleets"]["0"]["shots"]))

# ---------- fog of war ----------
pub0 = bs_public(st, 0)
check("owner sees own fleet", pub0["sides"][0]["fleet_board"] is not None)
check("enemy fleet hidden from owner", pub0["sides"][1]["fleet_board"] is None)
check("owner ships listed", len(pub0["sides"][0]["ships"]) == 5)
check("enemy ships not listed", pub0["sides"][1]["ships"] == [])
pubx = bs_public(st, None)
check("spectator sees no fleets",
      pubx["sides"][0]["fleet_board"] is None and pubx["sides"][1]["fleet_board"] is None)
check("sunk ships revealed on target board",
      any(3 in row for row in pub0["sides"][0]["target_board"]))
check("last_result exposed", pub0["last_result"] is not None and "sunk" in pub0["last_result"])

# ---------- bot ----------
st2 = bs_new()
st2["fleets"]["0"]["ships"] = bs_random_fleet()
st2["fleets"]["1"]["ships"] = bs_random_fleet()
st2["phase"] = "battle"
t0 = time.time()
mv = bots.battleship_move(st2, 0, mistake_rate=0)
dt = time.time() - t0
check("bot move shape", set(mv.keys()) == {"fire"} and len(mv["fire"]) == 2)
check("bot move legal", mv in bs_legal(st2, 0))
check("bot fast (<1s)", dt < 1.0)

# targeting: single hit -> orthogonal neighbor (no mistakes)
st3 = bs_new()
st3["fleets"]["0"]["ships"] = bs_random_fleet()
st3["fleets"]["1"]["ships"] = bs_random_fleet()
st3["phase"] = "battle"
st3["fleets"]["0"]["shots"] = [{"r": 5, "c": 5, "hit": True, "sunk": None}]
seen = set()
for _ in range(60):
    m = bots.battleship_move(st3, 0, mistake_rate=0)
    seen.add(tuple(m["fire"]))
check("target mode goes orthogonal",
      seen <= {(4, 5), (6, 5), (5, 4), (5, 6)} and len(seen) > 1)

# ---------- human flow: challenge -> deploy -> stake gate -> battle ----------
WALLET = "0x" + "ab" * 20
TX = "0x" + "12" * 32


def fresh():
    return Arena(tempfile.mktemp(suffix=".db"))


def fake_receipt(arena, wallet):
    usdc, pay_to = app.USDC_BASE, app.PAY_TO
    arena._rpc = lambda method, params: {
        "status": "0x1", "to": usdc,
        "logs": [{"address": usdc,
                  "topics": [app.TRANSFER_TOPIC,
                             "0x" + "0" * 24 + wallet[2:],
                             "0x" + "0" * 24 + pay_to[2:]],
                  "data": "0x" + format(arena.STAKE_UNITS, "064x")}]}


a = fresh()
fake_receipt(a, WALLET)
s = a.human_session(WALLET, "BSTester")
human = a.auth(s["token"])
g = a.human_challenge(human, "Zuckbot", "battleship")
gid = g["id"]
check("challenge opens deploy phase", g["battleship"]["phase"] == "deploy")
check("human moves first", g["turn"] == human["name"])

expect_api("fire before deploy blocked (stake gate)",
           lambda: a.make_move(human, gid, {"fire": [0, 0]}), 402)
expect_api("bad fleet rejected",
           lambda: a.deploy_fleet(human, gid, []), 400)

g = a.deploy_fleet(human, gid, bs_random_fleet())
check("deploy enters battle", g["battleship"]["phase"] == "battle")
check("bot fleet hidden after deploy",
      g["battleship"]["sides"][1]["fleet_board"] is None)
check("own fleet visible after deploy",
      g["battleship"]["sides"][0]["fleet_board"] is not None)
expect_api("re-deploy blocked",
           lambda: a.deploy_fleet(human, gid, bs_random_fleet()), 400)
expect_api("fire before stake blocked",
           lambda: a.make_move(human, gid, {"fire": [0, 0]}), 402)

st = a.human_stake(human, gid, TX)
check("stake accepted", st["game_staked"] is True)

# play a full game: human fires first-unknown cell, bot replies instantly
moves = 0
while True:
    g = a.board_game_state(gid, human["id"])
    if g["status"] != "open":
        break
    check("turn returns to human", g["turn"] == human["name"])
    me = g["battleship"]["sides"][0]
    tgt = next([r, c] for r in range(10) for c in range(10)
               if me["target_board"][r][c] == 0)
    a.make_move(human, gid, {"fire": tgt})
    a.house_bot_reply(gid)  # h_move does this in production
    g = a.board_game_state(gid, human["id"])
    moves += 1
    if g["status"] == "open":
        check("bot replied", g["last_move"]["by"] == "Zuckbot")
    check("no mid-game fleet leak",
          g["battleship"]["sides"][1]["fleet_board"] is None)
    if moves > 300:
        check("game finishes", False)
        break
check("game finished", g["status"] == "finished")
check("winner recorded", bool(g.get("winner")))
lr = g["battleship"]["last_result"]
check("final result shape",
      lr is not None and lr["won"] and lr["enemy_left"] == 0)

spec = a.board_game_state(gid)
check("spectator sees no fleets post-game",
      spec["battleship"]["sides"][0]["fleet_board"] is None and
      spec["battleship"]["sides"][1]["fleet_board"] is None)

# repeat / malformed shots on a fresh game
WALLET2 = "0x" + "cd" * 20
fake_receipt(a, WALLET2)
s2 = a.human_session(WALLET2, "BSTester2")
h2 = a.auth(s2["token"])
g3 = a.human_challenge(h2, "Zuckbot", "battleship")
a.deploy_fleet(h2, g3["id"], bs_random_fleet())
a.human_stake(h2, g3["id"], "0x" + "34" * 32)
a.make_move(h2, g3["id"], {"fire": [0, 0]})
a.house_bot_reply(g3["id"])
expect_api("repeat shot blocked",
           lambda: a.make_move(h2, g3["id"], {"fire": [0, 0]}), 400)
expect_api("off-grid shot blocked",
           lambda: a.make_move(h2, g3["id"], {"fire": [10, 10]}), 400)
expect_api("malformed shot blocked",
           lambda: a.make_move(h2, g3["id"], {"fire": [3]}), 400)

print()
if fails:
    print("FAILURES:", fails)
    raise SystemExit(1)
print("ALL BATTLESHIP TESTS PASSED")
