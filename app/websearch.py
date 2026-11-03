"""Web search for /web - no API key.

Uses DuckDuckGo's HTML endpoint, which needs no key and no account. It is a
best-effort feature: many hosts (and some networks) block it, so every failure is
reported plainly instead of pretending nothing happened.
"""
import html
import logging
import re
import urllib.parse

import requests

log = logging.getLogger("websearch")

_UA = {"User-Agent": "Mozilla/5.0 (compatible; BonkiBot/1.0; +https://t.me)"}
_URL_RE = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s):
    return html.unescape(_TAG_RE.sub("", s or "")).strip()


def _unwrap(href):
    """DuckDuckGo wraps links as /l/?uddg=<encoded>. Unwrap to the real url."""
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        try:
            q = urllib.parse.urlparse(href).query
            real = urllib.parse.parse_qs(q).get("uddg", [""])[0]
            if real:
                return real
        except Exception:
            pass
    return href


def _parse_html(text, limit):
    out, seen = [], set()
    for href, title in _URL_RE.findall(text):
        url = _unwrap(href)
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        out.append((_clean(title) or url, url))
        if len(out) >= limit:
            break
    return out


_LITE_RE = re.compile(r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def search(query, limit=5, timeout=15):
    """[(title, url)] - raises RuntimeError with a readable reason on failure.

    Tries the HTML endpoint, then the lite one. Both are keyless; either can be
    rate-limited (HTTP 202) or blocked by a host's egress rules.
    """
    q = (query or "").strip()
    if not q:
        return []

    attempts = [
        ("https://html.duckduckgo.com/html/", {"q": q}, _parse_html),
        ("https://lite.duckduckgo.com/lite/", {"q": q},
         lambda t, n: _parse_lite(t, n)),
    ]
    problems = []
    for url, data, parser in attempts:
        try:
            r = requests.post(url, data=data, headers=_UA, timeout=timeout)
        except Exception as e:
            problems.append(f"{type(e).__name__}")
            continue
        if r.status_code in (202, 403, 429):
            problems.append(f"blocked ({r.status_code})")
            continue
        if r.status_code != 200:
            problems.append(f"HTTP {r.status_code}")
            continue
        rows = parser(r.text, limit)
        if rows:
            return rows
        problems.append("no results parsed")

    raise RuntimeError(
        "; ".join(problems) + " — the search endpoint refused this host. "
        "That is a network restriction, not a bot bug.")


def _parse_lite(text, limit):
    out, seen = [], set()
    for href, title in _LITE_RE.findall(text):
        url = _unwrap(href)
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        out.append((_clean(title) or url, url))
        if len(out) >= limit:
            break
    return out
