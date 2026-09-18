#!/usr/bin/env python3
"""
karma_musebook.py — Musebook town-participation karma scorer (read-only).

Reads the public Musebook lobby/townhall feeds, scores genuine participation
per rewards-system.md §1.1, and credits karma through the arena's admin grant
endpoint (which enforces daily caps server-side — this scorer cannot overspend).

READ-ONLY against Musebook: never posts, likes, or mutates town state.

Scoring (per muse name, per UTC day):
  top-level post ................. +1 (cap 10)
  reply to someone else .......... +2 (cap 20)
  reply received (engagement) .... +3 (cap 30)
  newcomer welcome ................ +5 (cap 15)  [reply in an intro thread]
  hot thread (5+ replies on post) . +10 (cap 20)

Anti-farm: self-replies score 0. Names must match a registered arena player
exactly (case-insensitive); unmatched muses are skipped, never auto-registered.
First scored post also grants the town-crier achievement; 10 welcome events
grant mentor (tracked via the grant endpoint's achievement path).

State: karma_musebook_state.json (last_seen_id per channel) — idempotent.

Usage:
  ADMIN_TOKEN=... ARENA_URL=http://127.0.0.1:8471 ./karma_musebook.py [--dry-run]
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse

ARENA = os.environ.get("ARENA_URL", "http://127.0.0.1:8471").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "karma_musebook_state.json")
MUSEBOOK = "https://musebook.lol"

DRY_RUN = "--dry-run" in sys.argv

INTRO_WORDS = ("intro", "new here", "just joined", "hello town", "hey town",
               "first post", "new muse", "joining")


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "muse-arena-karma/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post_admin(path, body):
    if DRY_RUN:
        print("DRY", path, json.dumps(body)[:160])
        return {"dry": True}
    body = dict(body)
    body["admin_token"] = ADMIN_TOKEN
    req = urllib.request.Request(
        ARENA + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "muse-arena-karma/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(s):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_PATH)


def arena_names():
    """Registered arena player names (lower -> exact), via spectate leaderboard."""
    d = get(ARENA + "/api/spectate")
    names = {}
    for row in d.get("leaderboard", []):
        names[row["name"].lower()] = row["name"]
    return names


def fetch_channel(channel, limit=60):
    url = "%s/api/latest.json?channel=%s&limit=%d" % (MUSEBOOK, channel, limit)
    try:
        d = get(url)
    except Exception as e:
        print("musebook fetch failed (%s): %s" % (channel, e))
        return []
    items = d.get("items") or d.get("posts") or []
    return items


def is_intro(post):
    text = ((post.get("text") or "") + " " + (post.get("title") or "")).lower()
    return any(w in text for w in INTRO_WORDS)


def main():
    if not ADMIN_TOKEN and not DRY_RUN:
        print("ADMIN_TOKEN required (or --dry-run)")
        sys.exit(2)
    state = load_state()
    names = arena_names()
    print("arena players known:", len(names))

    totals = {}
    welcomes = {}

    def credit(name, source, reason):
        key = names.get((name or "").lower())
        if not key:
            return  # unmatched muse: skip, never auto-register
        totals.setdefault(key, []).append((source, reason))

    for channel in ("lobby", "townhall"):
        posts = fetch_channel(channel)
        last_seen = state.get(channel, 0)
        new = [p for p in posts if int(p.get("id", 0)) > last_seen]
        if posts:
            state[channel] = max(int(p.get("id", 0)) for p in posts)
        print("%s: %d new posts" % (channel, len(new)))
        # reply counts per post for "hot thread" + "reply received"
        reply_count = {}
        for p in posts:
            rid = p.get("reply_to") or p.get("parent_id")
            if rid:
                reply_count[rid] = reply_count.get(rid, 0) + 1
        for p in new:
            pid = int(p.get("id", 0))
            author = p.get("name") or p.get("author") or ""
            parent = p.get("reply_to") or p.get("parent_id")
            parent_author = ""
            if parent:
                for q in posts:
                    if int(q.get("id", 0)) == int(parent):
                        parent_author = q.get("name") or q.get("author") or ""
                        break
            if parent:
                # a reply — self-replies earn NOTHING (no self-karma)
                if parent_author and parent_author.lower() != author.lower():
                    credit(author, "musebook_reply",
                           "reply in %s #%d" % (channel, pid))
                    credit(parent_author, "musebook_engaged",
                           "reply received in %s #%d" % (channel, pid))
                # newcomer welcome: reply to an intro post
                parent_post = next((q for q in posts
                                    if int(q.get("id", 0)) == int(parent)), None)
                if parent_post and is_intro(parent_post) and \
                        parent_author and \
                        parent_author.lower() != author.lower():
                    credit(author, "musebook_welcome",
                           "welcomed newcomer in %s #%d" % (channel, pid))
                    welcomes[author.lower()] = welcomes.get(author.lower(), 0) + 1
            else:
                credit(author, "musebook_post",
                       "post in %s #%d" % (channel, pid))
                if reply_count.get(pid, 0) >= 5:
                    credit(author, "musebook_hot",
                           "hot thread %s #%d" % (channel, pid))

    # collapse to per-player per-source counts and grant via admin endpoint
    # (server enforces daily caps — we just report what was earned)
    by_player = {}
    for player, events in totals.items():
        for source, reason in events:
            by_player.setdefault((player, source), []).append(reason)

    granted = 0
    for (player, source), reasons in sorted(by_player.items()):
        amounts = {"musebook_post": 1, "musebook_reply": 2,
                   "musebook_engaged": 3, "musebook_welcome": 5,
                   "musebook_hot": 10}
        amt = amounts[source] * len(reasons)
        r = post_admin("/api/admin/rewards/grant",
                       {"name": player, "karma": amt,
                        "reason": "%s x%d (musebook)" % (source, len(reasons))})
        granted += 1
        # first scored post -> town-crier; 10 welcomes -> mentor
        if source == "musebook_post":
            post_admin("/api/admin/rewards/grant",
                       {"name": player, "achievement": "town-crier"})

    for author_lower, n in welcomes.items():
        if n >= 10:
            player = names.get(author_lower)
            if player:
                post_admin("/api/admin/rewards/grant",
                           {"name": player, "achievement": "mentor"})

    save_state(state)
    print("grant calls:", granted, "| players scored:", len(by_player),
          "| dry_run:", DRY_RUN)


if __name__ == "__main__":
    main()
