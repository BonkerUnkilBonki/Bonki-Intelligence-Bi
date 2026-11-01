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
import time

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


async def ask(target, command, wait=8.0):
    """Send `command` to `target` as the user account and return the reply text.

    This is how Bonki can drive another bot (Rose) - by using the connected user
    account exactly as a person would. It is automation of your own account, not
    an API: the other bot's replies are plain text and are relayed as-is.
    """
    client = _client
    if client is None:
        raise RuntimeError("user account is not connected - use /userbot first")
    ent = await _entity(target)
    before = await client.get_messages(ent, limit=1)
    last_id = before[0].id if before else 0
    await client.send_message(ent, command)
    deadline = time.time() + wait
    while time.time() < deadline:
        await asyncio.sleep(1.2)
        msgs = await client.get_messages(ent, limit=6)
        new = [m for m in msgs if m.id > last_id and not m.out]
        if new:
            return "\n".join((m.message or "") for m in reversed(new)).strip()
    return ""


async def ask_document(target, command, wait=15.0):
    """Send a command, then return (text, document_bytes|None) from the reply.

    Rose answers /fbanlist with a FILE, so this also fetches the newest document
    in the conversation after the command is sent.
    """
    client = _client
    if client is None:
        raise RuntimeError("user account is not connected - use /userbot first")
    ent = await _entity(target)
    before = await client.get_messages(ent, limit=1)
    last_id = before[0].id if before else 0
    await client.send_message(ent, command)
    deadline = time.time() + wait
    texts, blob = [], None
    while time.time() < deadline:
        await asyncio.sleep(1.5)
        msgs = await client.get_messages(ent, limit=8)
        new = [m for m in msgs if m.id > last_id and not m.out]
        if new:
            texts = [(m.message or "") for m in reversed(new) if m.message]
            for m in reversed(new):
                if m.document:
                    try:
                        blob = await client.download_media(m, bytes)
                    except Exception as e:
                        log.info("document download failed: %s", e)
            if texts or blob:
                break
    return ("\n".join(texts).strip(), blob)


async def list_participants(handle, limit=1000):
    """Every member of a chat, via the user account.

    This is the ONLY way to enumerate members: the Bot API has no member-list
    method at all. Returns [{"id","name","username","bot"}].
    """
    client = _client
    if client is None:
        raise RuntimeError("user account is not connected - use /userbot first")
    ent = await _entity(handle)
    out = []
    async for user in client.iter_participants(ent, limit=limit):
        name = " ".join(x for x in [getattr(user, "first_name", None),
                                    getattr(user, "last_name", None)] if x)
        out.append({"id": user.id,
                    "name": name or getattr(user, "username", None) or str(user.id),
                    "username": getattr(user, "username", None),
                    "bot": bool(getattr(user, "bot", False))})
    return out


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
def _delivery_name(sent_type):
    """Human name for where Telegram says it sent the code."""
    n = type(sent_type).__name__ if sent_type is not None else ""
    return {
        "SentCodeTypeApp": "inside the Telegram app",
        "SentCodeTypeSms": "by SMS",
        "SentCodeTypeCall": "by phone call",
        "SentCodeTypeFlashCall": "by flash call",
        "SentCodeTypeMissedCall": "by missed call",
        "SentCodeTypeEmailCode": "by email",
        "SentCodeTypeFragmentSms": "by Fragment SMS",
    }.get(n, n or "unknown")


async def start_login(api_id, api_hash, phone, force_sms=False):
    """Ask Telegram for a login code.

    Returns a human string saying WHERE the code was sent. `force_sms=True` asks
    for an SMS instead of the in-app message, which is the fix when the in-app
    code never shows up.
    """
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except Exception:
        raise RuntimeError("telethon isn't installed on the server")
    if not (api_id and api_hash and phone):
        raise RuntimeError("api_id, api_hash and phone are all required")

    phone = phone.strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        raise RuntimeError("phone must be in international format, e.g. +919876543210")

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    try:
        await client.connect()
        sent = await client.send_code_request(phone, force_sms=force_sms)
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise RuntimeError(_friendly(e))

    _pending["client"] = client
    _pending["phone"] = phone
    return _delivery_name(getattr(sent, "type", None))


def _friendly(e):
    """Turn Telethon errors into something a person can act on."""
    name = type(e).__name__
    msg = str(e)
    if name == "FloodWaitError" or "FloodWait" in name:
        secs = getattr(e, "seconds", None)
        wait = f"{secs} seconds" if secs else "a while"
        return (f"Telegram is rate-limiting you — wait {wait} before trying again. "
                f"Repeated code requests can lock this for 24 hours.")
    if name == "PhoneNumberInvalidError":
        return "That phone number is not valid — include the country code, e.g. +919876543210."
    if name == "PhoneNumberBannedError":
        return "Telegram has banned this phone number."
    if name == "ApiIdInvalidError":
        return "api_id / api_hash are wrong — copy them again from my.telegram.org."
    if name == "PhoneCodeInvalidError":
        return "That code is wrong. Check the digits, or send a new code."
    if name == "PhoneCodeExpiredError":
        return "That code expired — send a new one."
    if name == "AuthRestartError":
        return "Telegram wants the login restarted — send a new code."
    if name == "SessionPasswordNeededError":
        return "This account has two-step verification — enter the password too."
    if "database is locked" in msg.lower():
        return "Session file is locked — try again in a moment."
    return f"{name}: {msg}"


async def verify_login(code, password=None):
    client = _pending.get("client")
    if client is None:
        raise RuntimeError("no login in progress — send the code again")
    try:
        from telethon.errors import SessionPasswordNeededError
    except Exception:
        SessionPasswordNeededError = RuntimeError
    code = (code or "").strip().replace(" ", "")
    try:
        await client.sign_in(phone=_pending.get("phone"), code=code)
    except SessionPasswordNeededError:
        if not password:
            raise RuntimeError("This account has two-step verification — enter your "
                               "password in the second field as well.")
        try:
            await client.sign_in(password=password)
        except Exception as e:
            raise RuntimeError(_friendly(e))
    except Exception as e:
        raise RuntimeError(_friendly(e))

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
