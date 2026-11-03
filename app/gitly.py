"""GitHub release → Telegram: changelog + APK, automatically.

Ported from the Gitly-Leech-Bot. Every time a release is published on the watched
repo, Bonki posts the changelog to the configured channel and attaches the release
`.apk` as a document.

Default repo is SwitchToMonk/Gilty; change it with /gitly repo owner/name.

Notes that matter in practice:
  * The bot API caps uploads at 50 MB. A bigger APK is announced with a download
    link instead of being attached - it cannot be uploaded, by any bot.
  * Each release is posted exactly once; state is remembered across restarts.
  * Polling is used (no GitHub webhook needed). Every 10 minutes by default.
"""
import asyncio
import logging
import os
import re
import tempfile
import time

import requests

from . import db, telegram

log = logging.getLogger("gitly")

API = "https://api.github.com"
DEFAULT_REPO = "SwitchToMonk/Gitly"
UPLOAD_LIMIT = 50 * 1024 * 1024        # Telegram bot upload ceiling
POLL_SECONDS = 60         # 1 min; unchanged polls (304) cost no GitHub quota
MAX_SEEN = 100


# ------------------------------------------------------------------ config --
def repo():
    name = (db.get_settings().get("gitly_repo") or DEFAULT_REPO).strip()
    # tolerate the common typo and fix it in place
    if name.lower() in ("switchtomonk/gilty", "switchtomonk/gilty.git"):
        db.set_settings({"gitly_repo": DEFAULT_REPO})
        return DEFAULT_REPO
    return name.rstrip("/")


def channel():
    return (db.get_settings().get("gitly_channel") or "").strip()


def enabled():
    return str(db.get_settings().get("gitly_on") or "0") == "1"


def include_prereleases():
    """Pre-releases are posted too, by default."""
    return str(db.get_settings().get("gitly_prerelease") or "1") == "1"


def interval():
    """Seconds between checks. 60 by default; unchanged polls cost no quota."""
    try:
        v = int(db.get_settings().get("gitly_interval") or POLL_SECONDS)
    except (TypeError, ValueError):
        v = POLL_SECONDS
    return max(30, min(v, 3600))


def _seen():
    try:
        return [str(x) for x in (db.get_settings().get("gitly_seen") or "").split(",") if x]
    except Exception:
        return []


def _mark_seen(rid):
    seen = _seen()
    rid = str(rid)
    if rid in seen:
        return False
    seen.append(rid)
    db.set_settings({"gitly_seen": ",".join(seen[-MAX_SEEN:])})
    return True


# ------------------------------------------------------------------ github --
def _headers():
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "Bonki-Intelligence"}
    tok = (db.get_settings().get("github_token") or
           os.environ.get("GITHUB_TOKEN") or "").strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def fetch_releases(repo_full=None, limit=10):
    """Newest releases first. Returns None when nothing changed (HTTP 304).

    A public repo needs NO token. To keep well inside GitHub's 60 requests/hour
    for unauthenticated callers, this sends If-None-Match: a 304 "not modified"
    reply does not count against the limit at all, so steady polling is free.
    """
    repo_full = (repo_full or repo()).strip().strip("/")
    if "/" not in repo_full:
        raise RuntimeError("repo must look like owner/name")
    url = f"{API}/repos/{repo_full}/releases?per_page={limit}"
    headers = _headers()
    etag = db.get_settings().get("gitly_etag") or ""
    if etag:
        headers["If-None-Match"] = etag
    try:
        r = requests.get(url, headers=headers, timeout=20)
    except Exception as e:
        raise RuntimeError(f"could not reach GitHub ({type(e).__name__})") from e
    if r.status_code == 304:
        return None                       # unchanged - costs no quota
    if r.status_code == 404:
        raise RuntimeError(f"repo '{repo_full}' not found, or it is private")
    if r.status_code == 403:
        raise RuntimeError("GitHub rate limit reached — this usually means the "
                           "host's IP is shared. A token (/gitly token) fixes it, "
                           "but it is optional for a public repo.")
    if not r.ok:
        raise RuntimeError(f"GitHub returned HTTP {r.status_code}")
    new_etag = r.headers.get("ETag") or ""
    if new_etag:
        db.set_settings({"gitly_etag": new_etag})
    data = r.json()
    return data if isinstance(data, list) else []


def pick_apk(assets):
    """First .apk asset (the source bot's rule)."""
    apks = [a for a in (assets or [])
            if str(a.get("name", "")).lower().endswith(".apk")]
    return apks[0] if apks else None


def download_asset(asset, dest):
    url = asset.get("browser_download_url") or asset.get("url")
    r = requests.get(url, headers=_headers(), timeout=300, stream=True)
    if not r.ok:
        raise RuntimeError(f"download failed: HTTP {r.status_code}")
    with open(dest, "wb") as fh:
        for chunk in r.iter_content(1 << 16):
            if chunk:
                fh.write(chunk)
    return dest


# ------------------------------------------------------------------ posting --
def _header(rel):
    tag = rel.get("tag_name") or "?"
    title = rel.get("name") or tag
    out = f"🚀 <b>New release</b>: {telegram.esc(title)}\nTag: <code>{telegram.esc(tag)}</code>"
    if rel.get("html_url"):
        out += f"\n{rel['html_url']}"
    return out


def post_release(rel, target):
    """Post changelog + APK for one release. Returns a short status string."""
    if rel.get("draft"):
        return "draft"
    if rel.get("prerelease") and not include_prereleases():
        return "prerelease"
    rid = rel.get("id") or rel.get("tag_name")
    if not _mark_seen(rid):
        return "already"
    db.flush(force=False)

    header = _header(rel)
    body = (rel.get("body") or "").strip()
    try:
        telegram.send_message(f"{header}\n\n{body}" if body else header, chat_id=target)
    except Exception as e:
        # surface it: "chat not found" / "not enough rights" is the usual cause
        db.set_settings({"gitly_last_error": f"posting to {target}: {str(e)[:160]}"})
        log.warning("gitly changelog failed: %s", e)
        return "post-failed"

    apk = pick_apk(rel.get("assets"))
    if not apk:
        return "no-apk"

    size = int(apk.get("size") or 0)
    name = apk.get("name") or "release.apk"
    if size > UPLOAD_LIMIT:
        mb = size / (1024 * 1024)
        try:
            telegram.send_message(
                f"{header}\n\n⚠️ <b>{telegram.esc(name)}</b> is {mb:.1f} MB, above "
                f"Telegram's 50 MB bot upload limit, so I can't attach it.\n"
                f"Download: {apk.get('browser_download_url', '')}",
                chat_id=target)
        except Exception as e:
            log.warning("gitly oversize notice failed: %s", e)
        return "too-big"

    caption = f"{rel.get('name') or rel.get('tag_name')}"
    if body:
        caption += f"\n\n{body}"
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, name)
        try:
            download_asset(apk, dest)
            telegram.send_document(dest, name, caption, target)
            return "sent"
        except Exception as e:
            log.warning("gitly apk failed: %s", e)
            try:
                telegram.send_message(
                    f"{header}\n\n⚠️ Couldn't attach <b>{telegram.esc(name)}</b>: "
                    f"{telegram.esc(str(e)[:120])}\n"
                    f"Download: {apk.get('browser_download_url', '')}",
                    chat_id=target)
            except Exception:
                pass
            return "apk-failed"


async def check(force=False, target=None):
    if force:
        db.set_settings({"gitly_etag": ""})      # manual check: ignore the cache
    """Look for new releases and post them. Returns (posted, status)."""
    if not enabled() and not force:
        return 0, "off"
    tgt = (target or channel()).strip()
    if not tgt:
        return 0, "no-channel"
    try:
        backoff = int(db.get_settings().get("gitly_backoff_until") or 0)
    except (TypeError, ValueError):
        backoff = 0
    if backoff > time.time() and not force:
        return 0, "backoff"

    try:
        rels = await asyncio.to_thread(fetch_releases)
    except Exception as e:
        msg = str(e)
        db.set_settings({"gitly_last_error": msg[:200]})
        # If GitHub pushes back, stop hammering it for a while. Polling every
        # minute must never turn into a rate-limit ban.
        if "rate limit" in msg.lower():
            wait = 15 * 60
            db.set_settings({"gitly_backoff_until": str(int(time.time()) + wait)})
            log.warning("gitly backing off for %d minutes", wait // 60)
        return 0, msg
    if rels is None:
        db.set_settings({"gitly_last_check": str(int(time.time())),
                         "gitly_last_error": ""})
        return 0, "no-change"

    # First ever run: don't dump every old release into the channel. Mark the
    # history as seen and announce only the newest one.
    if not _seen() and rels:
        newest = max(rels, key=lambda r: r.get("id") or 0)
        for rel in rels:
            if rel is not newest:
                _mark_seen(rel.get("id") or rel.get("tag_name"))
        log.info("gitly first run: marked %d older release(s) as seen",
                 max(0, len(rels) - 1))

    posted = 0
    for rel in reversed(rels):                 # oldest first, so order is natural
        try:
            status = await asyncio.to_thread(post_release, rel, tgt)
            if status in ("sent", "too-big", "no-apk"):
                posted += 1
        except Exception as e:
            log.warning("gitly release failed: %s", e)
    db.set_settings({"gitly_last_check": str(int(time.time())),
                     "gitly_last_error": ""})
    return posted, "ok"


async def loop():
    log.info("gitly release watcher started (repo=%s)", repo())
    await asyncio.sleep(45)
    while True:
        try:
            if enabled() and channel():
                n, status = await check()
                if n:
                    log.info("gitly posted %d release(s)", n)
                elif status not in ("ok", "off", "no-channel"):
                    log.info("gitly check: %s", status)
                await asyncio.to_thread(db.flush)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("gitly loop error: %s", e)
        await asyncio.sleep(interval())


# ------------------------------------------------------------------ command --
async def cmd_gitly(chat, msg, user, args):
    """Configure the GitHub release → Telegram watcher."""
    from . import approval, rose
    parts = (args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("token", "gh"):
        if not approval.is_owner((user or {}).get("id")):
            return await rose._reply(chat, msg, "🔒 Owner only — this stores a token.")
        if not rest:
            return await rose._reply(chat, msg,
                                     "<b>Optional.</b> A public repo needs no token.\n"
                                     "Set one only if: the host's IP is shared and hits "
                                     "GitHub's 60/hour limit, or the repo is private.\n"
                                     "<code>/gitly token ghp_xxx</code>")
        db.set_settings({"github_token": rest.strip()})
        await asyncio.to_thread(db.flush, True)
        try:
            from . import envfile
            envfile.set_env("GITHUB_TOKEN", rest.strip())
        except Exception:
            pass
        return await rose._reply(chat, msg,
                                 "✅ GitHub token saved — 5000 requests/hour and "
                                 "private repos now readable.")

    if sub in ("interval", "every"):
        if not approval.is_owner((user or {}).get("id")) and \
                not await rose._is_admin(chat.get("id"), (user or {}).get("id")):
            return await rose._reply(chat, msg, "Admins only.")
        try:
            secs = int(re.sub(r"[^0-9]", "", rest) or 60)
        except ValueError:
            secs = 60
        secs = max(30, min(secs, 3600))
        db.set_settings({"gitly_interval": str(secs)})
        await asyncio.to_thread(db.flush, True)
        return await rose._reply(chat, msg,
                                 f"⏱ Checking every <b>{secs}s</b>.\n"
                                 f"<i>Unchanged checks return 304 and cost no GitHub "
                                 f"quota, so this stays free.</i>")

    if sub in ("repo", "channel", "on", "off", "prerelease"):
        if not approval.is_owner((user or {}).get("id")) and \
                not await rose._is_admin(chat.get("id"), (user or {}).get("id")):
            return await rose._reply(chat, msg, "Admins only.")
        if sub == "repo":
            if "/" not in rest:
                return await rose._reply(chat, msg,
                                         "Usage: <code>/gitly repo owner/name</code>")
            db.set_settings({"gitly_repo": rest.strip().strip("/")})
            await asyncio.to_thread(db.flush, True)
            return await rose._reply(chat, msg, f"📦 Watching <code>{telegram.esc(rest)}</code>.")
        if sub == "channel":
            if not rest:
                return await rose._reply(chat, msg,
                                         "Usage: <code>/gitly channel @mychannel</code> "
                                         "or a numeric id")
            db.set_settings({"gitly_channel": rest})
            await asyncio.to_thread(db.flush, True)
            return await rose._reply(chat, msg,
                                     f"📢 Releases will be posted to "
                                     f"<code>{telegram.esc(rest)}</code>.")
        if sub in ("on", "off"):
            db.set_settings({"gitly_on": "1" if sub == "on" else "0"})
            await asyncio.to_thread(db.flush, True)
            if sub == "off":
                return await rose._reply(chat, msg, "⏹ Release watcher off.")
            if not channel():
                return await rose._reply(
                    chat, msg,
                    "⚠️ Turned on, but no channel is set yet.\n"
                    "Run <code>/gitly channel @yourchannel</code> — then I'll check "
                    "every 3 minutes.")
            await rose._reply(chat, msg, "✅ On — checking GitHub right now…")
            n, status = await check(force=True)
            if status == "ok":
                return await rose._reply(
                    chat, msg,
                    f"📦 Posted {n} release(s) to <code>{telegram.esc(channel())}</code>."
                    if n else "📦 No new releases right now. I'll check every 3 minutes.")
            return await rose._reply(chat, msg, f"⚠️ {telegram.esc(status)}")
        if sub == "prerelease":
            on = rest.lower() in ("on", "1", "yes", "true")
            db.set_settings({"gitly_prerelease": "1" if on else "0"})
            return await rose._reply(chat, msg,
                                     f"Pre-releases: {'included' if on else 'ignored'}.")

    if sub in ("check", "now", "sync"):
        await rose._reply(chat, msg, "🔎 Checking GitHub…")
        n, status = await check(force=True, target=rest or None)
        if status not in ("ok",):
            return await rose._reply(chat, msg, f"⚠️ {telegram.esc(status)}")
        return await rose._reply(chat, msg,
                                 f"✅ Done — posted {n} release(s)."
                                 if n else "No new releases.")

    # status
    last = db.get_settings().get("gitly_last_check") or ""
    try:
        last_s = (time.strftime("%d %b %H:%M", time.localtime(int(last)))
                  if last else "never")
    except Exception:
        last_s = "never"
    err = db.get_settings().get("gitly_last_error") or ""
    lines = [
        "📦 <b>GitHub release watcher</b>", "",
        f"• repo: <code>{telegram.esc(repo())}</code>",
        f"• channel: {('<code>' + telegram.esc(channel()) + '</code>') if channel() else '❌ not set'}",
        f"• status: {'✅ on' if enabled() else '⏹ off'}",
        f"• pre-releases: {'included' if include_prereleases() else 'ignored'}",
        f"• last check: {last_s} (every {interval()}s)",
    ]
    if err:
        lines.append(f"• ⚠️ last error: {telegram.esc(err[:120])}")
    lines += ["",
              "<code>/gitly channel @mychannel</code> — where to post",
              "<code>/gitly repo owner/name</code> — what to watch",
              "<code>/gitly on</code> · <code>/gitly check</code> · "
              "<code>/gitly interval 60</code>",
              "<i>A public repo needs no token — one is only useful if the host's "
              "IP is shared, or the repo is private.</i>",
              "<i>Posts the changelog and attaches the .apk. Files over 50 MB "
              "are announced with a link — Telegram's bot limit.</i>"]
    await rose._reply(chat, msg, "\n".join(lines))


# ------------------------------------------------- release menu (DM) --------
_CACHE = {"at": 0, "rels": []}
CACHE_TTL = 300
PAGE = 6


def cached_releases(force=False):
    """Releases for the menu, cached briefly so paging does not re-hit GitHub."""
    now = time.time()
    if force or not _CACHE["rels"] or (now - _CACHE["at"]) > CACHE_TTL:
        rels = fetch_releases(limit=30) or []
        _CACHE["rels"] = [r for r in rels if not r.get("draft")]
        _CACHE["at"] = now
    return _CACHE["rels"]


def menu(page=0):
    """(text, keyboard) listing every release with its APK as a button."""
    rels = cached_releases()
    if not rels:
        return (f"📦 No releases found on <code>{telegram.esc(repo())}</code>.\n\n"
                f"• If that repo name is wrong: <code>/gitly repo owner/name</code>\n"
                f"• If it has no published releases yet, publish one and try again.",
                None)
    pages = max(1, (len(rels) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for r in rels[page * PAGE:(page + 1) * PAGE]:
        apk = pick_apk(r.get("assets"))
        name = r.get("name") or r.get("tag_name") or "release"
        tag = r.get("tag_name") or ""
        if apk:
            mb = int(apk.get("size") or 0) / (1024 * 1024)
            label = f"{name} ({tag}) · {mb:.1f} MB"
            rows.append([{"text": label[:52], "callback_data": f"gh:{r.get('id')}"}])
            rows.append([{"text": "📥 send to me",
                          "callback_data": f"gh:{r.get('id')}"},
                         {"text": "📢 to channel",
                          "callback_data": f"ghc:{r.get('id')}"}])
        else:
            rows.append([{"text": f"{name} ({tag}) · no APK"[:58],
                          "callback_data": f"gh:{r.get('id')}"}])
    nav = []
    if page > 0:
        nav.append({"text": "⬅️ Prev", "callback_data": f"ghp:{page - 1}"})
    nav.append({"text": f"{page + 1}/{pages}", "callback_data": "ghp:noop"})
    if page < pages - 1:
        nav.append({"text": "Next ➡️", "callback_data": f"ghp:{page + 1}"})
    if len(nav) > 1:
        rows.append(nav)
    head = (f"📦 <b>{telegram.esc(repo())}</b> — {len(rels)} release(s)\n"
            f"<i>Tap one and I'll send its APK here.</i>")
    return head, {"inline_keyboard": rows}


def _find_release(rid):
    for r in cached_releases():
        if str(r.get("id")) == str(rid):
            return r
    return None


async def send_release(cq, rid, to_channel=False):
    """Download one release's APK and send it to the DM (or the channel)."""
    m = cq.get("message") or {}
    dm = (m.get("chat") or {}).get("id")
    if to_channel:
        dm = channel()
        if not dm:
            await asyncio.to_thread(
                telegram.answer_callback_query, cq.get("id"),
                "No channel set — use /gitly channel @yourchannel", True)
            return
    rel = _find_release(rid)
    if not rel:
        await asyncio.to_thread(telegram.answer_callback_query, cq.get("id"),
                                "Release not found — try /releases again.", True)
        return
    apk = pick_apk(rel.get("assets"))
    if not apk:
        await asyncio.to_thread(telegram.answer_callback_query, cq.get("id"),
                                "That release has no .apk attached.", True)
        return
    size = int(apk.get("size") or 0)
    if size > UPLOAD_LIMIT:
        mb = size / (1024 * 1024)
        await asyncio.to_thread(
            telegram.send_message,
            f"⚠️ <b>{telegram.esc(apk.get('name'))}</b> is {mb:.1f} MB — over "
            f"Telegram's 50 MB bot limit, so I can't send it.\n"
            f"Download: {apk.get('browser_download_url', '')}", chat_id=dm)
        await asyncio.to_thread(telegram.answer_callback_query, cq.get("id"), "Too big")
        return

    await asyncio.to_thread(telegram.answer_callback_query, cq.get("id"),
                            "Downloading…")
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, apk.get("name") or "release.apk")
        try:
            await asyncio.to_thread(download_asset, apk, dest)
            caption = (f"{rel.get('name') or rel.get('tag_name')}\n"
                       f"{rel.get('html_url') or ''}")
            await asyncio.to_thread(telegram.send_document, dest,
                                    apk.get("name") or "release.apk", caption, dm)
        except Exception as e:
            log.warning("release send failed: %s", e)
            try:
                await asyncio.to_thread(
                    telegram.send_message,
                    f"⚠️ Couldn't fetch that APK: {telegram.esc(str(e)[:120])}",
                    chat_id=dm)
            except Exception:
                pass


async def cmd_releases(chat, msg, user, args):
    """Menu of every GitHub release, tap to pull its APK."""
    from . import rose
    try:
        text, kb = await asyncio.to_thread(menu, 0)
    except Exception as e:
        return await rose._reply(chat, msg, f"⚠️ {telegram.esc(str(e))}")
    await asyncio.to_thread(telegram.send_message, text, chat_id=chat.get("id"),
                            reply_to=msg.get("message_id"), reply_markup=kb)
