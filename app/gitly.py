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
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import re
import tempfile
import time

import requests

from . import db, telegram

log = logging.getLogger("gitly")
BUILD = "leech-2026-11-05m"   # bump when the leech code changes
INSTANCE = os.urandom(3).hex()   # identifies THIS process, to spot duplicates

API = "https://api.github.com"
DEFAULT_REPO = "SwitchToMonk/Gitly"
UPLOAD_LIMIT = 50 * 1024 * 1024        # Telegram bot upload ceiling
POLL_SECONDS = 60         # 1 min; unchanged polls (304) cost no GitHub quota
MAX_SEEN = 100


# ------------------------------------------------------------------ config --
def normalize_repo(text):
    """Turn anything the user pastes into 'owner/name'.

    People paste a clone URL, a browser URL, or a name with '.git' on the end -
    "https://github.com/SwitchToMonk/Gitly.git" is the normal thing to send, and
    using it verbatim made GitHub answer 404.
    """
    t = (text or "").strip().strip("<>\"'` ")
    if not t:
        return ""
    t = re.sub(r"^[a-z]+://", "", t, flags=re.I)          # https://
    # ssh clone form: git@github.com:owner/repo.git
    t = re.sub(r"^[\w.-]+@[\w.-]+:", "", t)               # git@github.com:
    t = re.sub(r"^(www\.)?github\.com/", "", t, flags=re.I)
    t = t.split("?")[0].split("#")[0].rstrip("/")
    if t.lower().endswith(".git"):
        t = t[:-4]
    parts = [x for x in t.split("/") if x]
    if len(parts) >= 2:
        t = f"{parts[0]}/{parts[1]}"
    return t.strip("/")


def suggest_repos(owner, want, limit=4):
    """Closest repo names for an owner - only used when a name 404s."""
    try:
        data = fetch_json(f"/users/{owner}/repos?per_page=100&sort=updated")
    except Exception:
        return []
    if not data:
        return []
    names = [d.get("name") for d in data if d.get("name")]
    from difflib import get_close_matches
    return get_close_matches(want, names, n=limit, cutoff=0.5)


def repo():
    raw = (db.get_settings().get("gitly_repo") or DEFAULT_REPO)
    name = normalize_repo(raw)
    if name and name != str(raw).strip():
        db.set_settings({"gitly_repo": name})     # repair what was stored
    return name or DEFAULT_REPO


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
    """Public releases. Reads the public web pages first - no API, no rate limit.

    Only if that fails AND a token is configured does it fall back to the API.
    """
    name = normalize_repo(repo_full or repo())
    try:
        return fetch_releases_web(name, limit)
    except Exception as web_err:
        log.info("web scrape failed for %s (%s) - trying the API", name, web_err)
        if not _headers().get("Authorization"):
            raise RuntimeError(str(web_err)) from web_err
    return fetch_releases_api(repo_full, limit)


def fetch_releases_api(repo_full=None, limit=10):
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


def download_asset(asset, dest, progress=None, on_stall=None):
    """Download an asset with stall detection and resume.

    GitHub occasionally stops sending mid-file. With a single blocking read the
    socket simply hung - which is what froze the progress bar for minutes at a
    time. Now each read has a short timeout, and a stall resumes from the last
    byte using an HTTP Range request instead of starting over.
    """
    url = asset.get("browser_download_url") or asset.get("url")
    if not url:
        raise RuntimeError("asset has no download url")
    expect = int(asset.get("size") or 0)      # rounded page label - a hint only
    done = 0
    stalls = 0
    max_stalls = 8
    confirmed = False                          # server told us the exact length

    with open(dest, "wb") as fh:
        while stalls < max_stalls:
            headers = dict(_headers())
            if done:
                headers["Range"] = f"bytes={done}-"
            try:
                # (connect, read) - a dead socket raises within ~30s
                r = requests.get(url, headers=headers, timeout=(15, 30), stream=True)
                if r.status_code == 416:            # already complete
                    break
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"download failed: HTTP {r.status_code}")
                if r.status_code == 200 and done:
                    # server ignored the range: start clean so the file is not corrupt
                    done = 0
                    fh.seek(0)
                    fh.truncate()
                clen = int(r.headers.get("Content-Length") or 0)
                total = clen
                if r.status_code == 206:
                    total = done + clen
                elif not total:
                    total = expect          # display only - never used to judge success
                for chunk in r.iter_content(1 << 16):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        try:
                            progress(done, total or expect)
                        except Exception:
                            pass
                # Judge completeness against what the SERVER said it would send.
                # The size shown on the release page is rounded ("107 MB"), so
                # comparing with it declared finished downloads incomplete and
                # retried them forever - 88,889 bytes short every single time.
                if clen and done < clen:
                    raise RuntimeError(f"short read: {done} of {clen} bytes")
                confirmed = True                     # length satisfied (or unknown)
                break                                # finished cleanly
            except Exception as e:
                # A full disk will never succeed - stop immediately instead of
                # retrying eight times and filling the log.
                if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
                    raise RuntimeError(
                        "no space left on device while downloading - free some "
                        "disk and try again") from e
                stalls += 1
                log.warning("download stall %d/%d at %d bytes: %s",
                            stalls, max_stalls, done, e)
                if on_stall:
                    try:
                        on_stall(done, stalls, e)
                    except Exception:
                        pass
                if stalls >= max_stalls:
                    raise RuntimeError(
                        f"download kept stalling after {stalls} attempts "
                        f"({done} bytes received)") from e
                time.sleep(min(2 * stalls, 10))      # back off, then resume

    # The page label is rounded, so a few bytes short of it is fine - but a body
    # that is wildly smaller than the label means the server sent a partial or an
    # error page, even if its own Content-Length agreed.
    if expect and done < expect * 0.95:
        raise RuntimeError(
            f"incomplete download: {done} of about {expect} bytes")
    if progress:
        try:
            progress(done, done or expect)
        except Exception:
            pass
    return dest


def _header(rel):
    tag = rel.get("tag_name") or "?"
    title = rel.get("name") or tag
    kind = "🟠 Pre-Release" if rel.get("prerelease") else "🟢 Stable"
    out = (f"🚀 <b>New release</b>: {telegram.esc(title)}\n"
           f"Tag: <code>{telegram.esc(tag)}</code> · {kind}")
    if rel.get("html_url"):
        out += f"\n{rel['html_url']}"
    return out


CAPTION_LIMIT = 1024        # Telegram's hard limit for a file caption


def release_hashtag(rel, repo_name=""):
    """A Telegram-safe hashtag: #Repo_v702 (dots and dashes are not allowed)."""
    name = str(repo_name or repo()).split("/")[-1]
    tag = rel.get("tag_name") or ""
    clean = re.sub(r"[^0-9A-Za-z_]", "", tag) or "release"
    base = re.sub(r"[^0-9A-Za-z_]", "", name) or "bonki"
    return f"#{base}_{clean}"


def build_changelog_text(rel, repo_name=""):
    """The FULL changelog as a message: hashtag, release name, notes, link."""
    tag = rel.get("tag_name") or ""
    title = rel.get("name") or tag
    kind = "🟠 Pre-Release" if rel.get("prerelease") else "🟢 Stable"
    head = (f"{release_hashtag(rel, repo_name)}\n"
            f"<b>{telegram.esc(title)}</b> · <code>{telegram.esc(tag)}</code> · {kind}")
    body = (rel.get("body") or "").strip()
    url = rel.get("html_url") or ""
    parts = [head]
    if body:
        parts.append(body)
    if url:
        parts.append(url)
    return "\n\n".join(parts)


def chunk_text(text, limit=3900):
    """Split a long message on line boundaries so nothing is cut mid-word."""
    out, cur = [], ""
    for line in (text or "").split("\n"):
        while len(line) > limit:                     # a single huge line
            if cur:
                out.append(cur); cur = ""
            out.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


def build_caption(rel):
    """One caption holding the header, the changelog and the link.

    Telegram caps a caption at 1024 characters, so a long changelog is trimmed in
    the MIDDLE - the header and the link always survive, which is what matters.
    """
    tag = rel.get("tag_name") or ""
    name = rel.get("name") or tag or "release"
    kind = "🟠 Pre-Release" if rel.get("prerelease") else "🟢 Stable"
    body = (rel.get("body") or "").strip()
    url = rel.get("html_url") or ""

    head = f"🚀 <b>{telegram.esc(name)}</b>\nTag: <code>{telegram.esc(tag)}</code> · {kind}"
    tail = f"\n\n{url}" if url else ""

    if not body:
        return (head + tail)[:CAPTION_LIMIT]

    room = CAPTION_LIMIT - len(head) - len(tail) - 6      # 6 for "\n\n…\n"
    if room <= 40:
        return (head + tail)[:CAPTION_LIMIT]
    if len(body) <= room:
        return head + "\n\n" + body + tail
    cut = body[:room - 1].rstrip()
    # prefer to break on a line boundary so it doesn't stop mid-word
    nl = cut.rfind("\n")
    if nl > room * 0.6:
        cut = cut[:nl]
    return head + "\n\n" + cut + "…" + tail


def post_release(rel, target):
    """Single-channel path - same behaviour as a route, all assets included."""
    return sync_release(rel, {"chat_id": target, "thread_id": "",
                              "prerelease": include_prereleases(),
                              "event": "release"})


async def check(force=False, target=None):
    if force:
        db.set_settings({"gitly_etag": ""})      # manual check: ignore the cache
    """Look for new releases and post them. Returns (posted, status)."""
    if not enabled() and not force:
        return 0, "off"
    tgt = (target or channel()).strip()
    if not tgt:
        return 0, "no-channel"
    resolved, _tried = await asyncio.to_thread(resolve_target, tgt)
    if resolved:
        if str(resolved) != str(tgt):
            db.set_settings({"gitly_channel": str(resolved)})
            log.info("gitly: using %s instead of %s", resolved, tgt)
        tgt = resolved
    else:
        db.set_settings({"gitly_last_error":
                         f"can't reach {tgt} — run /gitly channel and pick it"})
        return 0, f"can't reach {tgt}"
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
    await asyncio.sleep(5)
    # After a restart the state store may be empty (ephemeral disk). Read the
    # channel instead of re-posting everything.
    try:
        if routes():
            n_routes, n_entries = await asyncio.to_thread(rebuild_from_channel)
            if n_entries:
                log.info("leech: rebuilt %d record(s) from the channel across %d "
                         "route(s) - nothing will be re-posted", n_entries, n_routes)
            elif n_routes:
                log.info("leech: no existing posts found to rebuild from")
    except Exception as e:
        log.info("leech rebuild skipped: %s", e)
    while True:
        try:
            db.set_settings({"gitly_loop_last": str(int(time.time()))})
            if enabled() and routes():
                n, status = await check_routes()
                if n:
                    log.info("leech posted %d release(s)", n)
                elif status not in ("ok", "no-routes"):
                    log.info("leech check: %s", status)
                await asyncio.to_thread(db.flush)
            elif enabled() and not routes():
                # Nothing is configured. Stay silent - a removed repo must not
                # keep posting through some other saved setting.
                pass
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
            name = normalize_repo(rest)
            if "/" not in name:
                return await rose._reply(chat, msg,
                                         "Usage: <code>/gitly repo owner/name</code> "
                                         "(a full GitHub link works too)")
            db.set_settings({"gitly_repo": name})
            await asyncio.to_thread(db.flush, True)
            return await rose._reply(chat, msg,
                                     f"📦 Watching <code>{telegram.esc(name)}</code>.")
        if sub == "channel":
            if not rest:
                return await channel_picker(chat, msg)
            target = rest.strip()
            if not target.startswith("@") and not target.lstrip("-").isdigit():
                target = "@" + target.lstrip("@")
            # resolve it now, so a wrong name is caught here instead of at post time
            try:
                info = await asyncio.to_thread(telegram.get_chat, target)
                title = ((info.get("result") or {}).get("title") or target)
            except Exception as e:
                await rose._reply(
                    chat, msg,
                    f"⚠️ I can't reach <code>{telegram.esc(target)}</code>: "
                    f"{telegram.esc(str(e)[:120])}\n\n"
                    f"<b>Use the numeric chat id instead</b> — pick one below, or send "
                    f"any post in the channel and run <code>/gitly channel</code> "
                    f"again.")
                return await channel_picker(chat, msg)
            # create a real route, so there is only one mechanism to remove
            add_route(repo(), target, None, include_prereleases(), "release")
            db.set_settings({"gitly_channel": target, "gitly_on": "1"})
            await asyncio.to_thread(db.flush, True)
            await rose._reply(chat, msg,
                              f"📢 Releases will be posted to <b>{telegram.esc(title)}</b> "
                              f"(<code>{telegram.esc(target)}</code>).")
            await rose._reply(chat, msg, "✅ Auto-push is <b>on</b> — checking GitHub now…")
            n, status = await check(force=True)
            if status == "ok":
                return await rose._reply(
                    chat, msg, f"📦 Posted {n} release(s)."
                    if n else "📦 No new releases yet — I'll post the next one "
                              "within a minute of you publishing it.")
            return await rose._reply(chat, msg, f"⚠️ {telegram.esc(status)}")
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

    if sub in ("debug", "diag"):
        detail = await asyncio.to_thread(probe_github)
        return await rose._reply(chat, msg, f"🔍 <b>GitHub probe</b>\n\n{detail}")

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
    """Releases for the menu, cached briefly so paging does not re-hit GitHub.

    Careful with HTTP 304: it means "nothing changed", NOT "there are none". The
    watcher polls every minute and keeps the ETag fresh, so a menu opened right
    after a poll got a 304 and showed "No releases found" while releases existed.
    """
    now = time.time()
    if force or not _CACHE["rels"] or (now - _CACHE["at"]) > CACHE_TTL:
        rels = fetch_releases(limit=30)
        if rels is None:                     # 304 - unchanged
            if _CACHE["rels"]:
                _CACHE["at"] = now           # keep what we already have
            else:
                # nothing cached yet: ask again without the ETag
                db.set_settings({"gitly_etag": ""})
                rels = fetch_releases(limit=30) or []
                _CACHE["rels"] = [r for r in rels if not r.get("draft")]
                _CACHE["at"] = now
        else:
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
        kind = "🟠 Pre-Release" if r.get("prerelease") else "🟢 Stable"
        if apk:
            mb = int(apk.get("size") or 0) / (1024 * 1024)
            label = f"{kind} · {name} ({tag}) · {mb:.1f} MB"
            rows.append([{"text": label[:52], "callback_data": f"gh:{r.get('id')}"}])
            rows.append([{"text": "📥 send to me",
                          "callback_data": f"gh:{r.get('id')}"},
                         {"text": "📢 to channel",
                          "callback_data": f"ghc:{r.get('id')}"}])
        else:
            rows.append([{"text": f"{kind} · {name} ({tag}) · no APK"[:58],
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


def _known_channels():
    """Channels Bonki has actually seen - these have real numeric ids."""
    return [g for g in db.list_groups() if g.get("chat_type") == "channel"]


def resolve_target(target):
    """Return a chat id that works.

    A channel @username can fail with "chat not found" even when the bot is an
    admin there (custom invite links, renamed channels, or a typo). Rather than
    give up, fall back to the numeric ids Bonki has recorded.
    """
    tried = []
    for cand in [target] + [g.get("chat_id") for g in _known_channels()]:
        if not cand:
            continue
        if str(cand) in [str(t) for t in tried]:
            continue
        tried.append(cand)
        try:
            telegram.get_chat(cand)
            return cand, tried
        except Exception as e:
            log.info("target %s unusable: %s", cand, e)
    return None, tried


async def send_release(cq, rid, to_channel=False):
    """Download one release's APK and send it to the DM (or the channel)."""
    m = cq.get("message") or {}
    dm = (m.get("chat") or {}).get("id")
    if to_channel:
        want = channel()
        if not want:
            await asyncio.to_thread(
                telegram.answer_callback_query, cq.get("id"),
                "No channel set — run /gitly channel", True)
            return
        dm, tried = await asyncio.to_thread(resolve_target, want)
        if not dm:
            await asyncio.to_thread(
                telegram.answer_callback_query, cq.get("id"),
                f"Can't reach {want} — run /gitly channel and pick it", True)
            await asyncio.to_thread(
                telegram.send_message,
                f"⚠️ <b>Can't reach</b> <code>{telegram.esc(want)}</code>.\n"
                f"Tried: {', '.join('<code>' + telegram.esc(str(t)) + '</code>' for t in tried) or 'nothing'}\n\n"
                f"That address doesn't resolve for me. Run <code>/gitly channel</code> "
                f"and pick the channel from the list — it uses the real numeric id.",
                chat_id=(m.get("chat") or {}).get("id"))
            return
        if str(dm) != str(want):
            db.set_settings({"gitly_channel": str(dm)})
            log.info("gitly channel corrected %s -> %s", want, dm)
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
    with _disk_tmp() as tmp:
        dest = os.path.join(tmp, apk.get("name") or "release.apk")
        try:
            await asyncio.to_thread(download_asset, apk, dest)
            caption = build_caption(rel)           # same single-post caption
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


def probe_github():
    """Ask GitHub directly and report exactly what came back - for /gitly debug."""
    url = f"{API}/repos/{repo()}/releases?per_page=5"
    try:
        r = requests.get(url, headers=_headers(), timeout=20)
    except Exception as e:
        return f"network error: {type(e).__name__}: {e}"
    out = [f"repo: <code>{telegram.esc(repo())}</code>",
           f"url: {telegram.esc(url)}",
           f"HTTP: <b>{r.status_code}</b>"]
    try:
        data = r.json()
    except Exception:
        data = None
    if isinstance(data, list):
        out.append(f"releases returned: <b>{len(data)}</b>")
        for x in data[:5]:
            out.append(f"• <code>{telegram.esc(str(x.get('tag_name')))}</code>"
                       f" draft={x.get('draft')} pre={x.get('prerelease')}"
                       f" assets={len(x.get('assets') or [])}")
    else:
        out.append(f"body: {telegram.esc(str(data)[:200])}")
    out.append(f"token in use: {'yes' if _headers().get('Authorization') else 'no'}")
    return "\n".join(out)


async def cmd_releases(chat, msg, user, args):
    """Menu of every GitHub release, tap to pull its APK."""
    from . import rose
    try:
        text, kb = await asyncio.to_thread(menu, 0)
    except Exception as e:
        return await rose._reply(chat, msg, f"⚠️ {telegram.esc(str(e))}")

    if kb is None:
        # Empty list - do NOT stop there. Clear the cache and ETag and ask again.
        db.set_settings({"gitly_etag": ""})
        _CACHE["rels"] = []
        _CACHE["at"] = 0
        try:
            text, kb = await asyncio.to_thread(menu, 0)
        except Exception as e:
            return await rose._reply(chat, msg, f"⚠️ {telegram.esc(str(e))}")
    if kb is None:
        # still nothing: report what GitHub actually said
        detail = await asyncio.to_thread(probe_github)
        return await rose._reply(
            chat, msg,
            f"📦 <b>No releases could be read.</b>\n\n{detail}\n\n"
            f"<i>If GitHub returned releases above, the list was empty in memory — "
            f"run /releases once more.</i>")
    await asyncio.to_thread(telegram.send_message, text, chat_id=chat.get("id"),
                            reply_to=msg.get("message_id"), reply_markup=kb)


async def channel_picker(chat, msg):
    """Buttons for the channels Bonki is already in - no username guessing."""
    from . import rose
    chans = [g for g in db.list_groups()
             if (g.get("chat_type") == "channel")]
    if not chans:
        return await rose._reply(
            chat, msg,
            "I'm not in any channel yet.\n\n"
            "Add me as an <b>admin</b> of the channel (with Post messages), then run "
            "<code>/gitly channel</code> again — I'll list it here with its real id.")
    rows = [[{"text": (g.get("title") or str(g.get("chat_id")))[:40],
              "callback_data": f"gch:{g.get('chat_id')}"}] for g in chans[:12]]
    await asyncio.to_thread(
        telegram.send_message,
        "📢 <b>Which channel should I post releases to?</b>\n"
        "<i>These are channels I'm already in.</i>",
        chat_id=chat.get("id"), reply_to=msg.get("message_id"),
        reply_markup={"inline_keyboard": rows})


# ------------------------------------------------------- leech routes --------
# repo -> where its releases go. Each route is a channel + an optional topic
# (forum thread), so one repo can feed the "releases" topic and another the
# "updates" topic.
def routes():
    try:
        data = json.loads(db.get_settings().get("leech_routes") or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_routes(rows):
    db.set_settings({"leech_routes": json.dumps(rows)[:8000]})


def add_route(repo_name, chat_id, thread_id=None, prerelease=True, event="release"):
    rows = routes()
    event = event if event in EVENTS else "release"
    key = (repo_name.lower(), str(chat_id), str(thread_id or ""), event)
    for r in rows:
        if (str(r.get("repo", "")).lower(), str(r.get("chat_id")),
                str(r.get("thread_id") or ""), str(r.get("event") or "release")) == key:
            r["prerelease"] = prerelease
            _save_routes(rows)
            return False
    rows.append({"repo": repo_name, "chat_id": str(chat_id),
                 "thread_id": str(thread_id) if thread_id else "",
                 "prerelease": bool(prerelease), "event": event})
    _save_routes(rows)
    return True


def remove_route(repo_name=None, chat_id=None):
    """Remove matching routes.

    repo_name only -> that repo in every chat
    chat_id only   -> every route in that chat
    both           -> just that pair
    """
    rows = routes()
    keep = []
    for r in rows:
        match = True
        if repo_name:
            match = match and str(r.get("repo", "")).lower() == str(repo_name).lower()
        if chat_id is not None:
            match = match and str(r.get("chat_id")) == str(chat_id)
        if match:
            continue
        keep.append(r)
    _save_routes(keep)
    return len(rows) - len(keep)


def _seen_key(rid, route):
    return f"{rid}:{route.get('chat_id')}:{route.get('thread_id') or ''}"


def post_release_to(rel, route):
    """Post one release into one route (channel + topic). One post per release."""
    if rel.get("draft"):
        return "draft"
    if rel.get("prerelease") and not route.get("prerelease", True):
        return "prerelease"
    rid = rel.get("id") or rel.get("tag_name")
    if not _mark_seen(_seen_key(rid, route)):
        return "already"

    chat_id = route.get("chat_id")
    thread = route.get("thread_id") or None
    apk = pick_apk(rel.get("assets"))
    caption = build_caption(rel)

    if not apk:
        try:
            telegram.send_message(caption, chat_id=chat_id, thread_id=thread)
        except Exception as e:
            log.warning("leech post failed: %s", e)
            db.set_settings({"gitly_last_error": str(e)[:160]})
            return "post-failed"
        return "no-apk"

    size = int(apk.get("size") or 0)
    name = apk.get("name") or "release.apk"
    if size > UPLOAD_LIMIT:
        mb = size / (1024 * 1024)
        try:
            telegram.send_message(
                caption + f"\n\n⚠️ {telegram.esc(name)} is {mb:.1f} MB — over "
                          f"Telegram's 50 MB limit.\n"
                          f"Download: {apk.get('browser_download_url','')}",
                chat_id=chat_id, thread_id=thread)
        except Exception as e:
            log.warning("leech oversize failed: %s", e)
        return "too-big"

    with _disk_tmp() as tmp:
        dest = os.path.join(tmp, name)
        try:
            download_asset(apk, dest)
            telegram.send_document(dest, name, caption, chat_id, thread_id=thread)
            return "sent"
        except Exception as e:
            log.warning("leech apk failed: %s", e)
            return "apk-failed"


_SYNC_LOCK = None          # created lazily on the running loop


def _lock():
    global _SYNC_LOCK
    if _SYNC_LOCK is None:
        _SYNC_LOCK = asyncio.Lock()
    return _SYNC_LOCK


async def check_routes(force=False):
    """Run one sync cycle, never two at once.

    The background loop and a manual /leech check (or resync) used to overlap and
    both upload the same files, which is why every file appeared twice.
    """
    if not routes():
        return 0, "no-routes"
    lk = _lock()
    if lk.locked():
        return 0, "busy"
    async with lk:
        return await _check_routes_inner(force)


async def _check_routes_inner(force=False):
    if force:
        db.set_settings({"gitly_backoff_until": ""})
    rows = routes()
    posted = 0
    errors = []
    for repo_name in {r.get("repo") for r in rows}:
        mine = [r for r in rows if r.get("repo") == repo_name]
        for event in {str(r.get("event") or "release") for r in mine}:
            targets = [r for r in mine if str(r.get("event") or "release") == event]
            try:
                if event == "release":
                    items = await asyncio.to_thread(fetch_releases, repo_name, 15)
                elif event == "commit":
                    items = await asyncio.to_thread(fetch_commits, repo_name, 10)
                else:
                    items = await asyncio.to_thread(fetch_issues, repo_name, 10)
            except Exception as e:
                errors.append(f"{repo_name}/{event}: {e}")
                continue
            if not items:
                continue
            for route in targets:
                for item in reversed(items):
                    try:
                        if event == "release":
                            st = await asyncio.to_thread(sync_release, item, route)
                            ok = st in ("sent", "replaced", "edited", "sent-text")
                            if str(st).startswith("partial"):
                                ok = True
                                db.set_settings({"gitly_last_error":
                                                 f"{item.get('tag_name')}: "
                                                 f"{st.split(':')[1]} file(s) failed, "
                                                 f"retrying next check"})
                        else:
                            st = await asyncio.to_thread(post_event_to, repo_name,
                                                         event, item, route)
                            ok = st == "sent"
                        if ok:
                            posted += 1
                    except Exception as e:
                        log.warning("route post failed: %s", e)
    db.set_settings({"gitly_last_check": str(int(time.time())),
                     "gitly_last_error": "; ".join(errors)[:200]})
    return posted, ("ok" if not errors else "; ".join(errors))


LEECH_HELP = """📦 <b>Bonki Leech — commands</b>

<b>1. Choose the repo</b> (any chat or this DM)
<code>/leech repo owner/name</code>
A link works too: <code>/leech repo https://github.com/owner/name</code>

<b>2. Send it to a topic</b> (run INSIDE the channel topic)
<code>/leech here release</code> — the APK / zip / tar.gz files
<code>/leech here commit</code> — new commits
<code>/leech here issue</code> — issues and pull requests
Repeat in each topic. One repo can feed several topics.

<b>3. Check</b>
<code>/leech</code> — what is being watched and where
<code>/gitly debug</code> — what GitHub actually returned

<b>Remove</b>
<code>/leech remove</code> — everything in THIS chat
<code>/leech remove owner/name</code> — that repo everywhere
<code>/leech remove all</code> — every repo, everywhere

<b>Turn on / off</b>
<code>/leech on</code> · <code>/leech off</code>

<b>Check right now</b>
<code>/leech check</code> — forces an immediate look at every repo

<b>Progress</b>
<code>/leech progress on</code> · <code>off</code> — live download/upload % in DM

<b>Who uploads</b>
<code>/leech upload account</code> · <code>bot</code>

<b>Something stuck?</b>
<code>/leech resync</code> — forget what was posted and upload it all again
<code>/leech rebuild</code> — rebuild the record from the channel (after a restart)

<b>Start over</b>
<code>/leech reset</code> — delete every stored leech setting (asks first)

<b>Mode</b>
<code>/mode leech</code> — this chat becomes a release feed only
<code>/mode find</code> — back to the file bot

<i>Releases are read from GitHub's public pages, so there is no API limit.
Files over 50 MB upload through your user account (up to 2 GB).</i>

<b>It runs on your server</b> — you can close Telegram, log out or switch
off your phone; downloads and uploads carry on and the posts appear anyway.
The only thing that stops it is the bot process going down.</i>"""


# Everything the leech side stores. Reset wipes exactly these.
LEECH_KEYS = (
    "leech_routes", "leech_state", "leech_repo",
    "gitly_repo", "gitly_channel", "gitly_on", "gitly_interval",
    "gitly_prerelease", "gitly_seen", "gitly_etags", "gitly_etag",
    "gitly_last_check", "gitly_last_error", "gitly_backoff_until",
)


def reset_all(also_mode=True):
    """Wipe every stored leech setting. Returns how many keys were cleared."""
    cleared = 0
    for k in LEECH_KEYS:
        try:
            if db.get_settings().get(k) not in (None, ""):
                cleared += 1
            db.set_settings({k: ""})
        except Exception as e:
            log.info("reset %s failed: %s", k, e)
    _CACHE["rels"] = []
    _CACHE["at"] = 0
    if also_mode:
        from . import modes
        for g in db.list_groups():
            if modes.get_mode(g.get("chat_id")) == modes.LEECH:
                modes.set_mode(g.get("chat_id"), modes.FIND)
    db.flush(force=True)
    return cleared


async def cmd_leech(chat, msg, user, args):
    """Configure the leech bot: which repo, and which topic it feeds."""
    from . import approval, modes, rose
    cid = chat.get("id")
    parts = (args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    owner = approval.is_owner((user or {}).get("id"))
    admin = owner or await rose._is_admin(cid, (user or {}).get("id"),
                                          chat.get("type"))
    if not admin:
        return await rose._reply(chat, msg, "Admins only.")

    if sub in ("repo", "watch"):
        if "/" not in rest:
            return await rose._reply(chat, msg,
                                     "Usage: <code>/leech repo owner/name</code>")
        name = normalize_repo(rest)
        if "/" not in name:
            return await rose._reply(chat, msg,
                                     "That doesn't look like a repo. Send "
                                     "<code>owner/name</code> or paste the GitHub link.")
        try:
            rels = await asyncio.to_thread(fetch_releases, name, 3)
            n = len(rels or [])
        except Exception as e:
            owner, _, short = name.partition("/")
            near = await asyncio.to_thread(suggest_repos, owner, short)
            hint = ""
            if near:
                hint = ("\n\nDid you mean: " +
                        " · ".join(f"<code>{telegram.esc(owner + '/' + x)}</code>"
                                   for x in near))
            return await rose._reply(
                chat, msg,
                f"⚠️ <code>{telegram.esc(name)}</code> — {telegram.esc(str(e))}"
                f"{hint}")
        db.set_settings({"leech_repo": name, "gitly_on": "1"})
        await asyncio.to_thread(db.flush, True)
        modes.set_mode(cid, modes.LEECH)
        return await rose._reply(
            chat, msg,
            f"📦 Watching <code>{telegram.esc(name)}</code> — {n} release(s) visible.\n\n"
            f"Now open the <b>topic</b> you want releases in and send "
            f"<code>/leech here</code>.")

    if sub in ("help", "?", "commands"):
        return await rose._reply(chat, msg, LEECH_HELP)

    if sub in ("rebuild", "restore", "scanchannel"):
        if not owner and not admin:
            return await rose._reply(chat, msg, "Admins only.")
        await rose._reply(chat, msg, "🔎 Reading the channel to rebuild the record…")
        try:
            n_routes, n_entries = await asyncio.to_thread(rebuild_from_channel, 200)
        except Exception as e:
            return await rose._reply(chat, msg, f"⚠️ {telegram.esc(str(e))}")
        if not n_entries:
            return await rose._reply(
                chat, msg,
                "Nothing found to rebuild.\n<i>This needs the user account "
                "connected — a bot cannot read channel history.</i>")
        return await rose._reply(
            chat, msg,
            f"✅ Rebuilt {n_entries} record(s) from {n_routes} route(s).\n"
            f"Files already in the channel will not be posted again.")

    if sub in ("resync", "repair", "redo", "force"):
        if not owner and not admin:
            return await rose._reply(chat, msg, "Admins only.")
        had = len(_rstate())
        db.set_settings({"leech_state": "", "gitly_seen": "", "gitly_etags": "",
                         "gitly_backoff_until": ""})
        await asyncio.to_thread(db.flush, True)
        await rose._reply(
            chat, msg,
            f"♻️ Cleared the record of {had} posted release(s).\n"
            f"Re-checking now — files that were skipped will be uploaded again…")
        n, status = await check_routes(force=True)
        if status == "busy":
            lines = ["⏳ A check is already running — the files will appear "
                     "shortly. Nothing was duplicated."]
        else:
            lines = [f"✅ Re-synced — posted <b>{n}</b> item(s)."
                     if n else f"⚠️ Nothing posted: {telegram.esc(status)}"]
        for r in routes():
            g = db.get_group_by_chat(r.get("chat_id"))
            title = (g or {}).get("title") or r.get("chat_id")
            ev = EVENTS.get(str(r.get("event") or "release"), "🚀 Releases")
            where = f"topic {r['thread_id']}" if r.get("thread_id") else "main feed"
            lines.append(f"• {ev} · <code>{telegram.esc(str(r.get('repo')))}</code>"
                         f" → {telegram.esc(title)} · {where}")
        return await rose._reply(chat, msg, "\n".join(lines))

    if sub in ("upload", "sender", "via"):
        want = rest.strip().lower()
        if want not in ("account", "bot"):
            return await rose._reply(
                chat, msg,
                f"Now uploading as: <b>{'your account' if upload_via_account() else 'the bot'}</b>\n\n"
                "<code>/leech upload account</code> — everything through your user "
                "account (2 GB, no 50 MB cap)\n"
                "<code>/leech upload bot</code> — through the bot (50 MB max)")
        db.set_settings({"leech_upload_via": want})
        await asyncio.to_thread(db.flush, True)
        return await rose._reply(
            chat, msg,
            "✅ Files will upload through <b>your account</b>."
            if want == "account" else
            "✅ Files will upload through <b>the bot</b> (50 MB limit applies).")

    if sub in ("progress", "verbose"):
        want = rest.strip().lower()
        on = want not in ("off", "0", "no", "false")
        db.set_settings({"leech_progress": "1" if on else "0"})
        await asyncio.to_thread(db.flush, True)
        return await rose._reply(
            chat, msg,
            "📊 Progress reports <b>on</b> — you'll get a live download/upload "
            "percentage in this DM for every file."
            if on else "📊 Progress reports off.")

    if sub in ("check", "now", "scan", "sync", "run"):
        if not routes():
            return await rose._reply(
                chat, msg,
                "Nothing is being watched yet.\n"
                "1. <code>/leech repo owner/name</code>\n"
                "2. open the topic and send <code>/leech here release</code>")
        await rose._reply(chat, msg,
                          f"🔎 Checking {len({r.get('repo') for r in routes()})} "
                          f"repo(s) now…")
        lines = []
        # per-release file state: how many uploaded, how many still missing
        try:
            for rp in {r.get("repo") for r in routes()}:
                rels = await asyncio.to_thread(fetch_releases, rp, 5)
                st_all = _rstate()
                for rel in (rels or [])[:3]:
                    rid = rel.get("id") or rel.get("tag_name")
                    n_assets = len(rel.get("assets") or [])
                    if not n_assets:
                        continue
                    for route in [x for x in routes() if x.get("repo") == rp]:
                        ent = st_all.get(_state_key(route, rid)) or {}
                        done = ent.get("done") or {}
                        got = len([k for k in done])
                        mark = "✅" if got >= n_assets else "⏳"
                        lines.append(f"{mark} <code>{telegram.esc(str(rel.get('tag_name')))}</code>"
                                     f": {got}/{n_assets} file(s) uploaded")
        except Exception as e:
            lines.append(f"⚠️ {telegram.esc(str(e)[:80])}")
        lines.append("")
        # show what GitHub returned, so an empty fetch is visible
        for rp in {r.get("repo") for r in routes()}:
            try:
                found = await asyncio.to_thread(fetch_releases, rp, 5)
                cnt = len(found or [])
                assets = sum(len(x.get("assets") or []) for x in (found or []))
                lines.append(f"• <code>{telegram.esc(str(rp))}</code>: {cnt} "
                             f"release(s), {assets} file(s) found")
            except Exception as e:
                lines.append(f"• <code>{telegram.esc(str(rp))}</code>: "
                             f"⚠️ {telegram.esc(str(e)[:80])}")
        lines.append("")
        n, status = await check_routes(force=True)
        if n:
            lines.append(f"✅ Posted <b>{n}</b> item(s).")
        elif status == "ok":
            lines.append("✅ Checked — nothing new since last time.")
        else:
            lines.append(f"⚠️ {telegram.esc(status)}")
        # show what each route points at, so a wrong topic is obvious
        lines.append("")
        lines.append("<b>Watching</b>")
        for r in routes():
            g = db.get_group_by_chat(r.get("chat_id"))
            title = (g or {}).get("title") or r.get("chat_id")
            ev = EVENTS.get(str(r.get("event") or "release"), "🚀 Releases")
            where = f"topic {r['thread_id']}" if r.get("thread_id") else "main feed"
            lines.append(f"• {ev} · <code>{telegram.esc(str(r.get('repo')))}</code>"
                         f" → {telegram.esc(title)} · {where}")
        return await rose._reply(chat, msg, "\n".join(lines))

    if sub in ("reset", "wipe", "factory"):
        if not owner:
            return await rose._reply(chat, msg, "🔒 Owner only — this deletes data.")
        await asyncio.to_thread(
            telegram.send_message,
            "🧨 <b>Reset the leech bot?</b>\n\n"
            "This deletes <b>everything</b> the leech side has stored:\n"
            "• every watched repo and route\n"
            "• the record of which releases were already posted\n"
            "• cached GitHub data and rate-limit state\n"
            "• all leech settings, and it switches chats back to Find mode\n\n"
            "Messages already posted in your channel are <b>not</b> deleted — "
            "Telegram keeps those.\n\n"
            "<b>This cannot be undone.</b>",
            chat_id=chat.get("id"), reply_to=msg.get("message_id"),
            reply_markup={"inline_keyboard": [[
                {"text": "🧨 Yes, reset everything", "callback_data": "lreset:yes"},
                {"text": "Cancel", "callback_data": "lreset:no"}]]})
        return

    if sub in ("here", "topic"):
        # Anywhere a bot can post is a valid destination: a broadcast channel, a
        # normal group, or a forum topic inside a group. Only a DM is refused.
        if str(chat.get("type")) == "private":
            return await rose._reply(
                chat, msg,
                "⚠️ Run <code>/leech here</code> <b>inside the chat or topic</b> "
                "where the posts should land — a channel, a group, or a forum "
                "topic.\n\n"
                "A DM can't receive releases, so it can't be a destination.\n"
                "Use <code>/leech help</code> for the full steps.")
        repo_name = (db.get_settings().get("leech_repo") or repo()).strip()
        # which kind of activity? default = releases
        want = (rest or "release").strip().lower()
        want = {"releases": "release", "commits": "commit", "push": "commit",
                "issues": "issue", "prs": "issue", "pull": "issue",
                "everything": "release"}.get(want, want)
        if want not in EVENTS:
            return await rose._reply(
                chat, msg,
                f"Unknown type <code>{telegram.esc(rest)}</code>.\n"
                f"Use: <code>/leech here release</code>, "
                f"<code>/leech here commit</code> or <code>/leech here issue</code>")
        thread = msg.get("message_thread_id")
        is_topic = bool(thread)
        added = add_route(repo_name, cid, thread, include_prereleases(), event=want)
        db.set_settings({"gitly_on": "1"})      # a route implies you want it live
        await asyncio.to_thread(db.flush, True)
        modes.set_mode(cid, modes.LEECH)
        if is_topic:
            where = f"this topic (<code>{thread}</code>)"
        elif str(chat.get("type")) == "channel":
            where = "the channel's main feed (no topic)"
        else:
            where = "this group's main feed (no topic)"
        return await rose._reply(
            chat, msg,
            ("✅ " if added else "♻️ Already set: ") +
            f"<b>{EVENTS[want]}</b> from <code>{telegram.esc(repo_name)}</code> "
            f"will be posted to {where}.\n\n"
            f"<i>Run this inside each topic you want a different kind of update in — "
            f"releases in one topic, commits in another.</i>")

    if sub in ("remove", "rm", "stop", "delete", "unwatch"):
        target = normalize_repo(rest) if rest else ""
        rows_before = routes()
        if target and "/" in target:
            n = remove_route(target, None)          # that repo, in every chat
            label = f"<code>{telegram.esc(target)}</code>"
        elif target.lower() in ("all", "everything"):
            _save_routes([])
            n = len(rows_before)
            label = "all repos"
        else:
            n = remove_route(None, cid)             # everything in THIS chat
            label = "this chat"
        # clear the "repo being configured" pointer if it is now gone
        cur = (db.get_settings().get("leech_repo") or "").strip()
        if cur and (not target or target.lower() == "all" or
                    normalize_repo(cur).lower() == target.lower()):
            if not [r for r in routes() if normalize_repo(str(r.get("repo"))).lower()
                    == normalize_repo(cur).lower()]:
                db.set_settings({"leech_repo": ""})
        await asyncio.to_thread(db.flush, True)
        if not n:
            return await rose._reply(
                chat, msg,
                "Nothing matched.\n<code>/leech</code> shows what is currently "
                "watched.")
        left = len(routes())
        if not left:
            # nothing left anywhere: switch the watcher off and drop the old
            # single-channel pointer, so no stale setting can keep it alive
            db.set_settings({"gitly_on": "0", "gitly_channel": "",
                             "leech_repo": ""})
            await asyncio.to_thread(db.flush, True)
        note = ("\n\nNo routes left — the watcher has nothing to do. "
                "Use <code>/mode find</code> if you want this chat to be a find "
                "bot again." if not left else
                f"\n\n{left} route(s) still active — <code>/leech</code> to see them.")
        return await rose._reply(chat, msg,
                                 f"🗑 Removed {n} route(s) for {label}.{note}")

    if sub in ("on", "off"):
        db.set_settings({"gitly_on": "1" if sub == "on" else "0"})
        await asyncio.to_thread(db.flush, True)
        return await rose._reply(chat, msg,
                                 "✅ Leeching on." if sub == "on" else "⏹ Leeching off.")

    # status
    rows = routes()
    lines = ["📦 <b>Bonki Leech</b>", f"<i>code: {BUILD} · instance {INSTANCE}</i>", ""]
    cur = (db.get_settings().get("leech_repo") or "").strip()
    lines.append(f"• repo being configured: "
                 f"{'<code>' + telegram.esc(cur) + '</code>' if cur else 'none'}")
    try:
        st_info = db.storage_info()
        if not st_info.get("durable"):
            lines.append("• ⚠️ <b>storage is not durable</b> — a restart wipes my "
                         "memory. I rebuild it from the channel on boot, but "
                         "setting GITHUB_TOKEN (state gist) is safer.")
    except Exception:
        pass
    try:
        last_loop = int(db.get_settings().get("gitly_loop_last") or 0)
    except (TypeError, ValueError):
        last_loop = 0
    if last_loop:
        ago = int(time.time()) - last_loop
        lines.append(f"• last automatic check: {ago}s ago")
    if enabled() and rows:
        lines.append(f"• watcher: ✅ <b>automatic</b> — checks every {interval()}s")
        lines.append("• <i>runs on your server — you can close Telegram and it "
                     "keeps working</i>")
    elif rows:
        lines.append("• watcher: ⏹ paused — <code>/leech on</code> to resume")
    else:
        lines.append("• watcher: idle (nothing configured)")
    if not rows:
        lines.append("")
        lines.append("No routes yet. Three steps:")
        lines.append("1. <code>/leech repo owner/name</code>")
        lines.append("2. open the topic you want the updates in")
        lines.append("3. <code>/leech here release</code> — or <code>commit</code> / "
                     "<code>issue</code>")
    else:
        lines.append("")
        lines.append("<b>Routes</b>")
        for r in rows:
            g = db.get_group_by_chat(r.get("chat_id"))
            title = (g or {}).get("title") or r.get("chat_id")
            topic = f"topic {r['thread_id']}" if r.get("thread_id") else "main"
            pre = " + pre-releases" if r.get("prerelease") else ""
            ev = EVENTS.get(str(r.get("event") or "release"), "🚀 Releases")
            lines.append(f"• {ev} · <code>{telegram.esc(str(r.get('repo')))}</code>"
                         f" → {telegram.esc(title)} · {topic}{pre}")
    await rose._reply(chat, msg, "\n".join(lines))


# ------------------------------------------------- repo activity (events) ----
# A repo produces more than releases. Each route picks WHICH activity it wants,
# so "releases" can go to one topic and "commits" or "issues" to another.
EVENTS = {
    "release": "🚀 Releases",
    "commit": "🔨 Commits",
    "issue": "🐞 Issues & PRs",
}


def _etags():
    try:
        return json.loads(db.get_settings().get("gitly_etags") or "{}")
    except Exception:
        return {}


def _save_etags(d):
    # keep it small: 40 entries is plenty for a few repos x a few endpoints
    items = list(d.items())[-40:]
    db.set_settings({"gitly_etags": json.dumps(dict(items))})


def fetch_json(path):
    """GET an API path with a per-URL ETag. Returns None when unchanged."""
    url = path if path.startswith("http") else f"{API}{path}"
    etags = _etags()
    headers = _headers()
    if etags.get(url):
        headers["If-None-Match"] = etags[url]
    try:
        r = requests.get(url, headers=headers, timeout=20)
    except Exception as e:
        raise RuntimeError(f"network error ({type(e).__name__})") from e
    if r.status_code == 304:
        return None
    if r.status_code == 404:
        raise RuntimeError("not found (private repo, or wrong name)")
    if r.status_code == 403:
        raise RuntimeError("GitHub rate limit reached")
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}")
    tag = r.headers.get("ETag")
    if tag:
        etags[url] = tag
        _save_etags(etags)
    try:
        return r.json()
    except Exception:
        return None


def fetch_commits(repo_name, limit=5):
    """Public commits - web feed first, API only as a token-backed fallback."""
    try:
        return fetch_commits_web(repo_name, limit)
    except Exception as e:
        log.info("commit web feed failed for %s: %s", repo_name, e)
        if not _headers().get("Authorization"):
            raise
    data = fetch_json(f"/repos/{repo_name}/commits?per_page={limit}")
    if data is None:
        return None
    out = []
    for c in (data or []):
        commit = c.get("commit") or {}
        out.append({
            "id": c.get("sha"),
            "short": (c.get("sha") or "")[:7],
            "message": (commit.get("message") or "").split("\n")[0][:200],
            "author": ((commit.get("author") or {}).get("name") or "unknown"),
            "url": c.get("html_url") or "",
            "date": ((commit.get("author") or {}).get("date") or ""),
        })
    return out


def fetch_issues(repo_name, limit=5):
    data = fetch_json(f"/repos/{repo_name}/issues?state=all&per_page={limit}"
                      f"&sort=created&direction=desc")
    if data is None:
        return None
    out = []
    for i in (data or []):
        if i.get("pull_request"):
            kind = "PR"
        else:
            kind = "issue"
        out.append({
            "id": i.get("id"),
            "number": i.get("number"),
            "title": (i.get("title") or "")[:200],
            "body": (i.get("body") or "")[:600],
            "state": i.get("state") or "open",
            "kind": kind,
            "user": ((i.get("user") or {}).get("login") or ""),
            "url": i.get("html_url") or "",
        })
    return out


def format_commit(repo_name, c):
    return (f"🔨 <b>New commit</b> in <code>{telegram.esc(repo_name)}</code>\n"
            f"<code>{telegram.esc(c['short'])}</code> — {telegram.esc(c['message'])}\n"
            f"by {telegram.esc(c['author'])}\n{c['url']}")


def format_issue(repo_name, i):
    icon = "🟢" if i["state"] == "open" else "🔴"
    label = "PR" if i["kind"] == "PR" else "Issue"
    text = (f"{icon} <b>{label} #{i['number']}</b> · {i['state']} in "
            f"<code>{telegram.esc(repo_name)}</code>\n"
            f"<b>{telegram.esc(i['title'])}</b>")
    if i.get("body"):
        text += f"\n\n{telegram.esc(i['body'][:400])}"
    if i.get("user"):
        text += f"\n\nby {telegram.esc(i['user'])}"
    return text + f"\n{i['url']}"


def post_event_to(repo_name, event, item, route):
    """Post one commit/issue into one route."""
    key = f"{event}:{item.get('id')}:{route.get('chat_id')}:{route.get('thread_id') or ''}"
    if not _mark_seen(key):
        return "already"
    text = (format_commit(repo_name, item) if event == "commit"
            else format_issue(repo_name, item))
    try:
        telegram.send_message(text[:3800], chat_id=route.get("chat_id"),
                              thread_id=route.get("thread_id") or None)
        return "sent"
    except Exception as e:
        log.warning("event post failed: %s", e)
        db.set_settings({"gitly_last_error": str(e)[:160]})
        return "post-failed"


# ------------------------------------------------- per-release sync state ----
# Telegram cannot rename a file inside a message, so when a release's assets
# change we delete our old posts and upload the new ones. When only the text
# changes we edit the caption, which keeps the message and its file intact.
def _rstate():
    raw = db.get_settings().get("leech_state") or "{}"
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        if raw and raw not in ("", "{}"):
            log.error("leech_state is corrupt (%s) - %d chars lost. Every release "
                      "will look new until /leech resync clears it.", e, len(raw))
        return {}


def _save_rstate(st):
    """Persist the per-release state as VALID JSON, trimming only whole entries.

    The old version cut the JSON string at 60000 characters. Once the state grew
    past that, the text was no longer valid JSON, _rstate() silently returned {},
    and every release looked new again - so the bot re-uploaded everything on every
    cycle. That is what caused the repeated uploads.
    """
    if not st:
        db.set_settings({"leech_state": ""})
        return
    # newest first, then keep as many whole entries as fit
    ordered = sorted(st.items(),
                     key=lambda kv: (kv[1] or {}).get("t", 0) if isinstance(kv[1], dict)
                     else 0, reverse=True)
    for keep in (len(ordered), 80, 40, 20, 10, 5, 1):
        blob = json.dumps(dict(ordered[:keep]))
        if len(blob) <= 200000:
            db.set_settings({"leech_state": blob})
            return
    db.set_settings({"leech_state": json.dumps(dict(ordered[:1]))})


def asset_sig(assets):
    """A fingerprint of the asset set: any rename/replace changes it."""
    bits = []
    for a in sorted((assets or []), key=lambda x: str(x.get("name"))):
        bits.append(f"{a.get('id') or a.get('name')}:{a.get('name')}:{a.get('size')}")
    return "|".join(bits)


def _state_key(route, rid):
    return f"{route.get('chat_id')}:{route.get('thread_id') or ''}:{rid}"


def await_userbot_delete(chat_id, message_id):
    from . import userbot
    return userbot.run_sync(userbot.delete_message(chat_id, message_id), timeout=180)


def await_userbot_send(chat_id, path, caption, thread_id, tracker=None):
    """Upload through the user account, on the loop Telethon lives on."""
    from . import userbot
    return userbot.run_sync(
        userbot.send_file(chat_id, path, caption, thread_id, tracker=tracker),
        timeout=3600)


def _disk_tmp():
    """TemporaryDirectory on a real disk (never a RAM-backed /tmp)."""
    try:
        from . import lowmem
        where = lowmem.work_dir()
        if where:
            return tempfile.TemporaryDirectory(dir=where)
    except Exception:
        pass
    return tempfile.TemporaryDirectory()


def _check_space(asset):
    """Refuse early (with a clear reason) when the disk cannot hold the file."""
    try:
        from . import lowmem
        ok, have, need = lowmem.can_download(asset.get("size"))
        if not ok:
            raise RuntimeError(
                f"not enough disk: need about {need} MB, only {have} MB free. "
                f"Free some space or the download cannot start.")
    except RuntimeError:
        raise
    except Exception:
        pass


def upload_via_account():
    """Upload through the user account for EVERY file, not just the big ones.

    The bot has a 50 MB ceiling; the account has 2 GB and sends the file as you
    rather than as the bot. On by default, /leech upload bot to use the bot.
    """
    want = str(db.get_settings().get("leech_upload_via") or "account").lower()
    if want == "bot":
        return False
    try:
        from . import userbot
        return userbot.is_running()
    except Exception:
        return False


def _send_asset(rel, asset, route, index, total):
    """Upload one asset with a SHORT caption (name only, no link)."""
    name = asset.get("name") or "file"
    size = int(asset.get("size") or 0)
    chat_id = route.get("chat_id")
    thread = route.get("thread_id") or None
    caption = f"📎 <b>{telegram.esc(name)}</b>"
    caption = f"📎 <b>{telegram.esc(name)}</b>"

    use_account = upload_via_account()
    if size > UPLOAD_LIMIT and not use_account:
        mb = size / (1024 * 1024)
        telegram.send_message(
            f"⚠️ <b>{telegram.esc(name)}</b> is {mb:.1f} MB — over the 50 MB "
            f"<b>bot</b> limit and no user account is connected.\n"
            f"Download: {asset.get('browser_download_url', '')}",
            chat_id=chat_id, thread_id=thread)
        return None

    from . import progress as _pg
    with _disk_tmp() as tmp:
        dest = os.path.join(tmp, name)
        tr = _pg.Tracker(name, size) if _pg.enabled() else None
        try:
            _check_space(asset)
            download_asset(asset, dest, progress=_pg.callback_for(tr),
                           on_stall=(tr.stalled if tr else None))
            if tr:
                tr.uploading()
            if use_account:
                r = await_userbot_send(chat_id, dest, caption, thread, tracker=tr)
            else:
                r = telegram.send_document(dest, name, caption, chat_id,
                                           thread_id=thread, tracker=tr)
            if tr:
                tr.done()
            return ((r or {}).get("result") or {}).get("message_id")
        except Exception as e:
            if tr:
                tr.failed(e)
            raise


def _post_changelog(rel, route, repo_name=""):
    """Post the FULL changelog as text message(s). Returns their message ids."""
    text = build_changelog_text(rel, repo_name)
    ids = []
    for part in chunk_text(text):
        r = telegram.send_message(part, chat_id=route.get("chat_id"),
                                  thread_id=route.get("thread_id") or None)
        mid = ((r or {}).get("result") or {}).get("message_id")
        if mid:
            ids.append(mid)
    return ids


def sync_release(rel, route):
    """Keep Telegram in step with one release.

    Layout, exactly as asked for:
        #hashtag + release name + type
        the WHOLE changelog (its own message - a caption cannot hold it)
        each file, short caption, no links
    """
    if rel.get("draft"):
        return "draft"
    if rel.get("prerelease") and not route.get("prerelease", True):
        return "prerelease"

    rid = rel.get("id") or rel.get("tag_name")
    assets = rel.get("assets") or []
    sig = asset_sig(assets)
    body = (rel.get("body") or "").strip()
    repo_name = str(route.get("repo") or "")
    st = _rstate()
    key = _state_key(route, rid)
    entry = st.get(key)

    # ---- nothing yet: wait briefly for files, then post the changelog alone ----
    if not entry and not assets:
        first = st.get(key + ":first")
        if not first:
            st[key + ":first"] = int(time.time())
            _save_rstate(st)
            return "waiting"
        if time.time() - int(first) < 90:
            return "waiting"
        ids = _post_changelog(rel, route, repo_name)
        st[key] = {"sig": "", "body": body, "done": {}, "msg_ids": [],
                   "text_ids": ids, "t": int(time.time())}
        _save_rstate(st)
        try:
            db.flush(force=True)
        except Exception:
            pass
        return "sent-text"

    # ---- a rebuilt record has no signature: adopt it if every file is known ----
    if entry and entry.get("sig") == "" and sig:
        known = {k for k in (entry.get("done") or {})}
        current = {a.get("name") for a in assets if a.get("name")}
        if current and current <= known:
            entry["sig"] = sig

    # ---- the file set changed: drop our file posts, upload the new set ----
    if entry and entry.get("sig") != sig:
        for mid in (entry.get("msg_ids") or []):
            try:
                telegram.delete_message(route.get("chat_id"), mid)
            except Exception:
                try:
                    from . import userbot
                    if userbot.is_running():
                        await_userbot_delete(route.get("chat_id"), mid)
                except Exception:
                    pass
        entry = {"sig": sig, "body": entry.get("body") or body, "done": {},
                 "msg_ids": [], "text_ids": entry.get("text_ids") or [],
                 "t": int(time.time())}

    if not entry:
        entry = {"sig": sig, "body": body, "done": {}, "msg_ids": [],
                 "text_ids": [], "t": int(time.time())}

    done = entry.get("done") or {}
    if not isinstance(done, dict):
        done = {}

    # ---- the changelog goes FIRST so it sits above the files ----
    text_ids = entry.get("text_ids") or []
    if not text_ids:
        text_ids = _post_changelog(rel, route, repo_name)

    # ---- upload whatever is missing ----
    pending = [(i, a) for i, a in enumerate(assets)
               if (a.get("name") or f"file{i}") not in done]
    added, failed = 0, []
    if pending:
        import threading
        lock = threading.Lock()
        results = {}

        def _one(i, asset):
            nm = asset.get("name") or f"file{i}"
            for attempt in range(2):
                try:
                    mid = _send_asset(rel, asset, route, i, len(assets))
                    with lock:
                        done[nm] = mid
                        results[nm] = True
                    return
                except Exception as e:
                    log.warning("asset %s attempt %d failed: %s", nm, attempt + 1, e)
                    if attempt == 0:
                        time.sleep(3)
            with lock:
                results[nm] = False

        try:
            from . import lowmem
            workers = 1 if lowmem.is_low() else min(3, len(pending))
        except Exception:
            workers = min(3, len(pending))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_one, i, a) for i, a in pending]
            for f in futs:
                try:
                    f.result(timeout=1800)
                except Exception as e:
                    log.warning("upload worker failed: %s", e)
        for i, a in pending:
            nm = a.get("name") or f"file{i}"
            (added := added + 1) if results.get(nm) else failed.append(nm)

    # ---- changelog edited? re-post it (captions cannot be edited to fit) ----
    edited = 0
    old_body = entry.get("body") or ""
    if not added and not failed and old_body and old_body != body:
        for mid in text_ids:
            try:
                telegram.delete_message(route.get("chat_id"), mid)
            except Exception:
                pass
        text_ids = _post_changelog(rel, route, repo_name)
        edited = 1

    entry["sig"] = sig
    entry["body"] = body
    entry["done"] = done
    entry["msg_ids"] = [m for m in done.values() if m]
    entry["text_ids"] = text_ids
    entry["t"] = int(time.time())
    if failed:
        entry["failed"] = failed
    else:
        entry.pop("failed", None)
    st[key] = entry
    _save_rstate(st)
    try:
        db.flush(force=True)
    except Exception:
        pass
    try:
        import gc
        gc.collect()
    except Exception:
        pass

    if failed:
        return f"partial:{len(failed)}"
    if added:
        return "sent"
    if edited:
        return "edited"
    return "unchanged"


def _web_get(url, timeout=25):
    r = requests.get(url, headers=WEB_UA, timeout=timeout)
    if r.status_code == 404:
        raise RuntimeError("not found — check the repo name")
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.text


def _unescape_html(s):
    """HTML from the feed -> plain text that looks like the original.

    Closing tags used to add a newline on top of the newlines already in the
    source, so every line was followed by a blank line. Collapse all runs of
    blank lines to a single newline: the list then reads like GitHub shows it.
    """
    import html as _h
    txt = _h.unescape(s or "")
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.I)
    txt = re.sub(r"</p>|</li>|</h\d>|</div>", "\n", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = re.sub(r"[ \t]+\n", "\n", txt)          # trailing spaces
    txt = re.sub(r"\n{2,}", "\n", txt)              # no blank lines at all
    return txt.strip()


def _parse_size(text):
    # Strip the sha256 digest first: "...12502974be6af..." otherwise matches as
    # "12502974 B" and every size comes out wrong.
    t = re.sub(r"sha256:[0-9a-fA-F]{32,}", " ", text or "")
    m = re.search(r"([\d.]+)\s*(B|KB|MB|GB)\b", t, re.I)
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * _SIZE_UNITS[m.group(2).upper()])
    except Exception:
        return 0


def fetch_assets_web(repo_name, tag):
    """Files of one release, straight from the public assets panel."""
    url = f"https://github.com/{repo_name}/releases/expanded_assets/{tag}"
    html = _web_get(url)
    out = []
    # each asset row carries the download link and a size a little further along,
    # so measure from this link up to the NEXT one (not a fixed window)
    hits = list(re.finditer(r'href="(/[^"]+/releases/download/[^"]+)"', html))
    for i, m in enumerate(hits):
        href = m.group(1)
        name = href.rsplit("/", 1)[-1]
        end = hits[i + 1].start() if i + 1 < len(hits) else min(len(html), m.end() + 3000)
        row = re.sub(r"<[^>]+>", " ", html[m.end():end])
        size = _parse_size(row)
        out.append({
            "id": f"{tag}/{name}",
            "name": name,
            "size": size,
            "browser_download_url": "https://github.com" + href,
        })
    seen, uniq = set(), []
    for a in out:
        if a["name"] in seen:
            continue
        seen.add(a["name"])
        uniq.append(a)
    return uniq


def fetch_releases_web(repo_name, limit=10):
    """Releases from the public Atom feed — no API, no rate limit."""
    repo_name = normalize_repo(repo_name)
    xml = _web_get(f"https://github.com/{repo_name}/releases.atom")
    entries = re.findall(r"<entry>(.*?)</entry>", xml, re.S)
    out = []
    for e in entries[:limit]:
        tag_m = re.search(r"/releases/tag/([^\"]+)\"", e)
        tag = tag_m.group(1) if tag_m else ""
        if not tag:
            continue
        title = _unescape_html((re.search(r"<title>(.*?)</title>", e, re.S) or [None, tag])[1])
        content = (re.search(r"<content[^>]*>(.*?)</content>", e, re.S) or [None, ""])[1]
        body = _unescape_html(content)
        updated = (re.search(r"<updated>(.*?)</updated>", e) or [None, ""])[1]
        link = f"https://github.com/{repo_name}/releases/tag/{tag}"
        pre = bool(re.search(r"(alpha|beta|rc|preview|pre-?release|nightly|dev)",
                             f"{tag} {title}", re.I))
        try:
            assets = fetch_assets_web(repo_name, tag)
        except Exception as ex:
            log.info("assets for %s failed: %s", tag, ex)
            assets = []
        out.append({
            "id": f"{repo_name}@{tag}",
            "tag_name": tag,
            "name": title or tag,
            "body": body,
            "html_url": link,
            "draft": False,
            "prerelease": pre,
            "updated_at": updated,
            "assets": assets,
        })
    return out


def fetch_commits_web(repo_name, limit=10):
    """Recent commits from the public Atom feed (default branch)."""
    repo_name = normalize_repo(repo_name)
    xml = _web_get(f"https://github.com/{repo_name}/commits.atom")
    entries = re.findall(r"<entry>(.*?)</entry>", xml, re.S)
    out = []
    for e in entries[:limit]:
        link = (re.search(r'<link[^>]+href="([^"]+)"', e) or [None, ""])[1]
        title = _unescape_html((re.search(r"<title>(.*?)</title>", e, re.S) or [None, ""])[1])
        author = (re.search(r"<name>(.*?)</name>", e, re.S) or [None, "unknown"])[1]
        sha = link.rsplit("/", 1)[-1] if link else title[:7]
        out.append({"id": sha, "short": sha[:7], "message": title,
                    "author": _unescape_html(author), "url": link, "date": ""})
    return out


# ------------------------------------------- rebuild after a restart --------
# The state file can be wiped by a restart on hosts with ephemeral disks. Rather
# than re-posting everything, read what is ALREADY in the channel and treat those
# files as done. This needs the user account, because a bot cannot read history.
_TAG_RE = re.compile(r"Tag:\s*<code>([^<]+)</code>")
_URL_TAG_RE = re.compile(r"/releases/tag/([^/\s<]+)")


def _tag_of(caption):
    m = _TAG_RE.search(caption or "") or _URL_TAG_RE.search(caption or "")
    return m.group(1).strip() if m else ""


def rebuild_from_channel(limit=100):
    """Rebuild the posted-record from the channel's own messages.

    Returns (routes_checked, entries_rebuilt).
    """
    from . import userbot
    if not userbot.is_running():
        return 0, 0
    st = _rstate()
    checked = rebuilt = 0
    for route in routes():
        checked += 1
        try:
            msgs = userbot.run_sync(
                userbot.recent_documents(route.get("chat_id"), limit,
                                         route.get("thread_id")), timeout=180)
        except Exception as e:
            log.info("rebuild: could not read %s: %s", route.get("chat_id"), e)
            continue
        repo_name = str(route.get("repo") or "")
        # messages come newest-first; walk oldest-first and remember the last
        # release tag seen, so the follow-up files of that release are attributed
        # to it (their captions only carry the file name)
        current_tag = ""
        for m in reversed(msgs or []):
            tag = _tag_of(m.get("caption"))
            if tag:
                current_tag = tag
            elif not current_tag:
                continue
            tag = tag or current_tag
            if not tag:
                continue
            rid = f"{repo_name}@{tag}"
            key = _state_key(route, rid)
            ent = st.get(key) or {"sig": "", "body": "", "done": {}, "msg_ids": []}
            done = ent.get("done") or {}
            if m.get("name") and m["name"] not in done:
                done[m["name"]] = m.get("id")
            ent["done"] = done
            ent["msg_ids"] = [v for v in done.values() if v]
            ent.setdefault("t", int(time.time()))
            st[key] = ent
            rebuilt += 1
    if rebuilt:
        _save_rstate(st)
        try:
            db.flush(force=True)
        except Exception:
            pass
    return checked, rebuilt
