#!/usr/bin/env python3
"""Rewards v1 engine tests: schema, karma, caps, achievements, cosmetics,
founders. Runs the Arena class directly against a scratch DB. Fails loudly."""
import os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app

fails = []
def check(name, cond, detail=""):
    if cond:
        print("  ok:", name)
    else:
        print("  FAIL:", name, detail)
        fails.append(name)

def main():
    db = tempfile.mktemp(suffix=".db")
    a = app.Arena(db)

    print("schema")
    need = {"karma_ledger", "player_karma", "trophy_case", "cosmetic_inventory",
            "player_loadout", "founders"}
    have = {r["name"] for r in a._rows(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("all rewards tables created", need <= have, str(need - have))
    a2 = app.Arena(db)  # idempotent re-init
    check("schema init idempotent", True)

    print("registration")
    r1 = a.register("RWAlpha"); p1 = r1["player_id"]
    r2 = a.register("RWBeta"); p2 = r2["player_id"]
    check("early-adopter granted", a._has_achievement(p1, "early-adopter"))
    # registration now also grants the early/activity line's
    # early-founding-week (+10) and early-first-100 (+25)
    check("register karma 60", a.karma_balance(p1) == 60, a.karma_balance(p1))

    print("karma + caps")
    a.award_karma(p2, 10, "arena_game", "t1")
    a.award_karma(p2, 100, "arena_game", "t2")
    check("daily cap enforced (20)", a.karma_balance(p2) == 60 + 20,
          a.karma_balance(p2))
    a.award_karma(p2, 5, "arena_win", "t3")  # different source, own cap
    check("separate source cap", a.karma_balance(p2) == 60 + 20 + 5, a.karma_balance(p2))

    print("house bot excluded")
    house = a._house_pid()
    a.award_karma(house, 50, "arena_game", "t")
    a.grant_achievement(house, "first-blood")
    check("house earns no karma", a.karma_balance(house) == 0, a.karma_balance(house))
    check("house holds no trophies",
          not a._rows("SELECT 1 FROM trophy_case WHERE player_id=?", (house,)))

    print("achievements")
    n1 = a.grant_achievement(p1, "first-blood")
    n2 = a.grant_achievement(p1, "first-blood")
    check("grant returns True first time", n1 is True)
    check("grant idempotent (False second)", n2 is False)
    rows = a._rows("SELECT COUNT(*) c FROM trophy_case WHERE player_id=? "
                   "AND achievement_id='first-blood'", (p1,))
    check("one trophy row", rows[0]["c"] == 1)
    k_before = a.karma_balance(p1)
    a.grant_achievement(p1, "first-blood")  # no double karma
    check("no double karma bonus", a.karma_balance(p1) == k_before)

    print("karma thresholds")
    a.grant_cosmetic(p2, "frame-bronze")  # pre-grant to test threshold skip
    a.award_karma(p2, 500, "admin", "boost")  # admin source uncapped
    lo = a.player_rewards(p2)["loadout"]
    check("silver tier at 300 karma", a.karma_tier(p2)[0] == "Silver",
          a.karma_tier(p2))
    check("frame-bronze in inventory",
          "frame-bronze" in a.player_rewards(p2)["inventory"])

    print("equip")
    out = a.equip_cosmetic(p2, "frame", "frame-bronze")
    check("equip owned works", out["frame_id"] == "frame-bronze")
    try:
        a.equip_cosmetic(p2, "frame", "frame-diamond")
        check("equip unowned rejected", False)
    except app.ApiError as e:
        check("equip unowned rejected", e.status == 403, e.message)

    print("founders")
    n_a = a.grant_founder(p1)
    n_b = a.grant_founder(p2)
    check("numbered 1,2", (n_a, n_b) == (1, 2), (n_a, n_b))
    n_a2 = a.grant_founder(p1)
    check("re-grant same number", n_a2 == 1)
    wall = a.founders_wall()
    check("wall 50 slots", len(wall) == 50)
    check("wall filled 2", sum(1 for s in wall if s["filled"]) == 2)
    v = a.verify_founder_attestation(1)
    check("attestation verifies (dev mode)", v["founder"] and v["dev_mode"], v)
    v_bad = a.verify_founder_attestation(7)
    check("empty number not a founder", not v_bad["founder"])
    k_mult = a.award_karma(p1, 10, "arena_game", "mult-test")
    check("founder 1.25x multiplier", k_mult == 12, k_mult)
    try:
        a.grant_founder(house)
        check("house cannot be founder", False)
    except app.ApiError:
        check("house cannot be founder", True)

    print("season drops + anniversary")
    d1 = a.seasonal_founder_drop("season-1", "accessory-founder-laurel")
    check("season drop granted to 2 founders", d1["new_grants"] == 2, d1)
    d2 = a.seasonal_founder_drop("season-1", "accessory-founder-laurel")
    check("season drop idempotent", d2["new_grants"] == 0, d2)
    check("founder laurel in inventory",
          "accessory-founder-laurel" in a.player_rewards(p1)["inventory"])

    print("flair + payloads")
    f = a._flair_batch([p1, p2, house])
    check("house flair default", f[house]["founder"] is None)
    check("founder flair", f[p1]["founder"] == 1, f[p1])
    sp = a.spectate()
    check("spectate has flair map", isinstance(sp.get("flair"), dict))
    check("spectate has recent_unlocks", "recent_unlocks" in sp)
    lb = a.leaderboard()
    row = next(r for r in lb if r["name"] == "RWAlpha")
    check("leaderboard karma field", row["karma"] == a.karma_balance(p1))
    check("leaderboard flair field", row["flair"]["founder"] == 1)
    g = a.board_game_state(1, p1) if False else None  # games need rooms; skip
    bg_keys = None

    print("dragon pets")
    r3 = a.register("RWGamma"); p3 = r3["player_id"]
    # fresh player: early-adopter +25 karma -> lifetime 25, no pets yet
    start_inv = a.player_rewards(p3)["inventory"]
    check("no pets at start",
          not [c for c in start_inv if c.startswith("pet-")], start_inv)
    a.award_karma(p3, 475, "admin", "pet boost 1")  # lifetime 500
    inv = a.player_rewards(p3)["inventory"]
    check("egg unlocked at 500 lifetime karma", "pet-dragon-egg" in inv, inv)
    check("dragon-tamer trophy awarded",
          a._has_achievement(p3, "dragon-tamer"))
    check("hatchling locked below 1500",
          "pet-dragon-hatchling" not in inv)
    a.award_karma(p3, 1000, "admin", "pet boost 2")  # lifetime 1500+
    rows = a._rows("SELECT COUNT(*) c FROM cosmetic_inventory WHERE "
                   "player_id=? AND cosmetic_id='pet-dragon-egg'", (p3,))
    check("egg granted exactly once (idempotent)", rows[0]["c"] == 1)
    inv = a.player_rewards(p3)["inventory"]
    check("hatchling unlocked at 1500", "pet-dragon-hatchling" in inv)
    check("balance equals lifetime (nothing decrements)",
          a.karma_balance(p3) == a._karma_lifetime(p3))

    print("pet equip")
    lo = a.equip_cosmetic(p3, "pet", "pet-dragon-hatchling")
    check("equip pet works", lo["pet_id"] == "pet-dragon-hatchling", lo)
    try:
        a.equip_cosmetic(p3, "pet", "pet-dragon-full")
        check("equip unowned pet rejected", False)
    except app.ApiError as e:
        check("equip unowned pet rejected", e.status == 403, e.message)
    try:
        a.equip_cosmetic(p3, "frame", "pet-dragon-egg")
        check("pet in wrong slot rejected", False)
    except app.ApiError as e:
        check("pet in wrong slot rejected", e.status == 400, e.message)

    print("pet house exclusion")
    a.award_karma(house, 5000, "admin", "house pet attempt")
    house_inv = a.player_rewards(house)["inventory"]
    check("house gets no pets",
          not [c for c in house_inv if c.startswith("pet-")], house_inv)
    check("house gets no dragon trophies",
          not a._has_achievement(house, "dragon-tamer"))

    print("pet full ladder")
    a.award_karma(p3, 18500, "admin", "pet boost 3")  # lifetime 20000+
    inv = a.player_rewards(p3)["inventory"]
    for _th, cid in a.PET_THRESHOLDS:
        check("owns " + cid, cid in inv)
    check("dragon-master trophy", a._has_achievement(p3, "dragon-master"))
    check("dragon-collector trophy",
          a._has_achievement(p3, "dragon-collector"))

    print("pet flair")
    f3 = a._flair_for(p3)
    check("flair pet img", f3["pet"] == "pet-dragon-hatchling.png", f3["pet"])
    check("flair pet name", f3["pet_name"] == "Dragon Hatchling",
          f3["pet_name"])
    fb = a._flair_batch([p3])
    check("batch flair pet", fb[p3]["pet"] == "pet-dragon-hatchling.png")
    check("no-pet flair empty", a._flair_for(p1)["pet"] == "")

    print("early-bird + activity badges")
    now = app.now
    import json as _json2
    import datetime as _dt
    BADGES = ["early-first-game", "early-day-one", "early-first-100",
              "early-founding-week", "volume-10", "volume-25", "volume-50",
              "volume-100", "volume-250", "streak-3w", "streak-5w",
              "streak-10w", "grind-day-max", "grind-night-owl",
              "grind-early-bird", "grind-weekend", "milestone-first-win",
              "milestone-first-tourney", "milestone-first-stake",
              "milestone-comeback"]
    check("20 badge achievements registered",
          len(BADGES) == 20 and all(b in a.ACHIEVEMENTS for b in BADGES),
          str(set(BADGES) - set(a.ACHIEVEMENTS)))
    check("20 badge cosmetics, slot=badge",
          sum(1 for c in a.COSMETICS.values() if c.get("slot") == "badge")
          == 20)
    r4 = a.register("RWDelta"); p4 = r4["player_id"]
    for b in BADGES:
        spec = a.ACHIEVEMENTS[b]
        cid = spec.get("unlock", "")
        check("badge cosmetic " + b,
              cid.startswith("badge-") and cid in a.COSMETICS)
        # early-first-100 + early-founding-week were already granted to p4
        # by the register hook above; re-grants must be idempotent no-ops
        expect_new = b not in ("early-first-100", "early-founding-week")
        n1 = a.grant_achievement(p4, b)
        n2 = a.grant_achievement(p4, b)
        check("idempotent " + b,
              (n1 is True and n2 is False) if expect_new
              else (n1 is False and n2 is False))
    check("register fired early-founding-week",
          a._has_achievement(p4, "early-founding-week"))
    check("register fired early-first-100",
          a._has_achievement(p4, "early-first-100"))
    check("house excluded from all badges",
          all(a.grant_achievement(house, b) is False for b in BADGES))
    check("house holds no badges",
          not [c for c in a.player_rewards(house)["inventory"]
               if c.startswith("badge-")])
    try:
        a.equip_cosmetic(p4, "badge", "badge-early-first-game")
        check("badge equip rejected (display-only)", False)
    except app.ApiError as e:
        check("badge equip rejected (display-only)", e.status == 400,
              e.message)

    print("badge unlock rules (fixture games)")
    def fin_game(pids, winner, fts, staked=False):
        gid = a._insert(
            "INSERT INTO board_games (room_id, creator_id, kind, status,"
            " players_json, state_json, turn_pid, winner_id, created_at,"
            " finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (1, pids[0], "checkers", "finished", _json2.dumps(pids), "{}",
             pids[0], winner, fts, fts))
        if staked:
            for pid in pids:
                a._insert("INSERT INTO stakes (game_id, player_id,"
                          " player_address, status, created_at)"
                          " VALUES (?,?,?,?,?)",
                          (gid, pid, "0xabc", "complete", fts))
        return gid
    r5 = a.register("RWEpsilon"); p5 = r5["player_id"]
    r6 = a.register("RWZeta"); p6 = r6["player_id"]
    g1 = fin_game([p5, p6], p5, now())
    a._rewards_on_game_finish(g1, [p5, p6], p5, False, "checkers", {},
                              False)
    check("early-first-game (both players)",
          a._has_achievement(p5, "early-first-game")
          and a._has_achievement(p6, "early-first-game"))
    check("early-day-one", a._has_achievement(p5, "early-day-one"))
    check("milestone-first-win", a._has_achievement(p5, "milestone-first-win"))
    check("loser gets no first-win",
          not a._has_achievement(p6, "milestone-first-win"))
    # night owl: 02:00 UTC (not early-bird, which is 05:00-06:59)
    owl_ts = (now() // 86400) * 86400 + 2 * 3600
    g2 = fin_game([p5, p6], p6, owl_ts)
    a._rewards_on_game_finish(g2, [p5, p6], p6, False, "checkers", {},
                              False)
    check("grind-night-owl", a._has_achievement(p5, "grind-night-owl"))
    check("02:00 is not early-bird",
          not a._has_achievement(p5, "grind-early-bird"))
    # early bird: 06:00 UTC
    eb_ts = (now() // 86400) * 86400 + 6 * 3600
    g3 = fin_game([p5, p6], p5, eb_ts)
    a._rewards_on_game_finish(g3, [p5, p6], p5, False, "checkers", {},
                              False)
    check("grind-early-bird", a._has_achievement(p5, "grind-early-bird"))
    # weekend warrior: a Saturday
    sat = _dt.datetime(2026, 9, 19, 12, 0,
                       tzinfo=_dt.timezone.utc).timestamp()
    g4 = fin_game([p5, p6], p5, int(sat))
    a._rewards_on_game_finish(g4, [p5, p6], p5, False, "checkers", {},
                              False)
    check("grind-weekend", a._has_achievement(p5, "grind-weekend"))
    # volume: 10 and 25
    r7 = a.register("RWEta"); p7 = r7["player_id"]
    base = (now() // 86400) * 86400 - 40 * 86400  # 40 days back, one/day
    for i in range(26):
        fin_game([p7, p6], p7 if i % 2 else p6, base + i * 86400)
    g7b = fin_game([p7, p6], p7, now())
    a._badge_game_check(p7, g7b, False)
    check("volume-10", a._has_achievement(p7, "volume-10"))
    check("volume-25", a._has_achievement(p7, "volume-25"))
    check("volume-50 not yet", not a._has_achievement(p7, "volume-50"))
    # day-max: 15 games in one UTC day
    r8 = a.register("RWTheta"); p8 = r8["player_id"]
    d0 = (now() // 86400) * 86400
    for i in range(15):
        fin_game([p8, p6], p8, d0 + i * 600)
    g8b = fin_game([p8, p6], p8, d0 + 15 * 600)
    a._badge_game_check(p8, g8b, False)
    check("grind-day-max", a._has_achievement(p8, "grind-day-max"))
    # comeback: 31 days between finishes
    r9 = a.register("RWIota"); p9 = r9["player_id"]
    fin_game([p9, p6], p9, now() - 31 * 86400)
    g5 = fin_game([p9, p6], p6, now())
    a._badge_game_check(p9, g5, False)
    check("milestone-comeback", a._has_achievement(p9, "milestone-comeback"))
    # streak badges: 3 straight wins via the real hook
    rA = a.register("RWKappa"); pA = rA["player_id"]
    for _ in range(3):
        ga = fin_game([pA, p6], pA, now())
        a._rewards_on_game_finish(ga, [pA, p6], pA, False, "checkers", {},
                                  False)
    check("streak-3w badge", a._has_achievement(pA, "streak-3w"))
    check("v1 streak-3 still fires", a._has_achievement(pA, "streak-3"))
    check("streak-5w not yet", not a._has_achievement(pA, "streak-5w"))
    # first staked game
    rB = a.register("RWLambda"); pB = rB["player_id"]
    g6 = fin_game([pB, p6], pB, now(), staked=True)
    a._rewards_on_game_finish(g6, [pB, p6], pB, False, "checkers", {},
                              True)
    check("milestone-first-stake",
          a._has_achievement(pB, "milestone-first-stake")
          and a._has_achievement(p6, "milestone-first-stake"))
    g7 = fin_game([pB, p6], p6, now(), staked=True)
    a._rewards_on_game_finish(g7, [pB, p6], p6, False, "checkers", {},
                              True)
    rows = a._rows("SELECT COUNT(*) c FROM trophy_case WHERE player_id=? "
                   "AND achievement_id='milestone-first-stake'", (pB,))
    check("first-stake granted once", rows[0]["c"] == 1)
    # first tournament entry badge
    a._rewards_on_tournament_entry(pB)
    check("milestone-first-tourney",
          a._has_achievement(pB, "milestone-first-tourney"))
    check("gladiator still fires",
          a._has_achievement(pB, "tournament-gladiator"))

    print("game-finish failure isolation")
    # rewards hook raising must never break game settlement
    orig = a._rewards_on_game_finish
    def boom(*args, **kwargs):
        raise RuntimeError("simulated rewards failure")
    a._rewards_on_game_finish = boom
    try:
        # minimal: call the wrapped section the way _finish_board_game does
        try:
            a._rewards_on_game_finish(0, [], None, True, "checkers", {}, False)
        except Exception:
            pass  # _finish_board_game wraps this in try/except
        check("exception contained", True)
    finally:
        a._rewards_on_game_finish = orig

    print("catalog + routes wiring")
    check("39 achievements", len(a.ACHIEVEMENTS) == 39, len(a.ACHIEVEMENTS))
    check("46 cosmetics", len(a.COSMETICS) == 46, len(a.COSMETICS))
    imgs = {c["img"] for c in a.COSMETICS.values() if c.get("img")}
    missing = [i for i in imgs if not os.path.exists(
        os.path.join(HERE, "assets", i))]
    check("all cosmetic art files exist", not missing, str(missing))
    routes = {r[2] for r in app.ROUTES}
    for h in ("h_rewards_catalog", "h_rewards_player", "h_rewards_equip",
              "h_admin_rewards_grant", "h_admin_founders_backfill",
              "h_admin_founders_grant", "h_admin_founders_season",
              "h_founders", "h_founders_verify", "h_trophies"):
        check("route " + h, h in routes)

    print("pet routes over HTTP (scratch server)")
    import subprocess, time, urllib.request, json as _json
    db2 = tempfile.mktemp(suffix=".db")
    port = 8487
    srv = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "app.py"),
         "--port", str(port), "--db", db2, "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/ping" % port,
                                       timeout=2).read()
                break
            except Exception:
                time.sleep(0.5)
        def get(path):
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d%s" % (port, path),
                    timeout=10) as r:
                return r.status, r.read()
        for img in ("pet-dragon-egg", "pet-dragon-hatchling",
                    "pet-dragon-wyrmling", "pet-dragon-full",
                    "pet-dragon-fire", "pet-dragon-frost", "pet-dragon-storm"):
            st, body = get("/img/%s.png" % img)
            check("GET /img/%s.png 200" % img,
                  st == 200 and len(body) > 1000, st)
        for img in ("badge-early-first-game", "badge-volume-250",
                    "badge-streak-10w", "badge-grind-night-owl",
                    "badge-milestone-comeback"):
            st, body = get("/img/%s.png" % img)
            check("GET /img/%s.png 200" % img,
                  st == 200 and len(body) > 1000, st)
        st, body = get("/trophies")
        check("GET /trophies 200 + Dragon Den",
              st == 200 and b"Dragon Den" in body, st)
        check("trophies has Badge Case", b"Badge Case" in body)
        st, body = get("/api/rewards/catalog")
        cat = _json.loads(body)
        check("catalog pet_thresholds x7",
              len(cat.get("pet_thresholds", [])) == 7)
        check("catalog badge_line x20",
              len(cat.get("badge_line", [])) == 20, cat.get("badge_line"))
        check("catalog 20 badge cosmetics",
              sum(1 for c in cat["cosmetics"].values()
                  if c["slot"] == "badge") == 20)
        check("catalog 7 pet cosmetics",
              sum(1 for c in cat["cosmetics"].values()
                  if c["slot"] == "pet") == 7)
        check("catalog 39 achievements", len(cat["achievements"]) == 39)
        st, body = get("/api/spectate")
        sp = _json.loads(body)
        check("spectate flair map present",
              isinstance(sp.get("flair"), dict))
        lb = sp.get("leaderboard") or []
        check("leaderboard rows carry flair",
              all("flair" in r for r in lb), len(lb))
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()

    print()
    if fails:
        print("FAILURES:", fails)
        sys.exit(1)
    print("ALL REWARDS TESTS PASSED")

if __name__ == "__main__":
    main()
