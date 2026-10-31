"""Skills - scheduled and recurring tasks.

This is Mira's most-used feature ("Every Monday at 9am, summarise my issues")
implemented for what Bonki can actually do: summarise, report stats, list recent
files, or send a plain reminder.

Honest scope: Bonki has no OAuth integrations, so a skill cannot "pull my GitHub
issues". It works over this chat's own indexed data plus reminders. Anything that
needs a third-party account is out of reach, and the bot says so rather than
pretending.

Schedule syntax (deliberately small and predictable):
    /skill every day 09:00 summarise this group
    /skill every mon 09:00 send the weekly digest
    /skill in 2h remind me to check the upload
    /skill at 18:30 stats
"""
import asyncio
import logging
import re
import time

from . import db, telegram

log = logging.getLogger("skills")

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
        "saturday": 5, "sunday": 6}

_TICK = 30          # seconds between checks


def _hm(text):
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", text or "")
    if not m:
        m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", text or "", re.I)
        if not m:
            return None
        h = int(m.group(1)) % 12
        if m.group(2).lower() == "pm":
            h += 12
        return h, 0
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h, mi


_TIME_PREFIX = re.compile(r"^\s*(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b\s*", re.I)


def _strip_time(rest):
    """Remove a leading time token so the task text is just the task."""
    return _TIME_PREFIX.sub("", (rest or "").strip(), count=1).strip()


def parse_when(text):
    """(first_run_ts, repeat_seconds|None, task_text) or (None, None, None)."""
    t = (text or "").strip()
    now = time.time()

    m = re.match(r"in\s+(\d+)\s*(m|min|mins|minute|minutes|h|hr|hour|hours)\b\s*(.*)$",
                 t, re.I)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        secs = n * 60 if unit.startswith("m") else n * 3600
        return now + secs, None, m.group(3).strip()

    m = re.match(r"every\s+day\s+(.*)$", t, re.I)
    if m:
        hm = _hm(m.group(1))
        if not hm:
            return None, None, None
        h, mi = hm
        lt = time.localtime(now)
        first = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
        if first <= now:
            first += 86400
        return first, 86400, _strip_time(m.group(1))

    m = re.match(r"every\s+([a-z]+)\s+(.*)$", t, re.I)
    if m and m.group(1).lower() in DAYS:
        hm = _hm(m.group(2))
        if not hm:
            return None, None, None
        h, mi = hm
        want = DAYS[m.group(1).lower()]
        lt = time.localtime(now)
        first = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
        while time.localtime(first).tm_wday != want or first <= now:
            first += 86400
        return first, 7 * 86400, _strip_time(m.group(2))

    m = re.match(r"at\s+(.*)$", t, re.I)
    if m:
        hm = _hm(m.group(1))
        if not hm:
            return None, None, None
        h, mi = hm
        lt = time.localtime(now)
        first = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
        if first <= now:
            first += 86400
        return first, None, _strip_time(m.group(1))

    return None, None, None


def kind_of(task):
    low = (task or "").lower()
    if "summar" in low:
        return "summarise"
    if "digest" in low or "recent files" in low:
        return "digest"
    if low.startswith("stat") or " stats" in low:
        return "stats"
    if low.startswith("find ") or low.startswith("search "):
        return "find"
    return "remind"


def add(chat_id, when_text, by=None):
    first, repeat, task = parse_when(when_text)
    if not first:
        return None, ("Couldn't read the schedule. Try:\n"
                      "<code>/skill every day 09:00 summarise this group</code>\n"
                      "<code>/skill every mon 09:00 send the weekly digest</code>\n"
                      "<code>/skill in 2h remind me to check the upload</code>")
    if not task:
        return None, "Add what I should do after the schedule."
    row = db.add_skill(chat_id=chat_id, task=task, kind=kind_of(task),
                       next_ts=int(first), repeat=int(repeat or 0), by=str(by or ""))
    return row, None


def describe(row):
    rep = "once"
    if row.get("repeat"):
        rep = "daily" if row["repeat"] == 86400 else (
            "weekly" if row["repeat"] == 7 * 86400 else f"every {row['repeat']}s")
    when = time.strftime("%d %b %H:%M", time.localtime(row.get("next_ts") or 0))
    return (f"<code>#{row['id']}</code> <b>{rep}</b> · next {when}\n"
            f"   → {telegram.esc((row.get('task') or '')[:80])}")


async def _run(row):
    """Execute one skill and deliver the result to its chat."""
    chat_id = row.get("chat_id")
    task = row.get("task") or ""
    kind = row.get("kind") or "remind"
    chat = {"id": chat_id, "type": "supergroup", "title": ""}
    msg = {"message_id": None, "chat": chat, "from": {"id": 0}}

    try:
        if kind == "remind":
            await asyncio.to_thread(telegram.send_message,
                                    f"⏰ <b>Reminder</b>\n{telegram.esc(task)}",
                                    chat_id=chat_id)
            return
        from . import agent
        if kind == "summarise":
            await agent._summarise(chat, msg)
        elif kind == "stats":
            await agent._stats(chat, msg, db.list_files(chat_id=chat_id))
        elif kind == "digest":
            await agent._list_like("list", chat, msg, "")
        elif kind == "find":
            query = task[5:].strip() if task.lower().startswith("find ") else task
            best, _ = await agent.find_best(query, chat_id)
            if best:
                await asyncio.to_thread(
                    telegram.send_message,
                    f"🔎 Scheduled find: <b>{telegram.esc(best.get('name'))}</b>",
                    chat_id=chat_id)
            else:
                await asyncio.to_thread(
                    telegram.send_message,
                    f"🔎 Scheduled find found nothing for “{telegram.esc(query)}”.",
                    chat_id=chat_id)
        else:
            await asyncio.to_thread(telegram.send_message,
                                    f"⏰ {telegram.esc(task)}", chat_id=chat_id)
    except Exception as e:
        log.warning("skill #%s failed: %s", row.get("id"), e)


async def loop():
    """Background: run due skills. Started once from the app lifespan."""
    log.info("skills loop started")
    while True:
        try:
            now = int(time.time())
            for row in db.due_skills(now):
                await _run(row)
                if row.get("repeat"):
                    db.update_skill(row["id"], {"next_ts": now + int(row["repeat"])})
                else:
                    db.delete_skill(row["id"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("skills loop error: %s", e)
        await asyncio.sleep(_TICK)
