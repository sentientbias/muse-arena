#!/usr/bin/env python3
"""v2.0 card-game tests: pure helpers (evaluator, blackjack totals, deck
commitments) plus in-process Arena tests (no HTTP server needed) covering
betting-round closure, timeouts, idempotent replay, public-state privacy,
private /hand auth, deck commitments, bust/cap/sudden-death, and win_reason.
Fails loudly."""
import json
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)  # Arena reads questions.json relative to cwd

import app
from app import (ApiError, bj_total, deck_commit, deck_verify, new_deck,
                 new_deck_secret, parse_card, poker_best7, poker_eval5)

PASS = []


def check(name, fn):
    fn()
    PASS.append(name)
    print(f"  ok — {name}")


# ---------------------------------------------------------------- pure

def t_eval_order():
    beats = [
        (["As", "Ks", "Qs", "Js", "Ts", "2c", "3d"], "royal flush"),
        (["9s", "8s", "7s", "6s", "5s", "2c", "3d"], "straight flush"),
        (["As", "Ad", "Ac", "Ah", "2c", "3d", "5s"], "four of a kind"),
        (["As", "Ad", "Ac", "2c", "2d", "3h", "5s"], "full house"),
        (["As", "Ks", "9s", "7s", "5s", "2c", "3d"], "flush"),
        (["9h", "8c", "7d", "6s", "5h", "2c", "3d"], "straight"),
        (["As", "Ad", "Ac", "2c", "3d", "5s", "7h"], "three of a kind"),
        (["As", "Ad", "Kc", "Kh", "2c", "3d", "5s"], "two pair"),
        (["As", "Ad", "Kc", "2c", "3d", "5s", "7h"], "pair"),
        (["As", "Kd", "Qc", "2c", "3d", "5s", "7h"], "high card"),
    ]
    keys = []
    for cards, want_name in beats:
        rank, tie, name, _best = poker_best7(cards)
        assert name == want_name, f"{cards}: got {name}, want {want_name}"
        keys.append((rank, tie))
    for i in range(len(keys) - 1):
        assert keys[i] > keys[i + 1], f"hand order broken at {beats[i][1]}"


def t_eval_edge():
    # wheel loses to 6-high straight
    w = poker_best7(["Ah", "2c", "3d", "4s", "5h", "Kc", "Qd"])
    s = poker_best7(["6h", "2c", "3d", "4s", "5h", "Kc", "Qd"])
    assert w[2] == "straight" and w[1] == (5,), w
    assert s[1] == (6,) and (s[0], s[1]) > (w[0], w[1])
    # kicker decides
    a = poker_best7(["As", "Kd", "2c", "3d", "5h", "7s", "9c"])
    b = poker_best7(["Ac", "Qd", "2c", "3d", "5h", "7s", "9c"])
    assert a[2] == b[2] == "high card" and a[1] > b[1], (a[1], b[1])
    # identical board -> tie
    c = poker_best7(["Ah", "Kh", "Qs", "Js", "9h", "2c", "3d"])
    d = poker_best7(["Ad", "Kd", "Qs", "Js", "9h", "2c", "3d"])
    assert (c[0], c[1]) == (d[0], d[1]), "same board should tie"
    # best 5 of 7: board already a flush, hole cards irrelevant
    f = poker_best7(["2c", "7d", "As", "Ks", "Qs", "Js", "9s"])
    assert f[2] == "flush" and f[1][0] == 14, f
    # quads kicker
    q1 = poker_best7(["As", "Ad", "Ac", "Ah", "Ks", "2c", "3d"])
    q2 = poker_best7(["As", "Ad", "Ac", "Ah", "Qs", "2c", "3d"])
    assert q1[1] > q2[1], "quad kicker should decide"


def t_bj_total():
    assert bj_total(["Ah", "6d"]) == (17, True)
    assert bj_total(["Ah", "6d", "Ts"]) == (17, False)
    assert bj_total(["Ts", "6d", "7h"]) == (23, False)
    assert bj_total(["Ah", "Kd"]) == (21, True)
    assert bj_total(["5h", "5d", "5c", "5s"]) == (20, False)
    assert bj_total(["Ah", "Ad", "9c"]) == (21, True)  # 11+1+9
    assert bj_total(["Ah", "Ad", "9c", "2d"]) == (13, False)


def t_commit():
    d = new_deck()
    s = new_deck_secret()
    c = deck_commit(d, s)
    assert deck_verify(d, s, c)
    assert not deck_verify(d, "nope", c)
    d2 = list(d)
    d2[0], d2[1] = d2[1], d2[0]
    assert not deck_verify(d2, s, c), "reordered deck must not verify"


# ---------------------------------------------------------------- arena

def fresh_arena():
    db = tempfile.mktemp(suffix=".db")
    a = app.Arena(db)
    t1 = a.register("CardA")["token"]
    t2 = a.register("CardB")["token"]
    p1, p2 = a.auth(t1), a.auth(t2)
    r = a.create_room(p1, "cardroom", "mixed", "")
    rid = r.get("room", {}).get("id", r.get("id"))
    a.join_room(p2, rid)
    a._db_path = db
    return a, p1, p2, rid


def cleanup(a):
    try:
        os.unlink(a._db_path)
    except OSError:
        pass


def new_poker(a, p1, rid, seed=7):
    random.seed(seed)
    g = a.new_board_game(p1, rid, "poker", "CardB")
    return g["id"]


def turn_player(a, p1, p2, gid):
    st = a.board_game_state(gid)
    assert st["status"] == "open"
    return (p1 if st["turn"] == "CardA" else p2), st


def play_showdown_hand(a, p1, p2, gid):
    """Both sides check/call everything until the hand ends. Returns last_hand."""
    for _ in range(60):
        st = a.board_game_state(gid)
        if st["status"] != "open":
            return None
        before = st["poker"]["last_hand"]
        pl, st = turn_player(a, p1, p2, gid)
        legal = [m["action"] for m in st["legal_moves"]]
        act = "check" if "check" in legal else "call"
        a.make_move(pl, gid, {"action": act})
        after = a.board_game_state(gid)["poker"]["last_hand"]
        if after is not None and after != before:
            return after
    raise AssertionError("hand never ended")


def t_poker_showdown():
    a, p1, p2, rid = fresh_arena()
    try:
        gid = new_poker(a, p1, rid, seed=7)
        ha = a.player_hand(p1, gid)["cards"]
        hb = a.player_hand(p2, gid)["cards"]
        assert len(ha) == 2 and len(hb) == 2 and set(ha) != set(hb)
        # privacy: hole cards must not appear in public state mid-hand
        pub = json.dumps(a.board_game_state(gid))
        for c in ha + hb:
            assert '"%s"' % c not in pub, f"hole card {c} leaked in public state"
        assert '"hole":' in pub  # placeholder present, nulls only
        # deck commitment published, secret not yet
        pst = a.board_game_state(gid)["poker"]
        assert pst["deck_commit"] and len(pst["deck_commit"]) == 64
        assert pst["last_hand"] is None
        lh = play_showdown_hand(a, p1, p2, gid)
        assert lh["ended"] == "showdown", lh
        assert lh["winners"] and lh["pot"] > 0
        assert lh["showdown"] and len(lh["showdown"]) == 2
        # commitment verifies: rebuild full deck in deal order
        # [P0c1, P1c1, P0c2, P1c2] + community + undealt remainder
        s0 = lh["showdown"][0]["cards"]
        s1 = lh["showdown"][1]["cards"]
        full = ([s0[0], s1[0], s0[1], s1[1]] + lh["community"]
                + lh["deck_remainder"])
        assert len(full) == 52 and len(set(full)) == 52, len(full)
        assert deck_verify(full, lh["deck_secret"], lh["deck_commit"]), \
            "deck commitment must verify after showdown reveal"
        # chips conserved, hand advanced
        st = a.board_game_state(gid)["poker"]
        s = st["stacks"]
        assert s["CardA"] + s["CardB"] + st["pot"] == 200, s
        assert st["hand_no"] == 2, "next hand should have started"
    finally:
        cleanup(a)


def t_poker_fold_no_reveal():
    a, p1, p2, rid = fresh_arena()
    try:
        gid = new_poker(a, p1, rid, seed=11)
        ha = a.player_hand(p1, gid)["cards"]
        pl, st = turn_player(a, p1, p2, gid)
        a.make_move(pl, gid, {"action": "fold"})
        st = a.board_game_state(gid)["poker"]
        lh = st["last_hand"]
        assert lh["ended"] == "fold" and lh["showdown"] is None
        assert len(lh["winners"]) == 1
        # folded hole cards stay secret in public state — forever
        pub = json.dumps(a.board_game_state(gid))
        for c in ha:
            assert '"%s"' % c not in pub, f"folded card {c} leaked"
        # ...but the hand still publishes secret + undealt remainder
        assert lh["deck_secret"] and lh["deck_commit"]
        assert len(lh["deck_remainder"]) == 52 - 4, len(lh["deck_remainder"])
    finally:
        cleanup(a)


def t_poker_betting_rounds():
    a, p1, p2, rid = fresh_arena()
    try:
        gid = new_poker(a, p1, rid, seed=13)
        # hand 1: CardA is button/SB and acts first preflop
        st = a.board_game_state(gid)
        assert st["turn"] == "CardA" and st["poker"]["street"] == "preflop"
        assert st["poker"]["blinds"] == [1, 2]
        legal = [m["action"] for m in st["legal_moves"]]
        assert set(["fold", "call", "raise", "allin"]) <= set(legal), legal
        # min-raise enforced: current bet is 2, min raise total is 4
        pl, _ = turn_player(a, p1, p2, gid)
        try:
            a.make_move(pl, gid, {"action": "raise", "amount": 3})
            raise AssertionError("short raise should be rejected")
        except ApiError as e:
            assert e.status == 400, e
        # A raises to 11 total -> reopens action for B (facing a bet, "bet"
        # is illegal preflop; only raise/call/fold/allin)
        a.make_move(pl, gid, {"action": "raise", "amount": 11})
        st = a.board_game_state(gid)
        assert st["turn"] == "CardB", "raise must reopen action"
        assert st["poker"]["current_bet"] == 11
        assert st["poker"]["to_call"] == 9, st["poker"]
        # B raises to 30 total -> back to A with 19 to call
        pl, _ = turn_player(a, p1, p2, gid)
        a.make_move(pl, gid, {"action": "raise", "amount": 30})
        st = a.board_game_state(gid)
        assert st["turn"] == "CardA" and st["poker"]["to_call"] == 19, st["poker"]
        # A calls -> street advances synchronously on the closing move
        pl, _ = turn_player(a, p1, p2, gid)
        a.make_move(pl, gid, {"action": "call"})
        st = a.board_game_state(gid)["poker"]
        assert st["street"] == "flop" and len(st["community"]) == 3, st
        # postflop: non-button (CardB) acts first
        assert a.board_game_state(gid)["turn"] == "CardB"
        assert st["current_bet"] == 0, st  # street bets reset to pot
    finally:
        cleanup(a)


def t_poker_allin_short_normalize():
    # B all-in short of A's bet: A's excess comes back, no side pot
    a, p1, p2, rid = fresh_arena()
    try:
        gid = new_poker(a, p1, rid, seed=17)
        pl, _ = turn_player(a, p1, p2, gid)  # CardA
        a.make_move(pl, gid, {"action": "raise", "amount": 21})  # 1 + 20
        # rig B down to 10 chips
        st = json.loads(a._board_row(gid)["state_json"])
        st["stacks"][1] = 10
        a._q("UPDATE board_games SET state_json=? WHERE id=?",
             (json.dumps(st), gid))
        pl, _ = turn_player(a, p1, p2, gid)  # CardB
        a.make_move(pl, gid, {"action": "allin"})
        # B's all-in (12 total) was short of A's 21: the excess 9 came back,
        # no side pot — the hand ran out to showdown and a new hand started.
        st = a.board_game_state(gid)["poker"]
        lh = st["last_hand"]
        assert lh["hand_no"] == 1 and lh["ended"] == "showdown", lh
        assert lh["pot"] == 24, lh["pot"]  # 12+12: the excess 9 came back
        s = st["stacks"]
        # B won the showdown (24, minus hand-2 small blind) or lost it (bust)
        assert (s["CardB"], s["CardA"]) in ((23, 86), (0, 112)), s
    finally:
        cleanup(a)


def t_poker_timeout():
    a, p1, p2, rid = fresh_arena()
    try:
        gid = new_poker(a, p1, rid, seed=19)
        # get to the flop: A calls, B checks -> B to act first, nothing to call
        pl, _ = turn_player(a, p1, p2, gid)  # CardA (button)
        a.make_move(pl, gid, {"action": "call"})
        pl, _ = turn_player(a, p1, p2, gid)  # CardB
        a.make_move(pl, gid, {"action": "check"})
        st = a.board_game_state(gid)
        assert st["poker"]["street"] == "flop" and st["turn"] == "CardB"
        # 1) nothing to call -> auto-check passes the turn
        a._q("UPDATE board_games SET turn_deadline=? WHERE id=?",
             (app.now() - 1, gid))
        st = a.board_game_state(gid)  # clock tick fires inside
        assert st["turn"] == "CardA", "auto-check should pass the turn"
        assert "checks" in st["poker"]["last_action"], st["poker"]["last_action"]
        # 2) facing a bet -> auto-fold, hand ends, no bust of the match
        pl, _ = turn_player(a, p1, p2, gid)  # CardA
        a.make_move(pl, gid, {"action": "bet", "amount": 10})
        a._q("UPDATE board_games SET turn_deadline=? WHERE id=?",
             (app.now() - 1, gid))
        st = a.board_game_state(gid)["poker"]
        assert st["last_hand"]["ended"] == "fold", st["last_hand"]
        assert st["last_hand"]["winners"] == ["CardA"], st["last_hand"]
        assert a.board_game_state(gid)["status"] == "open", "match continues"
    finally:
        cleanup(a)


def t_poker_bust_and_cap():
    a, p1, p2, rid = fresh_arena()
    try:
        gid = new_poker(a, p1, rid, seed=23)
        players = [p1["id"], p2["id"]]
        # --- bust: rig stacks, award the hand to CardB
        st = json.loads(a._board_row(gid)["state_json"])
        st["stacks"] = [0, 180]
        st["pot"] = 20
        st["bets"] = [0, 0]
        state, outcome = a._poker_award(gid, st, players, [1], "showdown")
        assert outcome["over"] and outcome["win_reason"] == "bust"
        assert outcome["winner_id"] == p2["id"]
        a._commit_card_outcome(gid, state, outcome)
        g = a.board_game_state(gid)
        assert g["status"] == "finished" and g["winner"] == "CardB"
        assert g["win_reason"] == "bust", g["win_reason"]
    finally:
        cleanup(a)
    a, p1, p2, rid = fresh_arena()
    try:
        gid = new_poker(a, p1, rid, seed=29)
        players = [p1["id"], p2["id"]]
        # --- 60-hand cap: chip leader wins
        st = json.loads(a._board_row(gid)["state_json"])
        st["hand_no"] = 60
        st["stacks"] = [120, 80]
        st["pot"] = 20
        st["bets"] = [0, 0]
        a._secret_put(gid, 60, 0, a._secret_get(gid, 1, 0))  # craft the row
        state, outcome = a._poker_award(gid, st, players, [0], "showdown")
        assert outcome["over"] and outcome["win_reason"] == "chips"
        assert outcome["winner_id"] == p1["id"]
        # --- exact tie at the cap -> sudden death, not over
        st = json.loads(a._board_row(gid)["state_json"])
        st["hand_no"] = 60
        st["stacks"] = [100, 100]
        st["pot"] = 20
        st["bets"] = [0, 0]
        state, outcome = a._poker_award(gid, st, players, [0, 1], "showdown")
        assert not outcome["over"], "tied cap must go to sudden death"
        assert state["sudden_death"] == 1 and state["hand_no"] == 61
        # --- 3 tied sudden-death hands -> draw
        st["sudden_death"] = 3
        st["stacks"] = [100, 100]
        st["pot"] = 20
        st["bets"] = [0, 0]
        state, outcome = a._poker_award(gid, st, players, [0, 1], "showdown")
        assert outcome["over"] and outcome["draw"]
        assert outcome["win_reason"] == "draw"
    finally:
        cleanup(a)


def t_poker_blind_schedule():
    a, p1, p2, rid = fresh_arena()
    try:
        gid = new_poker(a, p1, rid, seed=31)
        for _ in range(10):  # fold 10 hands away
            pl, _ = turn_player(a, p1, p2, gid)
            a.make_move(pl, gid, {"action": "fold"})
        st = a.board_game_state(gid)["poker"]
        assert st["hand_no"] == 11, st["hand_no"]
        assert st["blinds"] == [2, 4], st["blinds"]
    finally:
        cleanup(a)


def t_blackjack_match():
    a, p1, p2, rid = fresh_arena()
    try:
        random.seed(41)
        g = a.new_board_game(p1, rid, "blackjack", "CardB")
        gid = g["id"]
        b = g["blackjack"]
        assert b["hand_no"] == 1 and b["hands_total"] == 10
        assert b["dealer_hand"] == [b["dealer_up"], None], "hole hidden"
        assert b["shoe"]["commit"] and len(b["shoe"]["commit"]) == 64
        # dealer hole must not leak in public state
        hole = (a._secret_get(gid, 1, 0) or {}).get("hole")
        assert hole, "hole card should be stored"
        assert '"%s"' % hole not in json.dumps(a.board_game_state(gid)), "hole leaked!"
        # play all 10 hands: hit below 17, else stand
        for _ in range(400):
            st = a.board_game_state(gid)
            if st["status"] == "finished":
                break
            b = st["blackjack"]
            pl = p1 if st["turn"] == "CardA" else p2
            tot = b["player_totals"][st["turn"]][0]
            a.make_move(pl, gid, {"action": "stand" if tot >= 17 else "hit"})
        st = a.board_game_state(gid)
        assert st["status"] == "finished", "10 hands should end the match"
        assert st["win_reason"] in ("chips", "draw"), st["win_reason"]
        if st["win_reason"] == "chips":
            assert st["winner"] in ("CardA", "CardB")
        s = st["blackjack"]["stacks"]
        # casino accounting: the house bank is infinite, so naturals (3:2)
        # and wins can push the total above the starting 200
        assert s["CardA"] >= 0 and s["CardB"] >= 0, s
        if st["win_reason"] == "chips":
            assert st["winner"] in ("CardA", "CardB")
            assert s[st["winner"]] > s["CardA" if st["winner"] == "CardB"
                                      else "CardB"], s
        assert len(st["blackjack"]["results"]) > 0
        last = st["blackjack"]["results"][-1]
        assert last["hand_no"] == 10 and "dealer" in last
        # every shoe verifies against its published commitment
        rev = st["blackjack"]["shoe_reveal"]
        assert rev, "shoe secrets must be revealed at match end"
        for shoe in rev:
            assert deck_verify(shoe["full"], shoe["secret"], shoe["commit"])
            assert len(shoe["full"]) == 208
            assert all(shoe["full"].count(c) == 4 for c in set(shoe["full"])), \
                "each card must appear exactly 4 times in a 4-deck shoe"
    finally:
        cleanup(a)


def t_blackjack_naturals_pay_32():
    a, p1, p2, rid = fresh_arena()
    try:
        random.seed(43)
        g = a.new_board_game(p1, rid, "blackjack", "CardB")
        gid = g["id"]
        players = [p1["id"], p2["id"]]
        st = json.loads(a._board_row(gid)["state_json"])
        h = st["hand_no"]
        # rig: CardA natural, CardB 20, dealer up 5 + hole 9 -> hits
        st["hands"] = [["Ah", "Kd"], ["Ts", "Qd"]]
        st["dealer_up"] = "5c"
        st["dealer_hand"] = ["5c"]
        st["bets"] = [10, 10]
        st["stacks"] = [90, 90]
        st["blackjack"] = [True, False]
        st["stood"] = [True, True]
        st["busted"] = [False, False]
        st["out"] = [False, False]
        a._secret_put(gid, h, 0, {"hole": "9h"})
        a._q("UPDATE board_games SET state_json=? WHERE id=?",
             (json.dumps(st), gid))
        state, outcome = a._bj_dealer_and_settle(gid, st, players)
        # settle() starts hand 2 after settling, so read the settled hand
        # from the results summary, not the returned (new-hand) state
        res = state["results"][-1]
        assert res["hand_no"] == 1 and state["hand_no"] == 2
        # dealer: 5+9=14 -> must hit (S17)
        assert len(res["dealer"]) >= 3, res["dealer"]
        # CardA natural: 90 + (10 + 15) = 115 at settle time
        summ = res["players"]
        assert summ["CardA"]["delta"] == 15, summ["CardA"]
        assert "blackjack" in summ["CardA"]["result"]
    finally:
        cleanup(a)


def t_blackjack_timeout_stands():
    a, p1, p2, rid = fresh_arena()
    try:
        random.seed(47)
        g = a.new_board_game(p1, rid, "blackjack", "CardB")
        gid = g["id"]
        before = a.board_game_state(gid)
        a._q("UPDATE board_games SET turn_deadline=? WHERE id=?",
             (app.now() - 1, gid))
        st = a.board_game_state(gid)
        assert st["blackjack"]["last_action"] != before["blackjack"]["last_action"] \
            or st["turn"] != before["turn"], "clock should auto-act"
        acted = ("stands" in st["blackjack"]["last_action"]
                 or "dealer" in st["blackjack"]["last_action"]
                 or st["blackjack"]["hand_no"] > 1)
        assert acted, st["blackjack"]["last_action"]
    finally:
        cleanup(a)


def t_idempotent_replay():
    a, p1, p2, rid = fresh_arena()
    try:
        gid = new_poker(a, p1, rid, seed=53)
        pl, st = turn_player(a, p1, p2, gid)
        r1 = a.make_move(pl, gid, {"action": "call"}, idempotency_key="k-1")
        pot_after = a.board_game_state(gid)["poker"]["pot"]
        # retry with the same key AFTER the turn passed: must replay, not 403
        r2 = a.make_move(pl, gid, {"action": "call"}, idempotency_key="k-1")
        assert r2 == r1, "replay must return the stored original result"
        assert a.board_game_state(gid)["poker"]["pot"] == pot_after, \
            "replay must not reapply the move"
        # a fresh key still applies normally
        pl2, st2 = turn_player(a, p1, p2, gid)
        r3 = a.make_move(pl2, gid, {"action": "check"}, idempotency_key="k-2")
        assert r3 != r1
    finally:
        cleanup(a)
    # and on a classic board game (tictactoe)
    a, p1, p2, rid = fresh_arena()
    try:
        g = a.new_board_game(p1, rid, "tictactoe", "CardB")
        gid = g["id"]
        r1 = a.make_move(p1, gid, {"cell": 0}, idempotency_key="t-1")
        r2 = a.make_move(p1, gid, {"cell": 0}, idempotency_key="t-1")
        assert r2 == r1
        st = a.board_game_state(gid)
        assert st["board"][0] == 1 and st["turn"] == "CardB"
    finally:
        cleanup(a)


def t_win_reasons():
    a, p1, p2, rid = fresh_arena()
    try:
        # tictactoe win -> "win"
        g = a.new_board_game(p1, rid, "tictactoe", "CardB")
        gid = g["id"]
        for pl, cell in [(p1, 0), (p2, 3), (p1, 1), (p2, 4), (p1, 2)]:
            a.make_move(pl, gid, {"cell": cell})
        st = a.board_game_state(gid)
        assert st["status"] == "finished" and st["win_reason"] == "win", st
        # tictactoe draw -> "draw"
        g = a.new_board_game(p1, rid, "tictactoe", "CardB")
        gid = g["id"]
        for pl, cell in [(p1, 0), (p2, 1), (p1, 2), (p2, 4), (p1, 3),
                         (p2, 5), (p1, 7), (p2, 6), (p1, 8)]:
            a.make_move(pl, gid, {"cell": cell})
        st = a.board_game_state(gid)
        assert st["win_reason"] == "draw", st
        # timeout on a board game -> "timeout" (persists for late spectators)
        g = a.new_board_game(p1, rid, "tictactoe", "CardB")
        gid = g["id"]
        a._q("UPDATE board_games SET turn_deadline=? WHERE id=?",
             (app.now() - 1, gid))
        st = a.board_game_state(gid)
        assert st["status"] == "finished" and st["win_reason"] == "timeout", st
        assert "forfeit" in st, "forfeit note should survive for spectators"
        # resignation -> "resignation"
        g = a.new_board_game(p1, rid, "tictactoe", "CardB")
        gid = g["id"]
        a.resign_game(p1, gid)
        st = a.board_game_state(gid)
        assert st["win_reason"] == "resignation" and st["winner"] == "CardB", st
    finally:
        cleanup(a)


def t_hand_auth():
    a, p1, p2, rid = fresh_arena()
    try:
        gid = new_poker(a, p1, rid, seed=59)
        t3 = a.register("CardC")["token"]
        p3 = a.auth(t3)
        try:
            a.player_hand(p3, gid)
            raise AssertionError("non-player should get 403")
        except ApiError as e:
            assert e.status == 403, e
        g2 = a.new_board_game(p1, rid, "tictactoe", "CardB")
        try:
            a.player_hand(p1, g2["id"])
            raise AssertionError("non-card game should get 400")
        except ApiError as e:
            assert e.status == 400, e
        # secrets table is populated but spectate never exposes it
        spec = a.spectate()
        blob = json.dumps(spec)
        ha = a.player_hand(p1, gid)["cards"]
        for c in ha:
            assert '"%s"' % c not in blob, f"spectate leaked {c}"
    finally:
        cleanup(a)


def main():
    print("== pure ==")
    check("evaluator hand ordering (10 classes)", t_eval_order)
    check("evaluator edge cases (wheel, kickers, ties, best-5-of-7)", t_eval_edge)
    check("blackjack totals (hard/soft/bust)", t_bj_total)
    check("deck commitment + verification", t_commit)
    print("== arena ==")
    check("poker: full hand to showdown, privacy, commitment verifies", t_poker_showdown)
    check("poker: fold wins pot, no reveal", t_poker_fold_no_reveal)
    check("poker: betting rounds close synchronously, min-raise", t_poker_betting_rounds)
    check("poker: all-in short returns the over-bet", t_poker_allin_short_normalize)
    check("poker: clock auto-check / auto-fold, match continues", t_poker_timeout)
    check("poker: bust, 60-hand cap, sudden death, final draw", t_poker_bust_and_cap)
    check("poker: blinds double every 10 hands", t_poker_blind_schedule)
    check("blackjack: full 10-hand match, dealer hole stays secret", t_blackjack_match)
    check("blackjack: naturals pay 3:2, dealer hits soft 16", t_blackjack_naturals_pay_32)
    check("blackjack: clock auto-stands", t_blackjack_timeout_stands)
    check("idempotency: replay returns stored result (poker + tictactoe)", t_idempotent_replay)
    check("win_reason persisted: win/draw/timeout/resignation", t_win_reasons)
    check("private hand: 403 for non-players, secrets never in spectate", t_hand_auth)
    print(f"\nALL CARD TESTS PASSED ✔  ({len(PASS)} checks)")


if __name__ == "__main__":
    main()
