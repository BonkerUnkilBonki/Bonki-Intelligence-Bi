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
_save_lock = threading.Lock()   # serialises remote writes only
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
    return {"settings": {}, "groups": [], "files": [], "activity": [], "feedback": [],
            "approvals": [], "notes": [], "warns": [], "chatconf": [], "stickerpacks": [],
            "feds": [], "ranker_rows": []}


def _normalize(state):
    base = _default_state()
    if not isinstance(state, dict):
        return base
    for k in ("groups", "files", "activity", "feedback", "approvals",
              "notes", "warns", "chatconf", "stickerpacks", "feds", "ranker_rows"):
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
    return _remote_save_body(cfg, json.dumps(state, separators=(",", ":")))


def _remote_save_body(cfg, body):
    h = _headers(cfg["token"])
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


def _save_local_body(body):
    try:
        with open(LOCAL_FILE, "w") as f:
            f.write(body)
    except Exception as e:
        log.warning("local save failed: %s", e)


def _save_local():
    _save_local_body(json.dumps(_STATE, separators=(",", ":")))


_last_local = 0.0
LOCAL_MIN_INTERVAL = 2.0        # seconds between full-state disk writes


def _touch():
    """Mark the state dirty.

    The full-state file write is DEBOUNCED. Writing the whole JSON on every
    change made indexing crawl - a 500-file scan meant 500 full rewrites.
    """
    global _dirty, _last_local
    _dirty = True
    now = time.time()
    if now - _last_local >= LOCAL_MIN_INTERVAL:
        _last_local = now
        _save_local()


def flush(force=False):
    """Push state to the durable store.

    Serialisation happens under the state lock, but the *network* write happens
    OUTSIDE it - otherwise every read/write in the bot blocked behind a slow
    gist/HTTP call, which is what made the bot laggy and unresponsive.
    """
    global _dirty, _last_remote
    with _lock:
        if not _dirty and not force:
            return
        if not _REMOTE:
            _dirty = False
            body = json.dumps(_STATE, separators=(",", ":"))
        else:
            now = time.time()
            if not force and now - _last_remote < REMOTE_MIN_INTERVAL:
                return
            body = json.dumps(_STATE, separators=(",", ":"))
            _last_remote = now
    _save_local_body(body)                          # cheap, off the lock
    if not _REMOTE:
        return
    with _save_lock:
        ok = _remote_save_body(_REMOTE, body)       # network, off the state lock
    with _lock:
        _last_save["ok"] = bool(ok)
        _last_save["at"] = time.time()
        if ok:
            _dirty = False


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
        # groups that existed before approval was introduced are grandfathered in,
        # so turning the gate on doesn't disable groups the owner already trusts.
        for _g in _STATE["groups"]:
            if "approved" not in _g:
                _g["approved"] = 1
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


def _norm_key(v):
    return str(v or "").strip().lstrip("@").lower()


def get_group_by_chat(chat_id, username=None):
    """Find a group by its numeric id OR its @username/handle.

    A group can be stored under either form (the dashboard may save @name while
    messages arrive with the -100… id), and both must resolve to the same row -
    otherwise the approval check misses it and the bot goes silent.
    """
    keys = {_norm_key(chat_id)}
    if username:
        keys.add(_norm_key(username))
    with _lock:
        for g in _STATE["groups"]:
            gid = _norm_key(g.get("chat_id"))
            gh = _norm_key(g.get("handle"))
            if gid in keys or (gh and gh in keys):
                return dict(g)
    return None


def add_group(chat_id, title="", handle="", approved=0):
    with _lock:
        if get_group_by_chat(chat_id, handle):      # already known under either form
            return None
        row = {"id": max([g.get("id", 0) for g in _STATE["groups"]] or [0]) + 1,
               "chat_id": chat_id, "handle": handle, "title": title,
               "enabled": 1, "approved": int(approved), "approval_notified": 0,
               "last_scan": None, "last_status": None}
        _STATE["groups"].append(row)
        _touch()
        return row["id"]


def is_approved(chat_id, username=None):
    """A group only scans/indexes/answers once the owner has approved it."""
    g = get_group_by_chat(chat_id, username)
    return bool(g and g.get("approved"))


def update_group(gid, fields):
    allowed = {"chat_id", "handle", "title", "enabled", "approved",
               "approval_notified", "last_scan", "last_status", "chat_type"}
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
    """Cheap count - does NOT copy the list (used on hot paths)."""
    with _lock:
        if chat_id is None:
            return len(_STATE["files"])
        return sum(1 for f in _STATE["files"]
                   if str(f.get("chat_id")) == str(chat_id))


def clear_files(chat_id):
    with _lock:
        _STATE["files"] = [f for f in _STATE["files"]
                           if str(f.get("chat_id")) != str(chat_id)]
        _touch()


# --------------------------------------------------------------- feedback ---
def add_feedback(chat_id, query, file_name, message_id, positive=True, source="button"):
    """Record one search outcome the bot can learn from."""
    with _lock:
        fb = _STATE.setdefault("feedback", [])
        fb.append({"t": time.time(), "chat_id": str(chat_id or ""),
                   "query": str(query or "")[:200], "file": str(file_name or "")[:200],
                   "message_id": message_id, "positive": bool(positive),
                   "source": str(source)})
        del fb[:-2000]          # keep the store bounded
        _touch()


def list_feedback(chat_id=None, limit=0):
    with _lock:
        out = [dict(f) for f in (_STATE.get("feedback") or [])]
    if chat_id is not None:
        out = [f for f in out if str(f.get("chat_id")) == str(chat_id)]
    return out[-limit:] if limit else out


def count_feedback(chat_id=None):
    with _lock:
        fb = _STATE.get("feedback") or []
        if chat_id is None:
            return len(fb)
        return sum(1 for f in fb if str(f.get("chat_id")) == str(chat_id))


def clear_feedback(chat_id=None):
    with _lock:
        if chat_id is None:
            _STATE["feedback"] = []
        else:
            _STATE["feedback"] = [f for f in (_STATE.get("feedback") or [])
                                  if str(f.get("chat_id")) != str(chat_id)]
        _touch()


# ------------------------------------------------- notes / warns / config --
def save_note(chat_id, name, content, by=""):
    with _lock:
        notes = _STATE.setdefault("notes", [])
        for n in notes:
            if str(n.get("chat_id")) == str(chat_id) and str(n.get("name", "")).lower() == name.lower():
                n.update({"content": content, "by": str(by), "t": time.time()})
                _touch()
                return
        notes.append({"chat_id": str(chat_id), "name": name, "content": content,
                      "by": str(by), "t": time.time()})
        del notes[:-500]
        _touch()


def get_note(chat_id, name):
    with _lock:
        for n in _STATE.get("notes", []):
            if str(n.get("chat_id")) == str(chat_id) and str(n.get("name", "")).lower() == name.lower():
                return dict(n)
    return None


def delete_note(chat_id, name):
    with _lock:
        before = len(_STATE.get("notes", []))
        _STATE["notes"] = [n for n in _STATE.get("notes", [])
                           if not (str(n.get("chat_id")) == str(chat_id)
                                   and str(n.get("name", "")).lower() == name.lower())]
        if len(_STATE["notes"]) != before:
            _touch()
            return True
    return False


def list_notes(chat_id):
    with _lock:
        return [n["name"] for n in _STATE.get("notes", [])
                if str(n.get("chat_id")) == str(chat_id)]


def add_warn(chat_id, user, reason, by=""):
    with _lock:
        w = _STATE.setdefault("warns", [])
        w.append({"chat_id": str(chat_id), "user": str(user), "reason": str(reason or "")[:200],
                  "by": str(by), "t": time.time()})
        _touch()
        return sum(1 for x in w if str(x.get("chat_id")) == str(chat_id)
                   and str(x.get("user")) == str(user))


def list_warns(chat_id, user=None):
    with _lock:
        return [dict(x) for x in _STATE.get("warns", [])
                if str(x.get("chat_id")) == str(chat_id)
                and (user is None or str(x.get("user")) == str(user))]


def reset_warns(chat_id, user):
    with _lock:
        _STATE["warns"] = [x for x in _STATE.get("warns", [])
                           if not (str(x.get("chat_id")) == str(chat_id)
                                   and str(x.get("user")) == str(user))]
        _touch()


def get_chatconf(chat_id):
    with _lock:
        for c in _STATE.get("chatconf", []):
            if str(c.get("chat_id")) == str(chat_id):
                return dict(c)
    return {}


def set_chatconf(chat_id, fields):
    with _lock:
        cc = _STATE.setdefault("chatconf", [])
        for c in cc:
            if str(c.get("chat_id")) == str(chat_id):
                c.update(fields)
                _touch()
                return
        row = {"chat_id": str(chat_id)}
        row.update(fields)
        cc.append(row)
        _touch()


# ------------------------------------------------------------ federations --
def list_feds():
    with _lock:
        return [dict(f) for f in _STATE.get("feds", [])]


def get_fed(fed_id):
    with _lock:
        for f in _STATE.get("feds", []):
            if str(f.get("id")) == str(fed_id):
                return dict(f)
    return None


def get_fed_by_name(name):
    with _lock:
        for f in _STATE.get("feds", []):
            if str(f.get("name", "")).lower() == str(name or "").lower():
                return dict(f)
    return None


def get_fed_for_chat(chat_id):
    with _lock:
        for f in _STATE.get("feds", []):
            if str(chat_id) in [str(g) for g in (f.get("groups") or [])]:
                return dict(f)
    return None


def add_fed(name, owner):
    with _lock:
        feds = _STATE.setdefault("feds", [])
        if any(str(f.get("name", "")).lower() == str(name).lower() for f in feds):
            return None
        row = {"id": max([f.get("id", 0) for f in feds] or [0]) + 1,
               "name": name, "owner": str(owner), "admins": [str(owner)],
               "groups": [], "bans": [], "t": time.time()}
        feds.append(row)
        _touch()
        return dict(row)


def update_fed(fed_id, fields):
    with _lock:
        for f in _STATE.get("feds", []):
            if str(f.get("id")) == str(fed_id):
                f.update(fields)
                _touch()
                return


def delete_fed(fed_id):
    with _lock:
        before = len(_STATE.get("feds", []))
        _STATE["feds"] = [f for f in _STATE.get("feds", [])
                          if str(f.get("id")) != str(fed_id)]
        if len(_STATE["feds"]) != before:
            _touch()
            return True
    return False


def fed_ban(fed_id, user, reason, by):
    with _lock:
        for f in _STATE.get("feds", []):
            if str(f.get("id")) != str(fed_id):
                continue
            bans = f.setdefault("bans", [])
            for b in bans:
                if str(b.get("user")) == str(user):
                    b.update({"reason": reason, "by": str(by), "t": time.time()})
                    _touch()
                    return
            bans.append({"user": str(user), "reason": reason[:200],
                         "by": str(by), "t": time.time()})
            _touch()
            return


def fed_unban(fed_id, user):
    with _lock:
        for f in _STATE.get("feds", []):
            if str(f.get("id")) != str(fed_id):
                continue
            before = len(f.get("bans") or [])
            f["bans"] = [b for b in (f.get("bans") or [])
                         if str(b.get("user")) != str(user)]
            if len(f["bans"]) != before:
                _touch()
                return True
    return False


def fed_is_banned(fed_id, user):
    key = str(user or "").lower()
    with _lock:
        for f in _STATE.get("feds", []):
            if str(f.get("id")) != str(fed_id):
                continue
            return any(str(b.get("user") or "").lower() == key
                       for b in (f.get("bans") or []))
    return False



# ---------------------------------------------------------------- ranker ----
def add_ranker_row(features, label):
    """One training example for the learned re-ranker."""
    with _lock:
        rows = _STATE.setdefault("ranker_rows", [])
        rows.append({"f": [float(x) for x in features], "y": 1 if label else 0,
                     "t": time.time()})
        if len(rows) > 5000:                 # keep the newest
            del rows[:-5000]
        _touch()


def list_ranker_rows():
    with _lock:
        return [dict(r) for r in _STATE.get("ranker_rows", [])]


def clear_ranker_rows():
    with _lock:
        _STATE["ranker_rows"] = []
        _touch()


# ------------------------------------------------------------ stickerpacks --
def get_stickerpack(user_id):
    with _lock:
        for p in _STATE.get("stickerpacks", []):
            if str(p.get("user_id")) == str(user_id):
                return dict(p)
    return None


def set_stickerpack(user_id, name, title, count):
    with _lock:
        packs = _STATE.setdefault("stickerpacks", [])
        for p in packs:
            if str(p.get("user_id")) == str(user_id):
                p.update({"name": name, "title": title, "count": int(count)})
                _touch()
                return
        packs.append({"user_id": str(user_id), "name": name, "title": title,
                      "count": int(count), "t": time.time()})
        _touch()


# -------------------------------------------------------------- approvals ---
def log_approval(chat_id, title, username, action, by=""):
    """Append to the approval / request history (who asked, which channel, what
    happened). Shown on the dashboard with a link to the channel."""
    with _lock:
        log = _STATE.setdefault("approvals", [])
        log.append({"t": time.time(), "chat_id": str(chat_id or ""),
                    "title": str(title or "")[:120], "username": str(username or ""),
                    "action": str(action), "by": str(by or "")})
        del log[:-300]
        _touch()


def list_approvals(limit=100):
    with _lock:
        return [dict(a) for a in (_STATE.get("approvals") or [])[-limit:]][::-1]


def clear_approvals():
    with _lock:
        _STATE["approvals"] = []
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
