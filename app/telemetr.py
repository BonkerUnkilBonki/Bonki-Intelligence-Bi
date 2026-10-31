"""Telemetr.io channel statistics.

What @telemetr_io_bot actually does (only four things):
  * /channelstat  - an image with detailed channel statistics
  * authorise you on the Telemetr.io website (Telegram login)
  * /help
  * /lang

This module implements the part a bot *can* implement: the statistics image, via
Telemetr.io's **public REST API** (https://api.tlmtr.io, `x-api-key` header).

Honest limits, stated up front:
  * You need your own API key from @telemetrio_api_bot. Without one this is off.
  * The free plan allows 1000 requests/month, 5 channels, 7 days of history, and
    verified channels only.
  * We cannot perform Telemetr.io's website login for you - that is their own
    server-side flow through @telemetr_io_bot. What we can do is use your API key,
    which is the supported way for a third-party app to act for you.
  * Field names in responses are read defensively (several fallbacks each), since
    the API evolves; anything missing is simply left off the card.
"""
import logging
import os

import requests

from . import db

log = logging.getLogger("telemetr")

BASE = os.environ.get("TELEMETRIO_API_BASE_URL", "https://api.tlmtr.io").rstrip("/")


def api_key():
    return (db.get_settings().get("telemetr_api_key")
            or os.environ.get("TELEMETRIO_API_KEY") or "").strip()


def enabled():
    return bool(api_key())


def _get(path, **params):
    """GET a Telemetr.io endpoint. Raises RuntimeError with a readable message."""
    k = api_key()
    if not k:
        raise RuntimeError("no Telemetr.io API key set")
    r = requests.get(f"{BASE}{path}", params=params, timeout=20,
                     headers={"x-api-key": k, "accept": "application/json"})
    if r.status_code == 401:
        raise RuntimeError("API key rejected (401) — check the key from @telemetrio_api_bot")
    if r.status_code == 402:
        raise RuntimeError("out of Telemetr.io credits (402)")
    if r.status_code == 429:
        raise RuntimeError("Telemetr.io rate limit hit (429) — try again shortly")
    if not r.ok:
        try:
            msg = (r.json() or {}).get("message")
        except Exception:
            msg = r.text[:150]
        raise RuntimeError(f"Telemetr.io error {r.status_code}: {msg}")
    return r.json()


def _pick(d, *names, default=None):
    """First present value among several possible field names."""
    if not isinstance(d, dict):
        return default
    for n in names:
        v = d.get(n)
        if v not in (None, "", [], {}):
            return v
    return default


def usage():
    return _get("/v1/usage/info")


def search(term, limit=5):
    data = _get("/v1/channels/search", term=term, limit=limit)
    if isinstance(data, dict):
        data = _pick(data, "channels", "items", "results", "data", default=[]) or []
    return data if isinstance(data, list) else []


def resolve(term):
    """Turn @name / t.me/name / name into a channel record with internal_id."""
    t = (term or "").strip()
    for pre in ("https://", "http://"):
        if t.startswith(pre):
            t = t[len(pre):]
    if t.startswith("t.me/"):
        t = t[5:]
    t = t.lstrip("@").rstrip("/")
    if not t:
        raise RuntimeError("give me a channel: /channelstat @name")
    hits = search(t, limit=5)
    if not hits:
        raise RuntimeError(f"no channel found for “{t}” (free plan covers verified "
                           f"channels only)")
    low = t.lower()
    for h in hits:
        uname = str(_pick(h, "username", "link", "name", default="")).lower().lstrip("@")
        if uname == low:
            return h
    return hits[0]


def _series(rows):
    """Pull a numeric series out of a charts response, whatever it is called."""
    if isinstance(rows, dict):
        rows = _pick(rows, "data", "items", "chart", "dynamics", "result", default=[])
    out = []
    for row in (rows or []):
        if isinstance(row, (int, float)):
            out.append(float(row))
            continue
        if not isinstance(row, dict):
            continue
        v = _pick(row, "value", "subscribers", "count", "reach", "err", "views",
                  "subscribers_count")
        try:
            if v is not None:
                out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out[-30:]


def channel_data(term):
    """Everything needed for the stats card: info, stats and a growth series."""
    rec = resolve(term)
    iid = _pick(rec, "internal_id", "id", "channel_id")
    info, stats, series = rec, {}, []
    if iid is not None:
        try:
            info = {**rec, **(_get("/v1/channel/info", internal_id=iid) or {})}
        except Exception as e:
            log.info("channel/info failed: %s", e)
        try:
            stats = _get("/v1/channel/stats", internal_id=iid) or {}
        except Exception as e:
            log.info("channel/stats failed: %s", e)
        try:
            series = _series(_get("/v1/charts/subscribers", internal_id=iid))
        except Exception as e:
            log.info("charts/subscribers failed: %s", e)
    return {"info": info, "stats": stats, "series": series}


def summarise(term):
    """Flat dict of the numbers we want on the card."""
    d = channel_data(term)
    info, stats = d["info"], d["stats"]
    merged = {**stats, **info} if isinstance(stats, dict) else info

    name = _pick(info, "title", "name", "username", default=str(term))
    uname = _pick(info, "username", "link", default="")
    uname = str(uname).lstrip("@") if uname else ""
    subs = _pick(merged, "subscribers", "participants_count", "members",
                 "subscribers_count", "members_count")
    views = _pick(merged, "avg_views", "views_avg", "average_views", "views")
    er = _pick(merged, "er", "err", "engagement_rate", "er_post")
    growth = _pick(merged, "growth", "growth_7d", "subscribers_growth", "delta")
    if growth is None and d["series"] and len(d["series"]) > 1:
        growth = d["series"][-1] - d["series"][0]
    return {
        "name": name,
        "username": uname,
        "subscribers": subs,
        "views": views,
        "er": er,
        "growth": growth,
        "series": d["series"],
    }
