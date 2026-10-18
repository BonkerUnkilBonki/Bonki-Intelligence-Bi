"""Bonki-Intelligence - an intelligent Telegram group file agent.

FastAPI app: receives Telegram updates (webhook), runs the agent, and serves a
small no-auth dashboard to configure the brain, the groups, and the user account.
"""
import asyncio
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse)
from fastapi.templating import Jinja2Templates

from . import agent, db, handlers, indexer, llm, telegram, userbot
from .config import SECRET_KEYS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

TEMPLATES = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _mask(v):
    v = str(v or "")
    if not v:
        return ""
    return v[:4] + "…" + v[-2:] if len(v) > 8 else "•••"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    threading_start_userbot()
    await handlers.init_identity()
    _ensure_webhook_secret()
    if _use_polling():
        _disable_webhook()
        asyncio.create_task(_poll_loop())
        log.info("telegram mode: long polling (robust on hosts that sleep)")
    else:
        _set_webhook()
        log.info("telegram mode: webhook")
    _set_commands()
    asyncio.create_task(_auto_index_loop())
    try:
        st = db.storage_info()
        log.info("startup: storage=%s, groups=%d, files=%d, brain=%s, userbot=%s",
                 st["mode"], len(db.list_groups()), db.count_files(),
                 llm.describe(), "on" if userbot.configured() else "off")
        if st["mode"] == "local":
            log.warning("state is LOCAL-only - set GITHUB_TOKEN so groups/index survive "
                        "restarts and redeploys on Render's ephemeral disk")
    except Exception as e:
        log.warning("startup summary failed: %s", e)
    yield


def threading_start_userbot():
    try:
        asyncio.get_event_loop()
    except Exception:
        pass
    try:
        asyncio.create_task(userbot.run())
        log.info("userbot task started (configured=%s)", userbot.configured())
    except Exception as e:
        log.warning("could not start userbot: %s", e)


def _use_polling():
    """Long polling vs webhook. Explicit USE_POLLING wins; otherwise poll when we
    have no public URL to receive a webhook on."""
    v = (os.environ.get("USE_POLLING") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return not (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_BASE"))


def _ensure_webhook_secret():
    """Guarantee a webhook secret exists so the webhook works with zero config.
    Uses WEBHOOK_SECRET from the env, else generates and saves one."""
    cur = (db.get_settings().get("webhook_secret") or "").strip()
    if cur:
        return cur
    secret = (os.environ.get("WEBHOOK_SECRET") or "").strip() or secrets.token_urlsafe(24)
    db.set_settings({"webhook_secret": secret})
    db.flush(force=True)
    return secret


def _webhook_url():
    secret = db.get_settings().get("webhook_secret")
    base = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_BASE") or ""
    return f"{base.rstrip('/')}/telegram/webhook/{secret}" if (secret and base) else ""


def _set_webhook():
    url = _webhook_url()
    if not url:
        return
    try:
        telegram.set_webhook(url, secret_token=db.get_settings().get("webhook_secret"))
        log.info("telegram webhook set")
    except Exception as e:
        log.warning("could not set webhook: %s", e)


def _disable_webhook():
    try:
        telegram.delete_webhook()
        log.info("cleared any existing webhook (polling mode)")
    except Exception as e:
        log.warning("could not delete webhook: %s", e)


async def _auto_index_loop():
    """Background: index new files from every enabled group on a timer, so a
    manual scan is never needed. Live capture handles files the bot sees at once;
    this catches everything else (including files posted while it was asleep)."""
    await asyncio.sleep(30)
    while True:
        try:
            s = db.get_settings()
            if (str(s.get("auto_scan") or "1") == "1" and not db.is_paused()
                    and userbot.is_running()):
                res = await indexer.scan_all()
                added = sum(r.get("added", 0) for r in res)
                if added:
                    log.info("auto-index: %d new file(s)", added)
        except Exception as e:
            log.warning("auto-index failed: %s", e)
        try:
            interval = max(2.0, float(db.get_settings().get("scan_interval_minutes") or 10))
        except (TypeError, ValueError):
            interval = 10.0
        await asyncio.sleep(interval * 60)


async def _poll_loop():
    """Long-poll Telegram and dispatch updates. Telegram keeps undelivered
    updates for ~24h, so a sleeping free host catches up when it wakes.

    The blocking HTTP call runs in a thread so it never starves the event loop,
    and the last update id is remembered so a restart doesn't reprocess (and
    re-answer) old messages.
    """
    await asyncio.sleep(2)
    try:
        saved = db.get_settings().get("tg_offset")
        offset = int(saved) if saved else None
    except (TypeError, ValueError):
        offset = None
    while True:
        try:
            res = await asyncio.to_thread(telegram.get_updates, offset, 25)
            ups = res.get("result", [])
            for u in ups:
                offset = u.get("update_id", 0) + 1
                asyncio.create_task(handlers.handle_update(u))
            if ups:
                db.set_settings({"tg_offset": str(offset)})
                await asyncio.to_thread(db.flush, True)
        except Exception as e:
            log.warning("poll failed: %s", e)
            await asyncio.sleep(3)


def _set_commands():
    try:
        cmds = [
            {"command": "scan", "description": "Scan this group and index its files"},
            {"command": "find", "description": "Find a file, e.g. /find apple music apk"},
            {"command": "list", "description": "List indexed files"},
            {"command": "stats", "description": "Group file stats"},
            {"command": "help", "description": "What Bonki can do"},
        ]
        requests_ = __import__("requests")
        requests_.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/setMyCommands",
                       json={"commands": cmds}, timeout=15)
    except Exception:
        pass


app = FastAPI(title="Bonki-Intelligence", lifespan=lifespan)


@app.middleware("http")
async def _head_as_get(request, call_next):
    """Serve HEAD like GET.

    Render's health checker probes with HEAD, and a plain @app.get route answers
    HEAD with 405 here - which can make the deploy look unhealthy. Rewriting the
    method before routing lets every GET route answer HEAD with a 2xx.
    """
    if request.method == "HEAD":
        request.scope["method"] = "GET"
    return await call_next(request)


def _redir(msg, path="/"):
    try:
        db.flush(force=True)
    except Exception as e:
        log.warning("flush failed: %s", e)
    return RedirectResponse(url=f"{path}?msg={quote(msg)}", status_code=303)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


# ------------------------------------------------------------- dashboard ----
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, msg: str = ""):
    groups = db.list_groups()
    for g in groups:
        g["file_count"] = db.count_files(g.get("chat_id"))
        g["last_scan_str"] = (time.strftime("%Y-%m-%d %H:%M", time.localtime(g["last_scan"]))
                              if g.get("last_scan") else "never")
    return TEMPLATES.TemplateResponse(request, "index.html", {
        "groups": groups, "msg": msg, "total_files": db.count_files(),
        "storage": db.storage_info(), "brain": llm.describe(),
        "llm_on": llm.enabled(), "userbot_on": userbot.is_running(),
        "userbot_configured": userbot.configured(),
        "activity": [dict(a, time=time.strftime("%H:%M:%S", time.localtime(a["t"])))
                     for a in db.list_activity(30)],
        "paused": db.is_paused(),
    })


@app.post("/groups/add")
def group_add(chat_id: str = Form(...), title: str = Form("")):
    handle = userbot.clean_handle(chat_id)
    cid = handle if handle.lstrip("-").isdigit() else ("@" + handle if handle else "")
    if db.add_group(cid, title.strip() or cid, handle):
        return _redir(f"Added group {cid}. Click Scan to index its files.")
    return _redir(f"{cid} is already added.")


@app.post("/groups/{gid}/delete")
def group_delete(gid: int):
    row = db.get_group(gid)
    db.delete_group(gid)
    return _redir(f"Removed {row.get('title') if row else gid} and its index.")


@app.post("/groups/{gid}/toggle")
def group_toggle(gid: int):
    row = db.get_group(gid)
    if row:
        db.update_group(gid, {"enabled": 0 if row.get("enabled") else 1})
        return _redir(f"{row.get('title')} {'off' if row.get('enabled') else 'on'}.")
    return _redir("Group not found.")


@app.post("/groups/{gid}/scan")
async def group_scan(gid: int):
    row = db.get_group(gid)
    if not row:
        return _redir("Group not found.")
    if not userbot.is_running():
        return _redir("User account not connected - log in at /userbot to scan history.")
    asyncio.create_task(indexer.scan(row, full=True))
    return _redir(f"Scanning {row.get('title')}… refresh in a moment.")


@app.post("/scan-all")
async def scan_all():
    if not userbot.is_running():
        return _redir("User account not connected - log in at /userbot to scan history.")
    asyncio.create_task(indexer.scan_all(full=True))
    return _redir("Scanning all groups…")


@app.post("/groups/{gid}/clear")
def group_clear(gid: int):
    row = db.get_group(gid)
    if row:
        db.clear_files(row.get("chat_id"))
    return _redir("Index cleared for that group.")


# -------------------------------------------------------------- settings ----
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    s = db.get_settings()
    view = dict(s)
    for k in SECRET_KEYS:
        view[k] = _mask(s.get(k))
    view["_has"] = {k: bool(s.get(k)) for k in SECRET_KEYS}
    return TEMPLATES.TemplateResponse(request, "settings.html", {
        "s": view, "msg": msg, "brain": llm.describe(), "llm_on": llm.enabled(),
        "webhook_url": _webhook_url(), "storage": db.storage_info(),
        "userbot_configured": userbot.configured(), "userbot_on": userbot.is_running(),
    })


@app.post("/settings")
def settings_save(
    wake_words: str = Form("bonki"),
    allowed_groups: str = Form(""),
    llm_provider: str = Form("off"),
    gemini_model: str = Form("gemini-2.0-flash"),
    gemini_api_key: str = Form(""),
    openai_base_url: str = Form("https://api.openai.com/v1"),
    openai_model: str = Form("gpt-4o-mini"),
    openai_api_key: str = Form(""),
    scan_depth: str = Form("500"),
    scan_interval_minutes: str = Form("10"),
    index_extensions: str = Form("apk, zip"),
    reply_footer: str = Form("Bonki-Intelligence"),
    max_upload_mb: str = Form("50"),
    allow_user_ids: str = Form(""),
    webhook_secret: str = Form(""),
    tg_api_id: str = Form(""),
    tg_api_hash: str = Form(""),
    tg_session: str = Form(""),
    require_mention: str = Form(None),
    index_live: str = Form(None),
    auto_scan: str = Form(None),
    reply_with_file: str = Form(None),
    mention_requester: str = Form(None),
    add_hashtag: str = Form(None),
    persist_tags: str = Form(None),
):
    cur = db.get_settings()
    items = {
        "wake_words": wake_words.strip() or "bonki",
        "allowed_groups": allowed_groups.strip(),
        "llm_provider": llm_provider.strip() or "off",
        "gemini_model": gemini_model.strip() or "gemini-2.0-flash",
        "openai_base_url": openai_base_url.strip() or "https://api.openai.com/v1",
        "openai_model": openai_model.strip() or "gpt-4o-mini",
        "scan_depth": scan_depth.strip() or "500",
        "scan_interval_minutes": scan_interval_minutes.strip() or "10",
        "index_extensions": index_extensions.strip(),
        "reply_footer": reply_footer.strip(),
        "max_upload_mb": max_upload_mb.strip() or "50",
        "allow_user_ids": allow_user_ids.strip(),
        "webhook_secret": webhook_secret.strip(),
        "tg_api_id": tg_api_id.strip(),
        "require_mention": "1" if require_mention is not None else "0",
        "index_live": "1" if index_live is not None else "0",
        "auto_scan": "1" if auto_scan is not None else "0",
        "reply_with_file": "1" if reply_with_file is not None else "0",
        "mention_requester": "1" if mention_requester is not None else "0",
        "add_hashtag": "1" if add_hashtag is not None else "0",
        "persist_tags": "1" if persist_tags is not None else "0",
    }
    # secrets: only overwrite when a real (non-masked) value is submitted
    for key, val in (("gemini_api_key", gemini_api_key), ("openai_api_key", openai_api_key),
                     ("tg_api_hash", tg_api_hash), ("tg_session", tg_session)):
        val = (val or "").strip()
        if val and "…" not in val and "•••" not in val:
            items[key] = val
    db.set_settings(items)
    return _redir("Settings saved.", path="/settings")


@app.post("/llm/test")
def llm_test():
    if not llm.enabled():
        return _redir("No brain configured (set a provider + key).", path="/settings")
    out = llm.complete("Reply with exactly: Bonki brain online.", max_tokens=20)
    return _redir(f"Brain says: {out or 'no reply (check key/model)'}", path="/settings")


# --------------------------------------------------------------- userbot ----
@app.get("/userbot", response_class=HTMLResponse)
async def userbot_page(request: Request, msg: str = ""):
    me = await userbot.whoami() if userbot.is_running() else None
    return TEMPLATES.TemplateResponse(request, "userbot.html", {
        "s": db.get_settings(), "msg": msg, "configured": userbot.configured(),
        "running": userbot.is_running(),
        "last_error": db.get_settings().get("userbot_last_error") or "",
        "me": me,
    })


@app.post("/userbot/start")
async def userbot_start(api_id: str = Form(""), api_hash: str = Form(""), phone: str = Form("")):
    db.set_settings({"tg_api_id": api_id.strip(), "tg_api_hash": api_hash.strip()})
    try:
        await userbot.start_login(api_id.strip(), api_hash.strip(), phone.strip())
        return _redir("Code sent. Check Telegram and enter it below.", path="/userbot")
    except Exception as e:
        return _redir(f"Error: {e}", path="/userbot")


@app.post("/userbot/verify")
async def userbot_verify(code: str = Form(""), password: str = Form("")):
    try:
        await userbot.verify_login(code.strip(), password.strip() or None)
        return _redir("Logged in — session saved. Restart the app if it doesn't connect.", path="/userbot")
    except Exception as e:
        return _redir(f"Error: {e}", path="/userbot")


@app.post("/userbot/clear")
def userbot_clear():
    db.set_settings({"tg_session": ""})
    return _redir("Session cleared.", path="/userbot")


# ----------------------------------------------------------- diagnostics ----
@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics_page(request: Request, msg: str = ""):
    groups = db.list_groups()
    for g in groups:
        g["file_count"] = db.count_files(g.get("chat_id"))
    return TEMPLATES.TemplateResponse(request, "diagnostics.html", {
        "msg": msg, "storage": db.storage_info(), "brain": llm.describe(),
        "llm_on": llm.enabled(), "provider": llm.provider(),
        "userbot_configured": userbot.configured(), "userbot_running": userbot.is_running(),
        "userbot_error": db.get_settings().get("userbot_last_error") or "",
        "groups": groups, "total_files": db.count_files(),
        "webhook": _webhook_url(), "paused": db.is_paused(),
        "activity": [dict(a, time=time.strftime("%H:%M:%S", time.localtime(a["t"])))
                     for a in db.list_activity(40)],
    })


@app.post("/pause")
def pause_toggle():
    new = "0" if db.is_paused() else "1"
    db.set_settings({"paused": new})
    return _redir("PAUSED." if new == "1" else "Resumed.", path="/diagnostics")


@app.post("/diagnostics/scan")
async def diagnostics_scan():
    if not userbot.is_running():
        return _redir("User account not connected.", path="/diagnostics")
    asyncio.create_task(indexer.scan_all())
    return _redir("Scanning all groups…", path="/diagnostics")


# --------------------------------------------------------------- webhook ----
@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if not secret or secret != (db.get_settings().get("webhook_secret") or ""):
        return JSONResponse({"ok": False}, status_code=404)
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": True})
    asyncio.create_task(handlers.handle_update(update))
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ cron ----
@app.api_route("/cron/scan", methods=["GET", "POST"])
async def cron_scan():
    """Hit this on a schedule to keep the app awake and refresh the index."""
    try:
        if userbot.is_running():
            asyncio.create_task(indexer.scan_all())
        return JSONResponse({"ok": True, "groups": len(db.list_groups()),
                             "files": db.count_files(),
                             "userbot": "on" if userbot.is_running() else "off"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
