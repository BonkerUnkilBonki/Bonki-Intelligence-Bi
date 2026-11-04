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
import json
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
    kind = "🟠 Pre-Release" if rel.get("prerelease") else "🟢 Stable"
    out = (f"🚀 <b>New release</b>: {telegram.esc(title)}\n"
           f"Tag: <code>{telegram.esc(tag)}</code> · {kind}")
    if rel.get("html_url"):
        out += f"\n{rel['html_url']}"
    return out


CAPTION_LIMIT = 1024        # Telegram's hard limit for a file caption


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
    apk = pick_apk(rel.get("assets"))

    # With an APK, everything goes in ONE post: the file with the changelog as its
    # caption. A separate text message first is what made two posts.
    if not apk:
        try:
            telegram.send_message(f"{header}\n\n{body}" if body else header,
                                  chat_id=target)
        except Exception as e:
            db.set_settings({"gitly_last_error": f"posting to {target}: {str(e)[:160]}"})
            log.warning("gitly changelog failed: %s", e)
            return "post-failed"
        return "no-apk"

    size = int(apk.get("size") or 0)
    name = apk.get("name") or "release.apk"
    if size > UPLOAD_LIMIT:
        mb = size / (1024 * 1024)
        try:
            telegram.send_message(
                build_caption(rel) +
                f"\n\n⚠️ <b>{telegram.esc(name)}</b> is {mb:.1f} MB — over Telegram's "
                f"50 MB bot limit, so it can't be attached.\n"
                f"Download: {apk.get('browser_download_url', '')}",
                chat_id=target)
        except Exception as e:
            log.warning("gitly oversize notice failed: %s", e)
        return "too-big"

    caption = build_caption(rel)
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
    await asyncio.sleep(45)
    while True:
        try:
            if enabled() and routes():
                n, status = await check_routes()
                if n:
                    log.info("leech posted %d release(s)", n)
                elif status not in ("ok", "no-routes"):
                    log.info("leech check: %s", status)
            elif enabled() and channel():
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
    with tempfile.TemporaryDirectory() as tmp:
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
    rows = routes()
    keep = []
    for r in rows:
        if repo_name and str(r.get("repo", "")).lower() == repo_name.lower() and \
                (chat_id is None or str(r.get("chat_id")) == str(chat_id)):
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

    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, name)
        try:
            download_asset(apk, dest)
            telegram.send_document(dest, name, caption, chat_id, thread_id=thread)
            return "sent"
        except Exception as e:
            log.warning("leech apk failed: %s", e)
            return "apk-failed"


async def check_routes(force=False):
    """Post new activity for every configured route, grouped by repo and event."""
    rows = routes()
    if not rows:
        return 0, "no-routes"
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
                            st = await asyncio.to_thread(post_release_to, item, route)
                            ok = st in ("sent", "too-big", "no-apk")
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


async def cmd_leech(chat, msg, user, args):
    """Configure the leech bot: which repo, and which topic it feeds."""
    from . import approval, modes, rose
    cid = chat.get("id")
    parts = (args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    owner = approval.is_owner((user or {}).get("id"))
    admin = owner or await rose._is_admin(cid, (user or {}).get("id"))
    if not admin:
        return await rose._reply(chat, msg, "Admins only.")

    if sub in ("repo", "watch"):
        if "/" not in rest:
            return await rose._reply(chat, msg,
                                     "Usage: <code>/leech repo owner/name</code>")
        name = rest.strip().strip("/")
        try:
            rels = await asyncio.to_thread(fetch_releases, name, 3)
            n = len(rels or [])
        except Exception as e:
            return await rose._reply(chat, msg, f"⚠️ {telegram.esc(str(e))}")
        db.set_settings({"leech_repo": name, "gitly_on": "1"})
        await asyncio.to_thread(db.flush, True)
        modes.set_mode(cid, modes.LEECH)
        return await rose._reply(
            chat, msg,
            f"📦 Watching <code>{telegram.esc(name)}</code> — {n} release(s) visible.\n\n"
            f"Now open the <b>topic</b> you want releases in and send "
            f"<code>/leech here</code>.")

    if sub in ("here", "topic"):
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
        where = (f"topic <code>{thread}</code>" if is_topic
                 else "the main channel (not a topic)")
        return await rose._reply(
            chat, msg,
            ("✅ " if added else "♻️ Already set: ") +
            f"<b>{EVENTS[want]}</b> from <code>{telegram.esc(repo_name)}</code> "
            f"will be posted to {where}.\n\n"
            f"<i>Run this inside each topic you want a different kind of update in — "
            f"releases in one topic, commits in another.</i>")

    if sub in ("remove", "rm", "stop"):
        n = remove_route(rest or None, cid if not rest else None)
        await asyncio.to_thread(db.flush, True)
        return await rose._reply(chat, msg,
                                 f"🗑 Removed {n} route(s)." if n else "Nothing to remove.")

    if sub in ("on", "off"):
        db.set_settings({"gitly_on": "1" if sub == "on" else "0"})
        await asyncio.to_thread(db.flush, True)
        return await rose._reply(chat, msg,
                                 "✅ Leeching on." if sub == "on" else "⏹ Leeching off.")

    # status
    rows = routes()
    lines = ["📦 <b>Bonki Leech</b>", ""]
    cur = (db.get_settings().get("leech_repo") or "").strip()
    lines.append(f"• repo being configured: "
                 f"{'<code>' + telegram.esc(cur) + '</code>' if cur else 'none'}")
    lines.append(f"• watcher: {'✅ on' if enabled() else '⏹ off'}")
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
