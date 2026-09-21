#!/usr/bin/env python3
"""MuseFM SSO client for Muse Arena.

Client half of the family global-login flow (see
~/workspace/global-login/SSO_PLAN.md): /auth/login builds a PKCE pair +
state and redirects to the MuseFM provider; /auth/callback verifies the
state, exchanges the code server-side, verifies the Ed25519 ID token, and
mints a local HMAC-signed session cookie.

Only stdlib + `cryptography` (Ed25519). No framework — this is used by the
raw http.server app in app.py.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request

PROVIDER = "https://musefm.lol"
CLIENT_ID = "arena"
REDIRECT_URI = "https://muse-arena.onrender.com/auth/callback"

STATE_COOKIE = "sso_state"      # 10-minute, HMAC-signed CSRF state
SESSION_COOKIE = "ma_session"   # 30-day, HMAC-signed local session
STATE_TTL = 10 * 60
SESSION_TTL = 30 * 24 * 3600

_TOKEN_TTL_SKEW = 60            # clock-skew leeway on ID token exp
_PUBKEY_TTL = 3600              # provider pubkey cache lifetime

_HTTP_TIMEOUT = 15

# Simple in-memory per-IP throttle for the /auth/* endpoints (plan item 8).
_THROTTLE_MAX = 30
_THROTTLE_WINDOW = 60
_throttle = {}
_throttle_lock = threading.Lock()


def _secret():
    """Server secret for HMAC-signing cookies. From the environment when
    set (persistent sessions); otherwise a random per-process secret with
    a loud warning (sessions die on restart, but never forgeable)."""
    s = os.environ.get("ARENA_SESSION_SECRET", "")
    if s:
        return s.encode("utf-8")
    # Ephemeral fallback — generated once per process.
    global _EPHEMERAL_SECRET
    try:
        return _EPHEMERAL_SECRET
    except NameError:
        _EPHEMERAL_SECRET = secrets.token_bytes(32)
        sys.stderr.write(
            "[arena][sso] WARNING: ARENA_SESSION_SECRET is not set — "
            "using an ephemeral secret. SSO sessions will not survive "
            "restarts. Set ARENA_SESSION_SECRET on the Render dashboard.\n")
        return _EPHEMERAL_SECRET


def configured():
    """True when ARENA_SESSION_SECRET is set (persistent SSO sessions).

    The /auth/* entry points fail closed without it — same as the
    Playbook and Trustline clients — instead of minting sessions that
    would silently die on the next restart.
    """
    return bool(os.environ.get("ARENA_SESSION_SECRET", ""))


def b64u_encode(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64u_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(value):
    return b64u_encode(hmac.new(_secret(), value.encode("utf-8"),
                                hashlib.sha256).digest())


def _signed(value):
    return value + "." + _sign(value)


def _verify_signed(signed_value):
    """Return the payload of a value.sig cookie, or None."""
    if not signed_value or "." not in signed_value:
        return None
    value, _, sig = signed_value.rpartition(".")
    if not value or not sig:
        return None
    if not hmac.compare_digest(_sign(value), sig):
        return None
    return value


# ------------------------------------------------------------------ PKCE/state

def pkce_pair():
    """(verifier, challenge): S256 PKCE pair per the plan."""
    verifier = secrets.token_urlsafe(64)
    challenge = b64u_encode(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge


def new_state():
    """(state, signed_cookie_value): 32 random bytes, HMAC-signed cookie."""
    state = secrets.token_urlsafe(32)
    return state, _signed(state)


def parse_state_cookie(cookie_value):
    """Return the state from a signed cookie, or None if bad signature."""
    return _verify_signed(cookie_value)


def pack_state_cookie(state, verifier):
    """Pack (state, verifier) into one HMAC-signed cookie value.

    Format: state.b64u(verifier).sig — the signature covers both, so the
    verifier can't be swapped between /auth/login and /auth/callback.
    """
    payload = state + "." + b64u_encode(verifier.encode("utf-8"))
    return payload + "." + _sign(payload)


def unpack_state_cookie(cookie_value):
    """Return (state, verifier), or (None, None) on any failure."""
    payload = _verify_signed(cookie_value)
    if payload is None:
        return None, None
    state, _, verifier_b64 = payload.rpartition(".")
    if not state or not verifier_b64:
        return None, None
    try:
        verifier = b64u_decode(verifier_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None, None
    if not verifier:
        return None, None
    return state, verifier


# ------------------------------------------------------------------ session

def mint_session(fm_id, handle):
    """HMAC-signed session cookie value for {fm_id, handle, exp}."""
    payload = b64u_encode(json.dumps(
        {"fm_id": fm_id, "handle": handle,
         "exp": int(time.time()) + SESSION_TTL},
        separators=(",", ":")).encode("utf-8"))
    return _signed(payload)


def read_session(cookie_value):
    """Return the session dict, or None (bad sig / expired / malformed)."""
    payload = _verify_signed(cookie_value)
    if payload is None:
        return None
    try:
        data = json.loads(b64u_decode(payload))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("exp"), int) or data["exp"] < time.time():
        return None
    if not data.get("fm_id") or not isinstance(data["fm_id"], str):
        return None
    return data


def session_cookie_header(value, max_age=SESSION_TTL):
    base = ("%s=%s; Path=/; Max-Age=%d; HttpOnly; Secure; SameSite=Lax"
            % (SESSION_COOKIE, value, max_age))
    return base


def clear_session_cookie_header():
    return ("%s=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
            % SESSION_COOKIE)


def state_cookie_header(signed_state):
    return ("%s=%s; Path=/; Max-Age=%d; HttpOnly; Secure; SameSite=Lax"
            % (STATE_COOKIE, signed_state, STATE_TTL))


def clear_state_cookie_header():
    return ("%s=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
            % STATE_COOKIE)


# ------------------------------------------------------------------ provider

class ProviderError(Exception):
    pass


def build_authorize_url(state, challenge):
    q = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    return PROVIDER + "/auth/authorize?" + q


def _post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "muse-arena-sso/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:200]
        except Exception:
            detail = ""
        raise ProviderError("provider HTTP %d: %s" % (e.code, detail))
    except Exception as e:
        raise ProviderError("provider unreachable: %r" % (e,))


_pubkey_cache = {"key": None, "at": 0.0}
_pubkey_lock = threading.Lock()


def provider_pubkey(force_refresh=False):
    """Ed25519PublicKey for the provider, cached for an hour."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey)
    now = time.time()
    with _pubkey_lock:
        if (not force_refresh and _pubkey_cache["key"] is not None
                and now - _pubkey_cache["at"] < _PUBKEY_TTL):
            return _pubkey_cache["key"]
    req = urllib.request.Request(
        PROVIDER + "/auth/pubkey",
        headers={"User-Agent": "muse-arena-sso/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise ProviderError("pubkey fetch failed: %r" % (e,))
    if not body.get("ok") or body.get("scheme") != "ed25519":
        raise ProviderError("bad pubkey response")
    try:
        key = Ed25519PublicKey.from_public_bytes(
            b64u_decode(body["public_key"]))
    except Exception as e:
        raise ProviderError("bad pubkey bytes: %r" % (e,))
    with _pubkey_lock:
        _pubkey_cache["key"] = key
        _pubkey_cache["at"] = now
    return key


def clear_pubkey_cache():
    with _pubkey_lock:
        _pubkey_cache["key"] = None
        _pubkey_cache["at"] = 0.0


def exchange_code(code, verifier):
    """POST the auth code to /auth/token. Returns (id_token, fm_id, handle)."""
    body = _post_json(PROVIDER + "/auth/token", {
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    })
    if not body.get("ok") or not body.get("id_token"):
        raise ProviderError("token exchange refused: %s"
                            % json.dumps(body)[:200])
    return body["id_token"], body.get("fm_id", ""), body.get("handle", "")


def verify_id_token(id_token):
    """Verify the Ed25519 signature and iss/aud/exp claims.

    Returns the claims dict. Raises ProviderError on anything wrong.
    Never accepts a token from anyone but the provider.
    """
    try:
        h_b, p_b, s_b = id_token.split(".")
        claims = json.loads(b64u_decode(p_b))
    except (ValueError, TypeError) as e:
        raise ProviderError("malformed id_token: %r" % (e,))
    try:
        pub = provider_pubkey()
        pub.verify(b64u_decode(s_b), (h_b + "." + p_b).encode("ascii"))
    except ProviderError:
        raise
    except Exception:
        # Signature failed — try once with a fresh pubkey (rotation),
        # then fail closed.
        try:
            pub = provider_pubkey(force_refresh=True)
            pub.verify(b64u_decode(s_b), (h_b + "." + p_b).encode("ascii"))
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError("bad id_token signature: %r" % (e,))
    now = time.time()
    if claims.get("iss") != PROVIDER:
        raise ProviderError("bad iss")
    if claims.get("aud") != CLIENT_ID:
        raise ProviderError("bad aud")
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp < now - _TOKEN_TTL_SKEW:
        raise ProviderError("id_token expired")
    iat = claims.get("iat")
    if isinstance(iat, (int, float)) and iat > now + _TOKEN_TTL_SKEW:
        raise ProviderError("id_token from the future")
    if not claims.get("sub") or not isinstance(claims["sub"], str):
        raise ProviderError("id_token missing sub")
    return claims


# ------------------------------------------------------------------ throttle

def throttle_check(ip):
    """In-memory per-IP throttle for /auth/*. Returns True when the
    request may proceed, False when it should get a 429."""
    if not ip:
        return True
    now = time.time()
    with _throttle_lock:
        hits = _throttle.get(ip, [])
        hits = [t for t in hits if now - t < _THROTTLE_WINDOW]
        if len(hits) >= _THROTTLE_MAX:
            _throttle[ip] = hits
            return False
        hits.append(now)
        _throttle[ip] = hits
        return True


def throttle_reset():
    with _throttle_lock:
        _throttle.clear()
