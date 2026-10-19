"""Durable state store for Bonki-Intelligence.

Keeps the settings, the list of groups, the *file index* (every document the bot
has scanned out of a group), tags and an activity log. Written to a local JSON
file on every change and synced to a remote store so it survives restarts:

  * a GitHub Gist (recommended)  - set GITHUB_TOKEN with the `gist` scope,
    optionally STATE_GIST_ID. Otherwise a private gist described
    "bonki-intel-state" is found or created automatically.
  * a file in one of your repos  - set STATE_REPO=owner/repo.

Without a token it still runs, but state is local-only (lost on redeploy) and the
dashboard warns you.
"""
import base64
import json
import logging
import os
import threading
import time

import requests

from .config import DEFAULT_SETTINGS

log = logging.getLogger("store")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_FILE = os.environ.get("STATE_FILE", os.path.join(_ROOT, "bonki-intel-state.json"))

GIST_DESC = "bonki-intel-state"
GIST_FILE = "bonki-intel-state.json"
API = "https://api.github.com"

_lock = threading.RLock()
_STATE = None
_REMOTE = None
_STORAGE_MODE = "local"
_STORAGE_NOTE = ""
_dirty = False
_last_remote = 0.0
REMOTE_MIN_INTERVAL = 5.0
_last_save = {"ok": None, "at": 0.0, "error": ""}


# --------------------------------------------------------------- helpers ---
def _default_state():
    return {"settings": {}, "groups": [], "files": [], "activity": []}


def _normalize(state):
    base = _default_state()
    if not isinstance(state, dict):
        return base
    for k in ("groups", "files", "activity"):
        if not isinstance(state.get(k), list):
            state[k] = []
    stored = state.get("settings")
    state["settings"] = ({k: str(v) for k, v in stored.items()}
                         if isinstance(stored, dict) else {})
    return state


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "bonki-intel",
    }


def _resolve_remote():
    global _STORAGE_NOTE
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        _STORAGE_NOTE = "No GITHUB_TOKEN - state is local-only and will be lost on restart."
        return None
    repo = os.environ.get("STATE_REPO")
    if repo:
        return {"kind": "repo", "token": tok, "repo": repo,
                "path": os.environ.get("STATE_PATH", GIST_FILE)}
    return {"kind": "gist", "token": tok, "id": os.environ.get("STATE_GIST_ID") or None,
            "file": GIST_FILE}


def _gist_id(cfg):
    if cfg.get("id"):
        return cfg["id"]
    h = _headers(cfg["token"])
    try:
        r = requests.get(f"{API}/gists", headers=h, params={"per_page": 100}, timeout=30)
        if r.status_code in (401, 403):
            log.warning("token lacks gist scope (list gists -> %s)", r.status_code)
            return None
        r.raise_for_status()
        for g in r.json():
            if g.get("description") == GIST_DESC:
                cfg["id"] = g["id"]
                return cfg["id"]
        r = requests.post(
            f"{API}/gists", headers=h, timeout=30,
            json={"description": GIST_DESC, "public": False,
                  "files": {GIST_FILE: {"content": "{}"}}},
        )
        r.raise_for_status()
        cfg["id"] = r.json()["id"]
        log.info("created state gist %s", cfg["id"])
        return cfg["id"]
    except Exception as e:
        log.warning("gist discovery failed: %s", e)
        return None


def _remote_load(cfg):
    h = _headers(cfg["token"])
    try:
        if cfg["kind"] == "gist":
            gid = _gist_id(cfg)
            if not gid:
                return None
            r = requests.get(f"{API}/gists/{gid}", headers=h, timeout=30)
            r.raise_for_status()
            files = r.json().get("files") or {}
            f = files.get(cfg["file"]) or next(iter(files.values()), None)
            if not f:
                return None
            content = f.get("content")
            if content is None and f.get("raw_url"):
                content = requests.get(f["raw_url"], timeout=30).text
            return json.loads(content) if content else None
        r = requests.get(f"{API}/repos/{cfg['repo']}/contents/{cfg['path']}",
                         headers=h, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        cfg["sha"] = data.get("sha")
        return json.loads(base64.b64decode(data["content"]).decode())
    except Exception as e:
        log.warning("remote load failed: %s", e)
        return None


def _remote_save(cfg, state):
    h = _headers(cfg["token"])
    body = json.dumps(state, separators=(",", ":"))
    try:
        if cfg["kind"] == "gist":
            gid = _gist_id(cfg)
            if not gid:
                return False
            r = requests.patch(f"{API}/gists/{gid}", headers=h, timeout=30,
                               json={"files": {cfg["file"]: {"content": body}}})
            r.raise_for_status()
            return True
        url = f"{API}/repos/{cfg['repo']}/contents/{cfg['path']}"
        if not cfg.get("sha"):
            r = requests.get(url, headers=h, timeout=30)
            if r.ok:
                cfg["sha"] = r.json().get("sha")
        payload = {"message": "bonki-intel state",
                   "content": base64.b64encode(body.encode()).decode()}
        if cfg.get("sha"):
            payload["sha"] = cfg["sha"]
        r = requests.put(url, headers=h, timeout=30, json=payload)
        r.raise_for_status()
        cfg["sha"] = r.json().get("content", {}).get("sha")
        return True
    except Exception as e:
        _last_save["error"] = str(e)
        log.warning("remote save failed: %s", e)
        return False


def _save_local():
    try:
        with open(LOCAL_FILE, "w") as f:
            json.dump(_STATE, f, separators=(",", ":"))
    except Exception as e:
        log.warning("local save failed: %s", e)


def _touch():
    global _dirty
    _dirty = True
    _save_local()


def flush(force=False):
    global _dirty, _last_remote
    with _lock:
        if not _dirty and not force:
            return
        if not _REMOTE:
            _dirty = False
            return
        now = time.time()
        if not force and now - _last_remote < REMOTE_MIN_INTERVAL:
            return
        ok = _remote_save(_REMOTE, _STATE)
        _last_save["ok"] = bool(ok)
        _last_save["at"] = time.time()
        if ok:
            _dirty = False
            _last_remote = now


def storage_info():
    return {"mode": _STORAGE_MODE, "note": _STORAGE_NOTE,
            "durable": _STORAGE_MODE in ("gist", "repo"),
            "last_save_ok": _last_save["ok"], "last_save_at": _last_save["at"],
            "last_save_error": _last_save["error"]}


# --------------------------------------------------------------- lifecycle -
def init_db():
    global _STATE, _REMOTE, _STORAGE_MODE, _STORAGE_NOTE, _dirty
    with _lock:
        _REMOTE = _resolve_remote()
        state = _remote_load(_REMOTE) if _REMOTE else None
        if state is not None and _REMOTE:
            _STORAGE_MODE = _REMOTE["kind"]
        if state is None:
            try:
                with open(LOCAL_FILE) as f:
                    state = json.load(f)
            except Exception:
                state = None
        if state is None:
            state = _default_state()
        _STATE = _normalize(state)
        _save_local()
        # one-time migration: an earlier version saved the old footer default
        # ("Bonki Intel"); a stored value overrides the new default, so move it
        # to the current brand.
        _st = _STATE.get("settings") or {}
        if str(_st.get("reply_footer", "")).strip() == "Bonki Intel":
            _st["reply_footer"] = "Bonki-Intelligence"
            _dirty = True
            _save_local()
            if _REMOTE:
                _remote_save(_REMOTE, _STATE)
                _dirty = False
            log.info("migrated footer: 'Bonki Intel' -> 'Bonki-Intelligence'")
        if _REMOTE and _STORAGE_MODE == "local":
            if _remote_save(_REMOTE, _STATE):
                _STORAGE_MODE = _REMOTE["kind"]
                _dirty = False
            else:
                _STORAGE_NOTE = "Remote store unreachable - using local file."
        if _STORAGE_MODE == "local" and not _STORAGE_NOTE:
            _STORAGE_NOTE = "State is local-only and will be lost on restart/redeploy."
        log.info("storage mode: %s", _STORAGE_MODE)


# --------------------------------------------------------------- settings ---
def get_settings():
    with _lock:
        s = dict(DEFAULT_SETTINGS)
        s.update(_STATE.get("settings") or {})
        return s


def get_raw_settings():
    with _lock:
        return dict(_STATE.get("settings") or {})


def set_settings(items):
    with _lock:
        _STATE.setdefault("settings", {}).update({k: str(v) for k, v in items.items()})
        _touch()


def is_paused():
    return str(get_settings().get("paused") or "0") == "1"


# ----------------------------------------------------------------- groups ---
def list_groups():
    with _lock:
        return [dict(g) for g in _STATE["groups"]]


def get_group(gid):
    with _lock:
        for g in _STATE["groups"]:
            if g.get("id") == gid:
                return dict(g)
    return None


def get_group_by_chat(chat_id):
    with _lock:
        for g in _STATE["groups"]:
            if str(g.get("chat_id")) == str(chat_id):
                return dict(g)
    return None


def add_group(chat_id, title="", handle=""):
    with _lock:
        if any(str(g.get("chat_id")) == str(chat_id) for g in _STATE["groups"]):
            return None
        row = {"id": max([g.get("id", 0) for g in _STATE["groups"]] or [0]) + 1,
               "chat_id": chat_id, "handle": handle, "title": title,
               "enabled": 1, "last_scan": None, "last_status": None}
        _STATE["groups"].append(row)
        _touch()
        return row["id"]


def update_group(gid, fields):
    allowed = {"chat_id", "handle", "title", "enabled", "last_scan", "last_status"}
    with _lock:
        for g in _STATE["groups"]:
            if g.get("id") == gid:
                for k, v in fields.items():
                    if k in allowed:
                        g[k] = v
                _touch()
                return


def delete_group(gid):
    with _lock:
        row = next((g for g in _STATE["groups"] if g.get("id") == gid), None)
        _STATE["groups"] = [g for g in _STATE["groups"] if g.get("id") != gid]
        if row:
            _STATE["files"] = [f for f in _STATE["files"]
                               if str(f.get("chat_id")) != str(row.get("chat_id"))]
        _touch()


# ------------------------------------------------------------- file index ---
def _file_key(chat_id, message_id):
    return (str(chat_id), str(message_id))


def upsert_file(chat_id, message_id, name, ext="", size=0, mime="", date=0,
                sender_id=None, sender_name="", caption="", tags=None, source="history"):
    """Add or refresh one file in the index. Keeps existing tags."""
    with _lock:
        cid, mid = _file_key(chat_id, message_id)
        for f in _STATE["files"]:
            if _file_key(f.get("chat_id"), f.get("message_id")) == (cid, mid):
                f.update({"name": name or f.get("name"), "ext": ext or f.get("ext"),
                          "size": size or f.get("size"), "mime": mime or f.get("mime"),
                          "date": date or f.get("date"),
                          "sender_id": sender_id if sender_id is not None else f.get("sender_id"),
                          "sender_name": sender_name or f.get("sender_name"),
                          "caption": caption or f.get("caption")})
                if tags:
                    f["tags"] = sorted(set((f.get("tags") or []) + list(tags)))
                _touch()
                return False
        _STATE["files"].append({
            "chat_id": chat_id, "message_id": message_id, "name": name or "file",
            "ext": (ext or "").lower(), "size": size or 0, "mime": mime or "",
            "date": date or 0, "sender_id": sender_id, "sender_name": sender_name,
            "caption": caption or "", "tags": sorted(set(tags or [])),
            "source": source, "added_at": time.time(),
        })
        _touch()
        return True


def list_files(chat_id=None, ext=None):
    with _lock:
        out = [dict(f) for f in _STATE["files"]]
    if chat_id is not None:
        out = [f for f in out if str(f.get("chat_id")) == str(chat_id)]
    if ext:
        exts = {e.strip().lower().lstrip(".") for e in str(ext).split(",") if e.strip()}
        out = [f for f in out if f.get("ext") in exts]
    return out


def get_file(chat_id, message_id):
    with _lock:
        cid, mid = _file_key(chat_id, message_id)
        for f in _STATE["files"]:
            if _file_key(f.get("chat_id"), f.get("message_id")) == (cid, mid):
                return dict(f)
    return None


def add_tag(chat_id, message_id, tag):
    with _lock:
        cid, mid = _file_key(chat_id, message_id)
        for f in _STATE["files"]:
            if _file_key(f.get("chat_id"), f.get("message_id")) == (cid, mid):
                f["tags"] = sorted(set((f.get("tags") or []) + [tag]))
                _touch()
                return True
    return False


def count_files(chat_id=None):
    return len(list_files(chat_id))


def clear_files(chat_id):
    with _lock:
        _STATE["files"] = [f for f in _STATE["files"]
                           if str(f.get("chat_id")) != str(chat_id)]
        _touch()


# --------------------------------------------------------------- activity ---
def log_activity(kind, chat_id, summary=""):
    with _lock:
        act = _STATE.setdefault("activity", [])
        act.append({"t": time.time(), "kind": str(kind), "chat_id": str(chat_id or ""),
                    "summary": str(summary or "")[:200]})
        del act[:-300]
        _touch()


def list_activity(limit=40):
    with _lock:
        return [dict(a) for a in (_STATE.get("activity") or [])[-limit:]][::-1]
