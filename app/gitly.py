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
DEFAULT_REPO = "SwitchToMonk/Gilty"
UPLOAD_LIMIT = 50 * 1024 * 1024        # Telegram bot upload ceiling
POLL_SECONDS = 600
MAX_SEEN = 100


# ------------------------------------------------------------------ config --
def repo():
    return (db.get_settings().get("gitly_repo") or DEFAULT_REPO).strip()


def channel():
    return (db.get_settings().get("gitly_channel") or "").strip()


def enabled():
    return str(db.get_settings().get("gitly_on") or "0") == "1"


def include_prereleases():
    return str(db.get_settings().get("gitly_prerelease") or "1") == "1"


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
        log.warning("gitly changelog failed: %s", e)

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
        rels = await asyncio.to_thread(fetch_releases)
    except Exception as e:
        db.set_settings({"gitly_last_error": str(e)[:200]})
        return 0, str(e)
    if rels is None:
        db.set_settings({"gitly_last_check": str(int(time.time())),
                         "gitly_last_error": ""})
        return 0, "no-change"

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
        await asyncio.sleep(POLL_SECONDS)


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
            return await rose._reply(
                chat, msg,
                "✅ Release watcher on — checking every 10 minutes."
                if sub == "on" else "⏹ Release watcher off.")
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
        f"• last check: {last_s}",
    ]
    if err:
        lines.append(f"• ⚠️ last error: {telegram.esc(err[:120])}")
    lines += ["",
              "<code>/gitly channel @mychannel</code> — where to post",
              "<code>/gitly repo owner/name</code> — what to watch",
              "<code>/gitly on</code> · <code>/gitly check</code>",
              "<i>A public repo needs no token — one is only useful if the host's "
              "IP is shared, or the repo is private.</i>",
              "<i>Posts the changelog and attaches the .apk. Files over 50 MB "
              "are announced with a link — Telegram's bot limit.</i>"]
    await rose._reply(chat, msg, "\n".join(lines))
