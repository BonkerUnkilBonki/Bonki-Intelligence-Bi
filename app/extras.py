"""Features ported from BonkBot that Bonki did not already have.

Deliberately NOT duplicated (Bonki already has these, better or equal):
  moderation (ban/kick/mute/warn/purge/pin), locks, notes, rules, welcome,
  federations, captcha, blocklist, antiflood, stickers/kang, quotes, tag,
  skills, analytics, ask, dump/mirror, approvals.

Ported here:
  filters (auto-replies), afk, tagall, per-chat command disable, ping,
  promote/demote/set-title/set-desc, export/import chat settings, global bans.
"""
import asyncio
import html
import json
import logging
import re
import time

from . import db, telegram

log = logging.getLogger("extras")

_MAX_TAGALL = 100
_TAGALL_COOLDOWN = 60
_tagall_last = {}


# ------------------------------------------------------------------ filters --
def _filters(cid):
    return dict((db.get_chatconf(cid) or {}).get("filters") or {})


async def cmd_filter(chat, msg, user, args):
    """Trigger → automatic reply. Supports prefix: and exact: modifiers."""
    from . import rose
    if not await rose._need_admin(chat, msg, user):
        return
    cid = chat.get("id")
    rt = msg.get("reply_to_message") or {}
    parts = (args or "").strip().split(None, 1)
    if rt and parts:
        trigger = parts[0].lower()
        text = rt.get("text") or rt.get("caption") or ""
    elif len(parts) >= 2:
        trigger, text = parts[0].lower(), parts[1]
    else:
        return await rose._reply(
            chat, msg,
            "Usage:\n• <code>/filter hi Hello there</code>\n"
            "• reply to a message with <code>/filter hi</code>\n"
            "Modifiers: <code>prefix:hi</code> · <code>exact:hi</code>")
    if not trigger or not text:
        return await rose._reply(chat, msg, "Need a trigger and a reply.")
    f = _filters(cid)
    f[trigger] = text[:1500]
    db.set_chatconf(cid, {"filters": f})
    await asyncio.to_thread(db.flush, True)
    await rose._reply(chat, msg, f"🔍 Filter <code>{telegram.esc(trigger)}</code> saved.")


async def cmd_stop(chat, msg, user, args):
    from . import rose
    if not await rose._need_admin(chat, msg, user):
        return
    cid = chat.get("id")
    f = _filters(cid)
    trig = (args or "").strip().lower()
    if trig == "all" or trig == "stopall":
        db.set_chatconf(cid, {"filters": {}})
        return await rose._reply(chat, msg, "🧹 All filters removed.")
    if trig not in f:
        return await rose._reply(chat, msg, "No such filter.")
    f.pop(trig)
    db.set_chatconf(cid, {"filters": f})
    await asyncio.to_thread(db.flush, True)
    await rose._reply(chat, msg, f"✅ Removed <code>{telegram.esc(trig)}</code>.")


async def cmd_filters(chat, msg, user, args):
    from . import rose
    f = _filters(chat.get("id"))
    if not f:
        return await rose._reply(chat, msg, "No filters set here.")
    lines = ["🔍 <b>Filters</b>"]
    for k, v in list(f.items())[:40]:
        lines.append(f"• <code>{telegram.esc(k)}</code> → {telegram.esc(str(v)[:60])}")
    await rose._reply(chat, msg, "\n".join(lines))


async def run_filters(chat, msg, user):
    """Send the matching auto-reply. Returns True if one fired."""
    cid = chat.get("id")
    f = _filters(cid)
    if not f:
        return False
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text or text.startswith("/"):
        return False
    low = text.lower()
    for trig, reply in f.items():
        t = trig
        if t.startswith("prefix:"):
            if not low.startswith(t[7:].strip()):
                continue
        elif t.startswith("exact:"):
            if low != t[6:].strip():
                continue
        else:
            if t not in low:
                continue
        try:
            await asyncio.to_thread(telegram.send_message, reply,
                                    chat_id=cid, reply_to=msg.get("message_id"))
        except Exception as e:
            log.info("filter reply failed: %s", e)
        return True
    return False


# ---------------------------------------------------------------------- afk --
async def cmd_afk(chat, msg, user, args):
    from . import rose
    reason = (args or "").strip() or "no reason given"
    db.set_afk((user or {}).get("id"), reason)
    await asyncio.to_thread(db.flush, True)
    await rose._reply(chat, msg,
                      f"💤 <b>{telegram.esc(user.get('first_name') or 'You')}</b> is now "
                      f"AFK: {telegram.esc(reason)}")


async def afk_watch(chat, msg, user):
    """Clear your own AFK on activity; tell others when they ping an AFK user."""
    uid = (user or {}).get("id")
    if uid and db.get_afk(uid):
        db.clear_afk(uid)
        try:
            await asyncio.to_thread(telegram.send_message,
                                    "👋 Welcome back — your AFK is cleared.",
                                    chat_id=chat.get("id"),
                                    reply_to=msg.get("message_id"))
        except Exception:
            pass
        return False
    rt = (msg.get("reply_to_message") or {}).get("from") or {}
    if rt.get("id"):
        info = db.get_afk(rt.get("id"))
        if info:
            try:
                await asyncio.to_thread(
                    telegram.send_message,
                    f"💤 <b>{telegram.esc(rt.get('first_name') or 'That user')}</b> is "
                    f"AFK: {telegram.esc(info)}",
                    chat_id=chat.get("id"), reply_to=msg.get("message_id"))
            except Exception:
                pass
            return True
    return False


# ------------------------------------------------------------------- tagall --
def track_member(chat_id, user):
    """Remember who has spoken, so /tagall has someone to tag."""
    if not (user or {}).get("id") or user.get("is_bot"):
        return
    db.track_member(chat_id, user.get("id"),
                    user.get("first_name") or user.get("username") or "user")


async def cmd_tagall(chat, msg, user, args):
    """Mention everyone who has spoken here. Bots cannot list members."""
    from . import rose
    cid = chat.get("id")
    uid = (user or {}).get("id")
    if not await rose._is_admin(cid, uid):
        return await rose._reply(chat, msg, "Admins only.")

    now = time.time()
    if now - _tagall_last.get(cid, 0) < _TAGALL_COOLDOWN:
        left = int(_TAGALL_COOLDOWN - (now - _tagall_last.get(cid, 0)))
        return await rose._reply(chat, msg, f"⏳ Wait {left}s before tagging again.")

    members = db.list_members(cid)[:_MAX_TAGALL]
    if not members:
        return await rose._reply(
            chat, msg, "I haven't seen anyone speak yet — I can only tag members "
                       "who have posted while I was here (bots can't list members).")
    _tagall_last[cid] = now
    note = (args or "").strip()
    mentions = " ".join(f'<a href="tg://user?id={m["id"]}">'
                        f'{html.escape(m["name"][:20])}</a>' for m in members)
    body = (f"{telegram.esc(note)}\n\n" if note else "") + mentions
    await rose._reply(chat, msg, body[:4000])
    return True


async def cmd_utagall(chat, msg, user, args):
    """Tag everyone EXCEPT admins."""
    from . import rose
    cid = chat.get("id")
    members = db.list_members(cid)[:_MAX_TAGALL]
    keep = []
    for m in members:
        try:
            if await rose._is_admin(cid, m["id"]):
                continue
        except Exception:
            pass
        keep.append(m)
    if not keep:
        return await rose._reply(chat, msg, "Nobody to tag.")
    note = (args or "").strip()
    mentions = " ".join(f'<a href="tg://user?id={m["id"]}">'
                        f'{html.escape(m["name"][:20])}</a>' for m in keep)
    body = (f"{telegram.esc(note)}\n\n" if note else "") + mentions
    await rose._reply(chat, msg, body[:4000])


# ---------------------------------------------------------------- disabling --
def is_disabled(chat_id, cmd):
    dis = (db.get_chatconf(chat_id) or {}).get("disabled") or []
    return (cmd or "").lower() in [str(d).lower() for d in dis]


async def cmd_disable(chat, msg, user, args):
    from . import rose
    if not await rose._need_admin(chat, msg, user):
        return
    cid = chat.get("id")
    cmd = (args or "").strip().lower().lstrip("/")
    if not cmd:
        return await rose._reply(chat, msg, "Usage: <code>/disable purge</code>")
    dis = [str(d) for d in ((db.get_chatconf(cid) or {}).get("disabled") or [])]
    if cmd not in dis:
        dis.append(cmd)
    db.set_chatconf(cid, {"disabled": dis})
    await asyncio.to_thread(db.flush, True)
    await rose._reply(chat, msg, f"🚫 <code>/{telegram.esc(cmd)}</code> disabled here.")


async def cmd_enable(chat, msg, user, args):
    from . import rose
    if not await rose._need_admin(chat, msg, user):
        return
    cid = chat.get("id")
    cmd = (args or "").strip().lower().lstrip("/")
    dis = [str(d) for d in ((db.get_chatconf(cid) or {}).get("disabled") or [])]
    if cmd not in dis:
        return await rose._reply(chat, msg, "That command isn't disabled.")
    dis.remove(cmd)
    db.set_chatconf(cid, {"disabled": dis})
    await asyncio.to_thread(db.flush, True)
    await rose._reply(chat, msg, f"✅ <code>/{telegram.esc(cmd)}</code> enabled.")


async def cmd_disabled(chat, msg, user, args):
    from . import rose
    dis = (db.get_chatconf(chat.get("id")) or {}).get("disabled") or []
    if not dis:
        return await rose._reply(chat, msg, "No commands are disabled here.")
    await rose._reply(chat, msg, "🚫 <b>Disabled</b>: " +
                      ", ".join(f"<code>/{telegram.esc(str(d))}</code>" for d in dis))


# --------------------------------------------------------------------- misc --
async def cmd_ping(chat, msg, user, args):
    from . import rose
    await rose._reply(chat, msg, "🏓 Pong — alive and indexing.")


async def cmd_promote(chat, msg, user, args):
    from . import rose
    cid = chat.get("id")
    if not await rose._is_admin(cid, (user or {}).get("id")):
        return await rose._reply(chat, msg, "Admins only.")
    target, _, _ = rose._split_target(msg, args)
    if not target:
        return await rose._reply(chat, msg, "Reply to a user or give @username.")
    try:
        await asyncio.to_thread(telegram.promote_chat_member, cid, target)
    except Exception as e:
        return await rose._reply(chat, msg, f"⚠️ {telegram.esc(e)}")
    await rose._reply(chat, msg, "⬆️ Promoted.")


async def cmd_demote(chat, msg, user, args):
    from . import rose
    cid = chat.get("id")
    if not await rose._is_admin(cid, (user or {}).get("id")):
        return await rose._reply(chat, msg, "Admins only.")
    target, _, _ = rose._split_target(msg, args)
    if not target:
        return await rose._reply(chat, msg, "Reply to a user or give @username.")
    try:
        await asyncio.to_thread(telegram.demote_chat_member, cid, target)
    except Exception as e:
        return await rose._reply(chat, msg, f"⚠️ {telegram.esc(e)}")
    await rose._reply(chat, msg, "⬇️ Demoted.")


async def cmd_settitle(chat, msg, user, args):
    from . import rose
    if not await rose._is_admin(chat.get("id"), (user or {}).get("id")):
        return await rose._reply(chat, msg, "Admins only.")
    title = (args or "").strip()
    if not title:
        return await rose._reply(chat, msg, "Usage: <code>/settitle New title</code>")
    try:
        await asyncio.to_thread(telegram.set_chat_title, chat.get("id"), title[:128])
    except Exception as e:
        return await rose._reply(chat, msg, f"⚠️ {telegram.esc(e)}")
    await rose._reply(chat, msg, "✅ Title updated.")


async def cmd_setdesc(chat, msg, user, args):
    from . import rose
    if not await rose._is_admin(chat.get("id"), (user or {}).get("id")):
        return await rose._reply(chat, msg, "Admins only.")
    desc = (args or "").strip()
    if not desc:
        return await rose._reply(chat, msg, "Usage: <code>/setdesc New description</code>")
    try:
        await asyncio.to_thread(telegram.set_chat_description, chat.get("id"), desc[:255])
    except Exception as e:
        return await rose._reply(chat, msg, f"⚠️ {telegram.esc(e)}")
    await rose._reply(chat, msg, "✅ Description updated.")


# --------------------------------------------------------------- gban -------
async def cmd_gban(chat, msg, user, args):
    """Global ban across every chat Bonki is in."""
    from . import approval, rose
    if not approval.is_owner((user or {}).get("id")):
        return await rose._reply(chat, msg, "🔒 Owner only — a gban hits every chat.")
    target, _, reason = rose._split_target(msg, args)
    if not target:
        return await rose._reply(chat, msg, "Reply to a user or give @username.")
    reason = reason or "global ban"
    db.add_gban(str(target), reason, str((user or {}).get("id")))
    await asyncio.to_thread(db.flush, True)
    banned = 0
    for g in db.list_groups():
        try:
            await asyncio.to_thread(telegram.ban_chat_member, g.get("chat_id"), target)
            banned += 1
        except Exception:
            pass
    await rose._reply(chat, msg,
                      f"🌐 <b>Global ban</b> on <code>{telegram.esc(str(target))}</code> "
                      f"— banned in {banned} chat(s).")


async def cmd_ungban(chat, msg, user, args):
    from . import approval, rose
    if not approval.is_owner((user or {}).get("id")):
        return await rose._reply(chat, msg, "🔒 Owner only.")
    target, _, _ = rose._split_target(msg, args)
    if not target:
        return await rose._reply(chat, msg, "Reply to a user or give @username.")
    ok = db.remove_gban(str(target))
    await rose._reply(chat, msg, "✅ Removed from the global ban list."
                      if ok else "Not on the global ban list.")


async def cmd_gbanlist(chat, msg, user, args):
    from . import rose
    rows = db.list_gbans()
    if not rows:
        return await rose._reply(chat, msg, "No global bans.")
    lines = [f"🌐 <b>Global bans</b> ({len(rows)})"]
    for r in rows[:30]:
        lines.append(f"• <code>{telegram.esc(r['user'])}</code> — "
                     f"{telegram.esc(str(r.get('reason'))[:40])}")
    await rose._reply(chat, msg, "\n".join(lines))


async def enforce_gban(chat, msg, user):
    """Ban a globally-banned user the moment they speak."""
    uid = (user or {}).get("id")
    if not uid or not db.is_gban(str(uid)):
        return False
    try:
        await asyncio.to_thread(telegram.ban_chat_member, chat.get("id"), uid)
        await asyncio.to_thread(telegram.delete_message, chat.get("id"),
                                msg.get("message_id"))
    except Exception as e:
        log.info("gban enforce failed: %s", e)
    return True


# ----------------------------------------------------------- export/import --
async def cmd_export(chat, msg, user, args):
    """Dump this chat's settings to a JSON file you can import elsewhere."""
    from . import rose
    if not await rose._is_admin(chat.get("id"), (user or {}).get("id")):
        return await rose._reply(chat, msg, "Admins only.")
    cid = chat.get("id")
    conf = db.get_chatconf(cid) or {}
    payload = {
        "version": 1,
        "chat": {"title": chat.get("title"), "id": cid},
        "settings": conf,
        "notes": {n.get("name"): (n.get("content") or n.get("text"))
                  for n in db.list_notes(cid)},
        "rules": conf.get("rules") or "",
        "welcome": conf.get("welcome") or "",
    }
    import os
    import tempfile
    path = os.path.join(tempfile.gettempdir(), f"bonki_export_{cid}.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        await asyncio.to_thread(telegram.send_document, path,
                                f"bonki_{chat.get('title') or cid}_settings.json",
                                "Import elsewhere with /import",
                                cid, msg.get("message_id"))
    except Exception as e:
        await rose._reply(chat, msg, f"⚠️ {telegram.esc(e)}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def cmd_import(chat, msg, user, args):
    """Apply settings from a /export file."""
    from . import rose
    cid = chat.get("id")
    if not await rose._is_admin(cid, (user or {}).get("id")):
        return await rose._reply(chat, msg, "Admins only.")
    doc = (msg.get("reply_to_message") or {}).get("document") or {}
    if not doc.get("file_id"):
        return await rose._reply(chat, msg, "Reply to a <code>.json</code> export "
                                           "with <code>/import</code>.")
    try:
        info = await asyncio.to_thread(telegram.get_file, doc["file_id"])
        raw = await asyncio.to_thread(telegram.download_file,
                                      (info.get("result") or {}).get("file_path"))
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception as e:
        return await rose._reply(chat, msg, f"⚠️ Couldn't read that file: {telegram.esc(e)}")
    settings = data.get("settings") or {}
    if not isinstance(settings, dict):
        return await rose._reply(chat, msg, "That doesn't look like a Bonki export.")
    db.set_chatconf(cid, settings)
    for name, text in (data.get("notes") or {}).items():
        db.save_note(cid, name, text, by=(user or {}).get("id"))
    await asyncio.to_thread(db.flush, True)
    await rose._reply(chat, msg,
                      f"✅ Imported settings + {len(data.get('notes') or {})} note(s).")
