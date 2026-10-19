"""The user account (MTProto / Telethon) - Bonki's *eyes*.

A bot cannot read a group's message history (`getChatHistory` isn't available to
bots) and can only download files up to 20 MB. So Bonki uses a user account for
the heavy lifting:

  * walk a group's history and index the files in it   -> scan()
  * pull the actual bytes of a file so the bot can re-post it -> download()

The account must be a member of the group. Credentials come from Settings or the
env (TG_API_ID / TG_API_HASH / TG_SESSION). A web login at /userbot produces the
session string.
"""
import asyncio
import logging
import os
import re

from . import db

log = logging.getLogger("userbot")

_client = None
_pending = {}

# Telegram document attribute names that carry a file name
_FILE_ATTR = "DocumentAttributeFilename"


def clean_handle(text):
    """@name / t.me/name / t.me/c/123 / numeric id -> a resolvable handle.

    Accepts ints too: a private group has no @username, so its handle falls back
    to the numeric chat id, which arrives as an int.
    """
    t = str(text or "").strip()
    m = re.search(r"t\.me/c/(\d+)", t)
    if m:
        return "-100" + m.group(1)
    m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]+)", t)
    if m and m.group(1) != "c":
        return m.group(1)
    if t.startswith("@"):
        return t[1:]
    return t


def _cfg():
    s = db.get_settings()
    return {
        "api_id": (s.get("tg_api_id") or os.environ.get("TG_API_ID") or "").strip(),
        "api_hash": (s.get("tg_api_hash") or os.environ.get("TG_API_HASH") or "").strip(),
        "session": (s.get("tg_session") or os.environ.get("TG_SESSION") or "").strip(),
    }


def configured():
    c = _cfg()
    return bool(c["api_id"] and c["api_hash"] and c["session"])


def is_running():
    return _client is not None


def _file_name(msg):
    f = getattr(msg, "file", None)
    if f is not None and getattr(f, "name", None):
        return f.name
    doc = getattr(msg, "document", None)
    for a in (getattr(doc, "attributes", None) or []):
        fn = getattr(a, "file_name", None)
        if fn:
            return fn
    return ""


def _ext_of(name):
    return name.rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def _msg_doc(m):
    """Return a normalised dict for a message that carries a real file, else None."""
    doc = getattr(m, "document", None)
    if doc is None:
        return None
    # exclude video/audio/voice/GIF/stickers - those are documents to Telegram too
    for attr in ("video", "audio", "voice", "video_note", "gif", "round", "sticker"):
        if getattr(m, attr, None) is not None:
            return None
    name = _file_name(m) or f"file_{getattr(m, 'id', '')}"
    sender = getattr(m, "sender", None)
    sender_name = ""
    if sender is not None:
        sender_name = (getattr(sender, "first_name", None) or
                       getattr(sender, "title", None) or
                       getattr(sender, "username", None) or "")
    return {
        "message_id": getattr(m, "id", None),
        "name": name,
        "ext": _ext_of(name),
        "size": getattr(doc, "size", 0) or 0,
        "mime": getattr(doc, "mime_type", "") or "",
        "date": int(getattr(m, "date", None).timestamp()) if getattr(m, "date", None) else 0,
        "sender_id": getattr(m, "sender_id", None),
        "sender_name": sender_name,
        "caption": getattr(m, "message", None) or "",
    }


def _keeps(ext, exts):
    if not exts:
        return True
    return (ext or "").lower() in exts


async def _entity(handle):
    h = clean_handle(handle)
    if re.fullmatch(r"-?\d+", h):          # a numeric id -> hand Telethon an int
        h = int(h)
    return await _client.get_entity(h)


async def scan(handle, limit=500, exts=None, min_id=0):
    """Walk a group's history and return every matching file as a dict.

    `min_id` makes it incremental: only messages newer than that id are fetched,
    so the periodic auto-scan is cheap after the first pass.
    """
    if _client is None:
        raise RuntimeError("userbot is not connected")
    entity = await _entity(handle)
    found, offset_id = [], 0
    while len(found) < limit:
        batch = await _client.get_messages(entity, limit=min(100, limit - len(found)),
                                           offset_id=offset_id, min_id=int(min_id or 0))
        if not batch:
            break
        for m in batch:
            d = _msg_doc(m)
            if d and _keeps(d["ext"], exts):
                found.append(d)
        ids = [m.id for m in batch if getattr(m, "id", None)]
        if not ids:
            break
        offset_id = min(ids)
    return found


async def download(handle, message_id, dest_path):
    """Download one message's file to dest_path. Returns the path, or None."""
    if _client is None:
        raise RuntimeError("userbot is not connected")
    entity = await _entity(handle)
    msg = await _client.get_messages(entity, ids=int(message_id))
    if msg is None or getattr(msg, "document", None) is None:
        return None
    path = await _client.download_media(msg, file=dest_path)
    return path


async def _resolve_dest(dest_chat):
    """Turn a stored destination (@name / -100… id) into something Telethon can
    send to. Falls back to the raw value if it can't be resolved."""
    if dest_chat is None:
        return dest_chat
    try:
        h = clean_handle(dest_chat)
        if re.fullmatch(r"-?\d+", h):
            return await _client.get_entity(int(h))
        return await _client.get_entity(h)
    except Exception as e:
        log.warning("couldn't resolve destination %r: %s", dest_chat, e)
        return dest_chat


async def forward(handle, message_id, dest_chat, reply_to=None, caption=None):
    """Fallback for files too big for the bot to upload: forward from the account."""
    if _client is None:
        raise RuntimeError("userbot is not connected")
    entity = await _entity(handle)
    msg = await _client.get_messages(entity, ids=int(message_id))
    if msg is None:
        return None
    dest = await _resolve_dest(dest_chat)
    kwargs = {"drop_author": True}
    if reply_to:
        kwargs["reply_to"] = int(reply_to)
    res = await _client.forward_messages(dest, msg, **kwargs)
    new = res[0] if isinstance(res, (list, tuple)) and res else res
    if caption and new is not None:
        try:
            await _client.edit_message(dest, new, text=caption)
        except Exception as e:
            log.warning("couldn't set caption on forwarded file: %s", e)
    return new


async def list_dialogs(limit=200):
    """Groups/channels the account is a member of - for the dashboard picker."""
    if _client is None:
        return []
    out = []
    try:
        async for d in _client.iter_dialogs(limit=limit):
            if d.is_group or d.is_channel:
                out.append({"id": d.id, "title": d.name,
                            "username": getattr(d.entity, "username", None),
                            "kind": "channel" if d.is_channel else "group"})
    except Exception as e:
        log.warning("list_dialogs failed: %s", e)
    return out


async def whoami():
    if _client is None:
        return None
    try:
        me = await _client.get_me()
        return {"id": me.id, "username": getattr(me, "username", None),
                "name": " ".join(x for x in [me.first_name, me.last_name] if x)}
    except Exception:
        return None


# ------------------------------------------------------------------ login ---
async def start_login(api_id, api_hash, phone):
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except Exception:
        raise RuntimeError("telethon isn't installed on the server")
    if not (api_id and api_hash and phone):
        raise RuntimeError("api_id, api_hash and phone are all required")
    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.connect()
    await client.send_code_request(phone)
    _pending["client"] = client
    _pending["phone"] = phone
    return True


async def verify_login(code, password=None):
    client = _pending.get("client")
    if client is None:
        raise RuntimeError("no login in progress - send the code again")
    try:
        from telethon.errors import SessionPasswordNeededError
    except Exception:
        SessionPasswordNeededError = RuntimeError
    try:
        await client.sign_in(phone=_pending.get("phone"), code=code)
    except SessionPasswordNeededError:
        if not password:
            raise RuntimeError("two-step password required - enter it too")
        await client.sign_in(password=password)
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {e}")
    session = client.session.save()
    db.set_settings({"tg_session": session})
    db.flush(force=True)
    try:
        await client.disconnect()
    except Exception:
        pass
    _pending.clear()
    return session


async def run():
    """Background task: keep a connected client alive for scanning."""
    global _client
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except Exception as e:
        log.warning("telethon not installed - userbot disabled (%s)", e)
        return
    while True:
        c = _cfg()
        if not (c["api_id"] and c["api_hash"] and c["session"]):
            await asyncio.sleep(60)
            continue
        try:
            client = TelegramClient(StringSession(c["session"]), int(c["api_id"]), c["api_hash"])
            await client.start()
            _client = client
            me = await client.get_me()
            log.info("userbot connected as @%s", getattr(me, "username", None) or me.id)
            db.set_settings({"userbot_last_error": ""})
            db.flush(force=True)
            break
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            log.warning("userbot login failed, retrying in 60s: %s", msg)
            db.set_settings({"userbot_last_error": msg})
            db.flush(force=True)
            await asyncio.sleep(60)
    while True:                       # stay connected
        try:
            if not _client.is_connected():
                await _client.connect()
        except Exception:
            pass
        await asyncio.sleep(60)
