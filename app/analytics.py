"""Native channel analytics - a Telemetr.io-style engine, computed by Bonki.

No third-party API, no key: every number here comes from what Bonki has actually
indexed plus Telegram's own `getChatMemberCount`. That makes it honest by
construction, and it also makes the limits honest:

  * **members / subscribers** - real, from Telegram, sampled on a schedule so
    growth over time can be charted
  * **files, size, types, top senders, activity per day** - real, from the index
  * **views and ER are NOT possible.** The Bot API never exposes post view
    counts, so any "views" figure a bot shows you without a third-party service
    is invented. This engine does not invent them.
  * **audience demographics** (language, premium share) need a user account, and
    are not shown here.
"""
import asyncio
import logging
import time

from . import db, telegram

log = logging.getLogger("analytics")

SNAPSHOT_EVERY = 6 * 3600          # sample member counts every 6 hours


def _members(chat_id):
    try:
        r = telegram.get_chat_member_count(chat_id)
        v = r.get("result")
        return int(v) if isinstance(v, (int, float)) else None
    except Exception as e:
        log.info("member count failed for %s: %s", chat_id, e)
        return None


def take_snapshot(chat_id):
    """Record member count + index size now, so growth can be charted later."""
    members = _members(chat_id)
    files = db.list_files(chat_id=chat_id)
    db.add_snapshot(chat_id, members=members, files=len(files),
                    size=sum(f.get("size") or 0 for f in files))


def _growth(chat_id, days=7):
    # small tolerance: a snapshot taken exactly `days` ago must still count
    since = time.time() - (days * 86400 + 7200)
    rows = [r for r in db.list_snapshots(chat_id, since=since) if r.get("members")]
    if len(rows) < 2:
        return None
    return int(rows[-1]["members"]) - int(rows[0]["members"])


def _daily(files, days=14):
    """Files indexed per day for the last `days`, oldest first."""
    now = time.time()
    buckets = [0] * days
    for f in files:
        d = f.get("date") or 0
        if not d:
            continue
        age = int((now - d) // 86400)
        if 0 <= age < days:
            buckets[days - 1 - age] += 1
    return buckets


def compute(chat_id):
    """Everything the stats card needs, all from our own data."""
    files = db.list_files(chat_id=chat_id)
    by_ext, by_sender, total = {}, {}, 0
    for f in files:
        e = f.get("ext") or "other"
        by_ext[e] = by_ext.get(e, 0) + 1
        s = f.get("sender_name") or "unknown"
        by_sender[s] = by_sender.get(s, 0) + 1
        total += f.get("size") or 0

    members = _members(chat_id)
    if members is not None:
        db.add_snapshot(chat_id, members=members, files=len(files), size=total)

    dates = [f.get("date") or 0 for f in files if f.get("date")]
    return {
        "files": len(files),
        "size": total,
        "types": sorted(by_ext.items(), key=lambda kv: -kv[1])[:6],
        "senders": sorted(by_sender.items(), key=lambda kv: -kv[1])[:5],
        "members": members,
        "growth": _growth(chat_id),
        "daily": _daily(files),
        "last_activity": max(dates) if dates else 0,
        "first_activity": min(dates) if dates else 0,
        "snapshots": len(db.list_snapshots(chat_id)),
    }


def catalog(query=None, limit=25):
    """All chats the bot is in, with live-ish metrics, best first."""
    q = (query or "").strip().lower().lstrip("@")
    out = []
    for g in db.list_groups():
        title = (g.get("title") or "")[:60]
        handle = (g.get("handle") or "").lstrip("@")
        if q and q not in title.lower() and q not in handle.lower():
            continue
        files = db.list_files(chat_id=g.get("chat_id"))
        snaps = db.list_snapshots(g.get("chat_id"))
        members = next((s["members"] for s in reversed(snaps) if s.get("members")), None)
        dates = [f.get("date") or 0 for f in files if f.get("date")]
        out.append({
            "title": title or str(g.get("chat_id")),
            "handle": handle,
            "type": g.get("chat_type") or "",
            "files": len(files),
            "size": sum(f.get("size") or 0 for f in files),
            "members": members,
            "approved": bool(g.get("approved")),
            "last": max(dates) if dates else 0,
        })
    out.sort(key=lambda r: (-r["files"], -(r["last"] or 0)))
    return out[:limit]


async def snapshot_loop():
    """Sample member counts on a schedule so growth is chartable."""
    log.info("analytics loop started")
    while True:
        try:
            for g in db.list_groups():
                await asyncio.to_thread(take_snapshot, g.get("chat_id"))
            await asyncio.to_thread(db.flush)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("snapshot loop error: %s", e)
        await asyncio.sleep(SNAPSHOT_EVERY)
