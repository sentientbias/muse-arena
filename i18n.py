#!/usr/bin/env python3
"""i18n helper for Muse Arena — JSON catalogs, t() lookup, locale resolution.

No dependencies — stdlib only.

Catalog layout (next to this file)::
    i18n/en.json   # source of truth: {"meta": {...}, "strings": {key: {"source":..., "note":...}}}
    i18n/zh.json   # {"meta": {...}, "strings": {key: "translation" | {"one":..,"other":..}}}
    i18n/hi.json

Conventions (per ~/workspace/i18n/PLAN-i18n.md):
- Keys are stable and opaque (e.g. "watch.k001"). NEVER the English source string.
- Placeholders use {name} syntax, interpolated by t().
- Missing key in a non-English locale -> English fallback + miss logged. NEVER blank.
- Plural forms: {"one": ..., "other": ...}; zh always selects "other".
"""

import contextvars
import json
import os
import re
import threading

SUPPORTED_LOCALES = ("en", "zh", "hi")
DEFAULT_LOCALE = "en"
COOKIE_NAME = "locale"
HTML_LANG = {"en": "en", "zh": "zh-CN", "hi": "hi"}
LOCALE_NATIVE_NAME = {"en": "English", "zh": "\u4e2d\u6587", "hi": "\u0939\u093f\u0928\u094d\u0926\u0940"}

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_DIR = os.path.join(HERE, "i18n")

_catalogs = {}
_catalog_lock = threading.Lock()
_miss_log = []          # in-memory pack-update queue (key, locale)
_miss_lock = threading.Lock()
_MISS_CAP = 5000

MARKER_RE = re.compile(r"\{\{t:([a-z0-9_.]+)\}\}")
_TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# Per-request locale for the stdlib threaded server. The request handler sets
# this at entry; t() with no explicit locale uses it (defaults to "en").
_request_locale = contextvars.ContextVar("arena_request_locale",
                                         default=DEFAULT_LOCALE)


def set_request_locale(locale):
    """Set the locale for the current request (call at request entry)."""
    _request_locale.set(locale if locale in SUPPORTED_LOCALES
                        else DEFAULT_LOCALE)


def get_request_locale():
    """Locale for the current request; 'en' outside a request."""
    loc = _request_locale.get()
    return loc if loc in SUPPORTED_LOCALES else DEFAULT_LOCALE


def _catalog_path(locale):
    return os.path.join(CATALOG_DIR, locale + ".json")


def get_catalog(locale):
    """Load (and cache) a locale catalog. Unknown locale -> default.

    A missing catalog file degrades to an empty pack (English fallback
    per key); it never raises.
    """
    if locale not in SUPPORTED_LOCALES:
        locale = DEFAULT_LOCALE
    with _catalog_lock:
        if locale not in _catalogs:
            try:
                with open(_catalog_path(locale), encoding="utf-8") as f:
                    _catalogs[locale] = json.load(f)
            except (OSError, ValueError):
                _catalogs[locale] = {"meta": {"locale": locale,
                                              "status": "missing-fallback-en"},
                                     "strings": {}}
        return _catalogs[locale]


def clear_cache():
    with _catalog_lock:
        _catalogs.clear()


def log_miss(key, locale):
    with _miss_lock:
        if len(_miss_log) < _MISS_CAP:
            _miss_log.append((key, locale))


def get_misses():
    with _miss_lock:
        return list(_miss_log)


def clear_misses():
    with _miss_lock:
        _miss_log.clear()


def plural_form(locale, n):
    """CLDR-ish minimal: zh has no plurals; en/hi use one/other."""
    if locale == "zh":
        return "other"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "other"
    return "one" if n == 1 else "other"


def _interpolate(text, variables):
    def _rep(m):
        name = m.group(1)
        return str(variables[name]) if name in variables else m.group(0)
    return _TOKEN_RE.sub(_rep, text)


def t(key, locale=None, count=None, **variables):
    """Translate key. Falls back to English (logging the miss); never blank.

    locale=None means the current request's locale (set via
    set_request_locale); 'en' outside a request.
    """
    locale = locale or get_request_locale()
    if locale not in SUPPORTED_LOCALES:
        locale = DEFAULT_LOCALE
    cat = get_catalog(locale)
    entry = cat.get("strings", {}).get(key)
    if entry is None:
        if locale != DEFAULT_LOCALE:
            log_miss(key, locale)
            return t(key, DEFAULT_LOCALE, count=count, **variables)
        return key  # unknown even in en: return the key, never blank
    if isinstance(entry, dict) and "source" in entry:
        text = entry["source"]
    elif isinstance(entry, dict):
        # plural forms {"one": .., "other": ..} (+ optional "note")
        n = count
        if n is None:
            n = variables.get("n", variables.get("count", 2))
        text = entry.get(plural_form(locale, n)) or entry.get("other")
    else:
        text = entry  # plain translated string
    if not text:
        if locale != DEFAULT_LOCALE:
            log_miss(key, locale)
            return t(key, DEFAULT_LOCALE, count=count, **variables)
        return key
    if count is not None:
        variables = dict(variables)
        variables.setdefault("n", count)
        variables.setdefault("count", count)
    if variables:
        text = _interpolate(text, variables)
    # literal doubled braces (stored as {{ }}) become single
    text = text.replace("{{", "{").replace("}}", "}")
    return text


def reverse_key(english):
    """Find the catalog key for an English source string (API-message lookup).

    Returns None when the literal is not registered. Built from en.json so the
    171 raise sites need no changes: the boundary translates via this map.
    """
    cat = get_catalog(DEFAULT_LOCALE)
    strings = cat.get("strings", {})
    # exact source match; plural entries match on either form
    for key, entry in strings.items():
        if isinstance(entry, dict) and "source" in entry:
            if entry["source"] == english:
                return key
    return None


def translate_api_message(message, locale, key=None, params=None):
    """Translate an API error/message string for the request locale."""
    if key:
        return t(key, locale, **(params or {}))
    found = reverse_key(message)
    if found:
        return t(found, locale)
    return message  # unregistered: return as-is (English), never blank


def resolve_locale(cookie_header=None, accept_language=None):
    """Locale resolution order: cookie > Accept-Language > en."""
    if cookie_header:
        m = re.search(r"(?:^|;\s*)" + COOKIE_NAME + r"=([A-Za-z-]+)",
                      cookie_header)
        if m:
            loc = m.group(1).lower().split("-")[0]
            if loc in SUPPORTED_LOCALES:
                return loc
    if accept_language:
        for part in accept_language.split(","):
            tag = part.split(";")[0].strip().lower().split("-")[0]
            if tag in SUPPORTED_LOCALES:
                return tag
    return DEFAULT_LOCALE


def _js_bootstrap(locale):
    """Per-request <script>: window.__STRINGS__ + __t() for JS-side strings.

    Merges English as the base so a missing translation degrades to English,
    never to a blank or a raw key. Plural entries stay structured so __t can
    pick one/other by count.
    """
    en_strings = get_catalog(DEFAULT_LOCALE).get("strings", {})
    loc_strings = get_catalog(locale).get("strings", {}) if locale != DEFAULT_LOCALE else {}

    def norm(entry):
        # -> "text" | {"one":..,"other":..} | None
        if isinstance(entry, dict) and "source" in entry:
            return entry["source"]
        if isinstance(entry, dict):
            out = {}
            if entry.get("one"):
                out["one"] = entry["one"]
            if entry.get("other"):
                out["other"] = entry["other"]
            return out or None
        return entry or None

    merged = {}
    for k, e in en_strings.items():
        v = norm(e)
        if v is not None:
            merged[k] = v
    for k, e in loc_strings.items():
        v = norm(e)
        if v is not None:
            base = merged.get(k)
            if isinstance(base, dict) and isinstance(v, dict):
                merged[k] = {"one": v.get("one") or base.get("one"),
                             "other": v.get("other") or base.get("other")}
            else:
                merged[k] = v
    payload = json.dumps(merged, ensure_ascii=False).replace("</", "<\\/")
    return (
        '<script>window.__LOCALE__=' + json.dumps(locale) +
        ';window.__STRINGS__=' + payload +
        ';function __t(k,v){var e=window.__STRINGS__.hasOwnProperty(k)'
        '?window.__STRINGS__[k]:null;var s;'
        'if(e==null){s=k;}else if(typeof e==="string"){s=e;}'
        'else{var n=v?(v.n!==undefined?v.n:v.count):2;'
        'if(window.__LOCALE__==="zh"){s=e.other||e.one||k;}'
        'else{s=(n===1?(e.one||e.other||k):(e.other||e.one||k));}}'
        'if(v){for(var p in v){if(v.hasOwnProperty(p)){'
        's=s.split("{"+p+"}").join(String(v[p]));}}}return s;}</script>'
    )


def render_template(html, locale):
    """Substitute {{t:key}} markers, inject the JS i18n bootstrap,
    the locale switcher (inside the shared sidebar), and set lang."""
    if locale not in SUPPORTED_LOCALES:
        locale = DEFAULT_LOCALE

    def _sub(m):
        return t(m.group(1), locale)

    out = MARKER_RE.sub(_sub, html)
    # per-locale <html lang>
    out = re.sub(r'<html(\s+lang=")[a-zA-Z-]+"',
                 r'<html\1' + HTML_LANG.get(locale, "en") + '"',
                 out, count=1)
    # locale switcher at the foot of the shared sidebar drawer
    sw = locale_switcher_html(locale)
    out = re.sub(r'(<aside class="ma-side" id="maSide"[^>]*>.*?)</aside>',
                 lambda m: m.group(1) + sw + "</aside>",
                 out, count=1, flags=re.S)
    # JS bootstrap before </head> (or at the top if no head)
    boot = _js_bootstrap(locale)
    if "</head>" in out:
        out = out.replace("</head>", boot + "\n</head>", 1)
    else:
        out = boot + "\n" + out
    return out


def locale_switcher_html(locale):
    """Footer locale switcher (labels stay in their native names, always)."""
    parts = []
    for loc in SUPPORTED_LOCALES:
        name = LOCALE_NATIVE_NAME[loc]
        if loc == locale:
            parts.append('<span class="ma-loc-cur">%s</span>' % name)
        else:
            parts.append(
                '<a href="#" class="ma-loc-link" data-locale="%s">%s</a>'
                % (loc, name))
    return ('<div class="ma-locale">%s</div>'
            '<script>(function(){document.querySelectorAll(".ma-loc-link")'
            '.forEach(function(a){a.addEventListener("click",function(e){'
            'e.preventDefault();document.cookie="locale="+a.getAttribute'
            '("data-locale")+";path=/;max-age=31536000";location.reload();});});})();'
            '</script>' % " · ".join(parts))
