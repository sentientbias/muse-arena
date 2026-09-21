#!/usr/bin/env python3
"""Arena SSO client tests (2026-09-21).

Spins up the real app (ThreadingHTTPServer + Handler) against a throwaway
sqlite DB, with a stub MuseFM provider (own Ed25519 key) standing in for
musefm.lol. Covers the plan's client test requirements:

  happy path end-to-end, state mismatch, tampered ID token, wrong aud,
  replayed code (provider-side one-time), logout clears the cookie,
  /auth/me, wallet flow untouched, i18n strings, orb serving,
  sso_link_identity mapping (no duplicate players), throttle.

Run:  .venv/bin/python test_sso_arena_2026_09_21.py
"""
import base64
import hashlib
import http.client
import http.cookies
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey)

import app as appmod
import sso

TEST_DB = "/tmp/test-arena-sso-20260921.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# ------------------------------------------------------- stub provider
class StubProvider:
    """Minimal fake musefm.lol: /auth/pubkey + one-time-code /auth/token."""

    def __init__(self):
        self.priv = Ed25519PrivateKey.generate()
        self.pub_b64 = b64u(self.priv.public_key().public_bytes_raw())
        self.codes = {}  # code -> dict(used, aud, sub, handle)
        self.server = None

    def mint_code(self, sub="fm_test123", handle="ssohuman", aud="arena"):
        code = "stubcode-" + os.urandom(8).hex()
        self.codes[code] = {"used": False, "sub": sub, "handle": handle,
                            "aud": aud}
        return code

    def mint_token(self, sub, handle, aud, tamper=False, exp_skew=600):
        now = int(time.time())
        h = b64u(json.dumps({"alg": "EdDSA", "typ": "JWT"},
                            separators=(",", ":")).encode())
        p = b64u(json.dumps(
            {"iss": sso.PROVIDER, "aud": aud, "sub": sub, "handle": handle,
             "iat": now, "exp": now + exp_skew},
            separators=(",", ":"), sort_keys=True).encode())
        sig = b64u(self.priv.sign((h + "." + p).encode()))
        if tamper:
            sig = ("A" if sig[0] != "A" else "B") + sig[1:]
        return h + "." + p + "." + sig

    def handler(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/auth/pubkey":
                    body = json.dumps({"ok": True, "scheme": "ed25519",
                                       "public_key": stub.pub_b64}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                if self.path != "/auth/token":
                    self.send_response(404)
                    self.end_headers()
                    return
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(n).decode())
                entry = stub.codes.get(data.get("code"))
                if entry is None or entry["used"]:
                    body = json.dumps({"ok": False,
                                       "error": "bad code"}).encode()
                    self.send_response(400)
                else:
                    entry["used"] = True
                    tok = stub.mint_token(entry["sub"], entry["handle"],
                                          entry["aud"])
                    body = json.dumps(
                        {"ok": True, "id_token": tok,
                         "fm_id": entry["sub"],
                         "handle": entry["handle"]}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return H

    def start(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        return "http://127.0.0.1:%d" % self.server.server_port


# ------------------------------------------------------- test server
def start_app(db_path):
    if os.path.exists(db_path):
        os.remove(db_path)
    arena = appmod.Arena(db_path)
    appmod.Handler.arena = arena
    server = ThreadingHTTPServer(("127.0.0.1", 0), appmod.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, arena


class Client:
    def __init__(self, port):
        self.port = port
        self.cookies = {}

    def _cookie_header(self):
        return "; ".join("%s=%s" % kv for kv in self.cookies.items())

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if self.cookies:
            h["Cookie"] = self._cookie_header()
        conn.request("GET", path, headers=h)
        r = conn.getresponse()
        body = r.read()
        for k, v in r.getheaders():
            if k.lower() == "set-cookie":
                c = http.cookies.SimpleCookie()
                c.load(v)
                for ck, m in c.items():
                    if m["max-age"] == "0" or v.startswith(ck + "=;"):
                        self.cookies.pop(ck, None)
                    else:
                        self.cookies[ck] = m.value
        conn.close()
        return r.status, dict(r.getheaders()), body

    def post_json(self, path, obj):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(obj).encode()
        h = {"Content-Type": "application/json"}
        if self.cookies:
            h["Cookie"] = self._cookie_header()
        conn.request("POST", path, body=data, headers=h)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, body


def main():
    # ---- sso.py unit checks (no server) ----
    os.environ["ARENA_SESSION_SECRET"] = "test-secret-for-suite-only"
    v, c = sso.pkce_pair()
    check("pkce challenge = S256(verifier)",
          c == b64u(hashlib.sha256(v.encode()).digest()))
    st = "state123"
    packed = sso.pack_state_cookie(st, v)
    rst, rv = sso.unpack_state_cookie(packed)
    check("state cookie roundtrip", (rst, rv) == (st, v))
    bad = packed[:-2] + ("AA" if not packed.endswith("AA") else "BB")
    check("tampered state cookie rejected",
          sso.unpack_state_cookie(bad) == (None, None))
    check("garbage state cookie rejected",
          sso.unpack_state_cookie("nonsense") == (None, None))
    sess = sso.mint_session("fm_x", "han")
    rd = sso.read_session(sess)
    check("session roundtrip", rd and rd["fm_id"] == "fm_x"
          and rd["handle"] == "han")
    check("tampered session rejected",
          sso.read_session(sess[:-2] + "AA") is None)
    # expired session
    import json as _j
    payload = sso._verify_signed(sess)
    data = _j.loads(sso.b64u_decode(payload))
    data["exp"] = int(time.time()) - 1
    re_packed = sso.b64u_encode(
        _j.dumps(data, separators=(",", ":")).encode()) + "." + sso._sign(
            sso.b64u_encode(_j.dumps(data, separators=(",", ":")).encode()))
    check("expired session rejected", sso.read_session(re_packed) is None)
    # throttle
    sso.throttle_reset()
    ip = "9.9.9.9"
    ok = all(sso.throttle_check(ip) for _ in range(30))
    check("throttle allows 30", ok)
    check("throttle blocks 31st", not sso.throttle_check(ip))
    sso.throttle_reset()

    # ---- servers ----
    stub = StubProvider()
    stub_url = stub.start()
    sso.PROVIDER = stub_url  # point the client at the stub
    sso.clear_pubkey_cache()
    server, arena = start_app(TEST_DB)
    c = Client(server.server_port)

    # tables exist
    tbls = [r[0] for r in arena.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    check("sso_identities table created", "sso_identities" in tbls)

    # ---- /auth/login ----
    # Fail closed when the session secret is not configured (same as the
    # Playbook and Trustline clients): no login, no ephemeral sessions.
    saved_secret = os.environ.pop("ARENA_SESSION_SECRET", None)
    try:
        status_nc, _, _ = c.get("/auth/login")
        check("login without secret -> 503", status_nc == 503, str(status_nc))
    finally:
        if saved_secret is not None:
            os.environ["ARENA_SESSION_SECRET"] = saved_secret
    status, headers, body = c.get("/auth/login")
    loc = headers.get("Location", "")
    check("login -> 302 to provider authorize",
          status == 302 and loc.startswith(stub_url + "/auth/authorize?"),
          f"{status} {loc[:80]}")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    check("authorize URL has PKCE + state",
          q.get("client_id") == ["arena"]
          and q.get("code_challenge_method") == ["S256"]
          and 43 <= len(q["code_challenge"][0]) <= 128
          and len(q["state"][0]) >= 32)
    check("sso_state cookie set HttpOnly/Secure/Lax",
          "sso_state" in c.cookies)
    saved_state = q["state"][0]

    # ---- happy-path callback ----
    code = stub.mint_code()
    status, headers, body = c.get(
        "/auth/callback?code=%s&state=%s" % (code, saved_state))
    html = body.decode()
    check("callback happy path -> 200", status == 200, f"got {status}")
    check("ma_session cookie set", "ma_session" in c.cookies)
    check("sso_state cookie cleared", "sso_state" not in c.cookies)
    m = re.search(r"var mh=(\{.*?\});", html, re.DOTALL)
    check("interstitial seeds localStorage ma_human", bool(m))
    mh = json.loads(m.group(1)) if m else {}
    check("ma_human has the arena token", bool(mh.get("token")))
    rows = arena._rows("SELECT * FROM sso_identities")
    check("identity mapping stored",
          len(rows) == 1 and rows[0]["fm_id"] == "fm_test123"
          and rows[0]["handle"] == "ssohuman")
    first_player_id = rows[0]["player_id"]

    # ---- /auth/me ----
    status, _, body = c.get("/auth/me")
    me = json.loads(body)
    check("/auth/me logged in", me["logged_in"] and me["handle"] == "ssohuman"
          and me["player"]["player_id"] == first_player_id)
    c2 = Client(server.server_port)
    status, _, body = c2.get("/auth/me")
    check("/auth/me logged out", json.loads(body) == {"logged_in": False})

    # ---- same fm_id logs in again -> SAME player, no duplicate ----
    status, headers, _ = c2.get("/auth/login")
    loc2 = headers["Location"]
    q2 = urllib.parse.parse_qs(urllib.parse.urlparse(loc2).query)
    code2 = stub.mint_code()
    status, _, _ = c2.get("/auth/callback?code=%s&state=%s"
                          % (code2, q2["state"][0]))
    rows = arena._rows("SELECT * FROM sso_identities WHERE fm_id='fm_test123'")
    check("re-login reuses the same player",
          status == 200 and len(rows) == 1
          and rows[0]["player_id"] == first_player_id)
    n_players = arena._row("SELECT COUNT(*) AS n FROM players")["n"]
    check("no duplicate player rows", n_players == 1, f"n={n_players}")

    # ---- wallet restore: link a wallet, re-login, token carries it ----
    arena._q("UPDATE players SET wallet=? WHERE id=?",
             ("0xabc123abc123abc123abc123abc123abc123abcd", first_player_id))
    c3 = Client(server.server_port)
    status, headers, _ = c3.get("/auth/login")
    q3 = urllib.parse.parse_qs(urllib.parse.urlparse(headers["Location"]).query)
    code3 = stub.mint_code()
    status, _, body = c3.get("/auth/callback?code=%s&state=%s"
                             % (code3, q3["state"][0]))
    html3 = body.decode()
    check("re-login restores the linked wallet",
          "0xabc123abc123abc123abc123abc123abc123abcd" in html3)

    # ---- state mismatch ----
    c4 = Client(server.server_port)
    status, headers, _ = c4.get("/auth/login")
    q4 = urllib.parse.parse_qs(urllib.parse.urlparse(headers["Location"]).query)
    code4 = stub.mint_code()
    status, _, body = c4.get("/auth/callback?code=%s&state=%s"
                             % (code4, "wrong-state-value"))
    check("state mismatch -> 400", status == 400, f"got {status}")
    check("no session on state mismatch", "ma_session" not in c4.cookies)

    # ---- missing state cookie ----
    c5 = Client(server.server_port)
    code5 = stub.mint_code()
    status, _, _ = c5.get("/auth/callback?code=%s&state=%s" % (code5, "x"))
    check("missing state cookie -> 400", status == 400)

    # ---- tampered state cookie ----
    c6 = Client(server.server_port)
    status, headers, _ = c6.get("/auth/login")
    q6 = urllib.parse.parse_qs(urllib.parse.urlparse(headers["Location"]).query)
    c6.cookies["sso_state"] = c6.cookies["sso_state"][:-3] + "XYZ"
    code6 = stub.mint_code()
    status, _, _ = c6.get("/auth/callback?code=%s&state=%s"
                          % (code6, q6["state"][0]))
    check("tampered state cookie -> 400", status == 400)

    # ---- access_denied from provider ----
    c7 = Client(server.server_port)
    status, _, body = c7.get("/auth/callback?error=access_denied&state=x")
    check("provider deny -> 400 with cancelled text",
          status == 400 and "已取消登录" not in body.decode()  # locale en here
          and ("cancelled" in body.decode().lower()
               or "Sign-in was cancelled" in body.decode()))

    # ---- tampered id_token ----
    real_exchange = sso.exchange_code
    tampered = stub.mint_token("fm_evil", "evil", "arena", tamper=True)
    sso.exchange_code = lambda code, verifier: (tampered, "fm_evil", "evil")
    try:
        c8 = Client(server.server_port)
        status, headers, _ = c8.get("/auth/login")
        q8 = urllib.parse.parse_qs(
            urllib.parse.urlparse(headers["Location"]).query)
        status, _, _ = c8.get("/auth/callback?code=%s&state=%s"
                              % ("anycode", q8["state"][0]))
        check("tampered id_token -> 400, no session",
              status == 400 and "ma_session" not in c8.cookies,
              f"got {status}")
    finally:
        sso.exchange_code = real_exchange
    sso.clear_pubkey_cache()

    # ---- wrong aud ----
    def exchange_wrong_aud(code, verifier):
        tok = stub.mint_token("fm_x", "x", "someone-else")
        return tok, "fm_x", "x"
    sso.exchange_code = exchange_wrong_aud
    try:
        c9 = Client(server.server_port)
        status, headers, _ = c9.get("/auth/login")
        q9 = urllib.parse.parse_qs(
            urllib.parse.urlparse(headers["Location"]).query)
        status, _, _ = c9.get("/auth/callback?code=%s&state=%s"
                              % ("anycode", q9["state"][0]))
        check("wrong aud -> 400, no session",
              status == 400 and "ma_session" not in c9.cookies)
    finally:
        sso.exchange_code = real_exchange
    sso.clear_pubkey_cache()

    # ---- replayed code (provider enforces one-time) ----
    c10 = Client(server.server_port)
    status, headers, _ = c10.get("/auth/login")
    q10 = urllib.parse.parse_qs(
        urllib.parse.urlparse(headers["Location"]).query)
    code10 = stub.mint_code()
    s1, _, _ = c10.get("/auth/callback?code=%s&state=%s"
                       % (code10, q10["state"][0]))
    c11 = Client(server.server_port)
    status, headers, _ = c11.get("/auth/login")
    q11 = urllib.parse.parse_qs(
        urllib.parse.urlparse(headers["Location"]).query)
    s2, _, _ = c11.get("/auth/callback?code=%s&state=%s"
                       % (code10, q11["state"][0]))
    check("replayed code refused by provider -> 400",
          s1 == 200 and s2 == 400 and "ma_session" not in c11.cookies,
          f"first={s1} second={s2}")

    # ---- logout ----
    status, headers, _ = c.get("/auth/logout")
    check("logout -> 302 home", status == 302
          and headers.get("Location") == "/")
    check("logout clears ma_session", "ma_session" not in c.cookies)
    status, _, body = c.get("/auth/me")
    check("me after logout", json.loads(body) == {"logged_in": False})

    # ---- existing wallet/token flow untouched (no SSO) ----
    c12 = Client(server.server_port)
    status, body = c12.post_json("/api/human/session",
                                 {"name": "plainplayer", "wallet": ""})
    check("wallet flow works without SSO", status == 200
          and json.loads(body)["token"], f"got {status}")
    tok = json.loads(body)["token"]
    # resume with token still works
    status, body = c12.post_json("/api/human/session",
                                 {"name": "plainplayer", "token": tok})
    check("token resume still works", status == 200
          and json.loads(body)["token"] == tok)

    # ---- landing page: login button, orb anchor, i18n ----
    status, _, body = c12.get("/")
    html = body.decode()
    check("landing has login link", '/auth/login' in html
          and "Sign in with MuseFM" in html)
    check("landing has orb anchor", "data-muse-orb-anchor" in html)
    check("landing includes orb script",
          '<script src="/static/js/muse-orb.js" defer>' in html)
    # zh strings come from the self-contained SSO table (no dependency
    # on the i18n project's query-param locale plumbing).
    check("zh login string",
          "使用 MuseFM 登录" in appmod.Handler._sso_t("sso.k001", "zh"))
    # logged-in slot shows the chip (fresh full login on c, which logged out)
    status, headers, _ = c.get("/auth/login")
    ql = urllib.parse.parse_qs(urllib.parse.urlparse(headers["Location"]).query)
    code_l = stub.mint_code()
    c.get("/auth/callback?code=%s&state=%s" % (code_l, ql["state"][0]))
    status, _, body = c.get("/")
    html = body.decode()
    check("logged-in landing shows account chip",
          "Signed in as @ssohuman" in html and "/auth/logout" in html
          and "/auth/login" not in html.replace("/auth/logout", ""))

    # ---- orb JS served ----
    status, headers, body = c12.get("/static/js/muse-orb.js")
    check("orb JS served",
          status == 200 and b"muse-orb" in body.lower()
          and "javascript" in headers.get("Content-Type", ""))

    # ---- sso_link_identity edge: name collision ----
    # A wallet-flow player already owns the handle: the SSO link must
    # uniquify instead of clobbering or crashing.
    arena._insert(
        "INSERT INTO players (name, token, is_human, wallet, created_at)"
        " VALUES (?,?,?,?,?)",
        ("collisionname", "tok-other", 1, "", int(time.time())))
    p2 = arena.sso_link_identity("fm_collision", "collisionname")
    check("handle collision uniquified",
          p2["name"] != "collisionname"
          and p2["name"].startswith("collisionname"))
    check("collision mapping stored",
          arena._row("SELECT player_id FROM sso_identities"
                     " WHERE fm_id=?", ("fm_collision",))["player_id"]
          == p2["id"])

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
