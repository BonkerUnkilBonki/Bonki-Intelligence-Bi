"""Rose-style group management - a practical, working core.

Commands are typed as ordinary slash commands and need **no wake word**:

  moderation : /ban /unban /kick /mute /unmute /warn /warns /resetwarns
               /purge /del /pin /unpin /report
  notes      : /save /get /clear /notes
  rules      : /setrules /rules
  welcome    : /setwelcome /welcome /setgoodbye /goodbye
  info       : /id /info /admins /help
  special    : /tag <@user> <n>   (also "Bonki tag @user 5")

Not included (Rose has these, this build doesn't): filters, locks, blacklists,
antiflood, federations, connections, captcha, scheduled messages. Those are big
separate modules - see the README.

Moderation needs the bot to be an admin with the relevant rights, and the person
issuing it to be an admin of the group.
"""
import asyncio
import logging
import os
import re
import tempfile
import time

from . import analytics, db, federation, memory, ml, skills, stickers, telegram

log = logging.getLogger("rose")

MAX_WARNS = 3
TAG_MAX = 100                # ceiling; the actual cap comes from settings tag_max
TAG_COOLDOWN = 60            # seconds between tag commands, per user
MAX_PURGE = 200              # messages per purge
LOCK_TYPES = ("media", "sticker", "gif", "link", "forward", "bots", "commands")


def _tag_cap():
    try:
        return max(1, min(int(db.get_settings().get("tag_max") or TAG_MAX), 100))
    except (TypeError, ValueError):
        return TAG_MAX
ADMIN_STATUS = ("administrator", "creator")

_tag_last = {}


# ---------------------------------------------------------------- helpers ---
def _esc(s):
    return telegram.esc(s)


async def _reply(chat, msg, text):
    return await asyncio.to_thread(telegram.send_message, text, chat_id=chat.get("id"),
                                   reply_to=msg.get("message_id"))


async def _is_admin(chat_id, user_id):
    from . import approval
    if approval.is_owner(user_id):
        return True
    try:
        r = await asyncio.to_thread(telegram.get_chat_member, chat_id, user_id)
        return (r.get("result") or {}).get("status") in ADMIN_STATUS
    except Exception:
        return False


def _mention(uid, uname=None, fallback="user"):
    if uname:
        return "@" + str(uname).lstrip("@")
    if uid:
        return f'<a href="tg://user?id={uid}">{fallback}</a>'
    return fallback


def _split_target(msg, args):
    """(user_id, username, reason) for a moderation/tag command.

    An explicit @username or numeric id in the command ALWAYS wins. Only when no
    target is typed does it fall back to the message being replied to - otherwise
    replying to any message (e.g. one of the bot's own results) hijacked the
    target, and "Bonki tag @someone 10" ended up tagging the bot itself.
    """
    parts = (args or "").split(None, 1)
    if parts:
        a = parts[0]
        if a.startswith("@") or re.fullmatch(r"-?\d+", a.lstrip("-")):
            uid = int(a) if a.lstrip("-").isdigit() else a
            uname = a.lstrip("@") if a.startswith("@") else None
            return uid, uname, (parts[1] if len(parts) > 1 else "").strip()
    rt = msg.get("reply_to_message") or {}
    frm = rt.get("from") or {}
    if frm.get("id"):
        return frm.get("id"), frm.get("username"), (args or "").strip()
    return None, None, ""


def _until(spec):
    """'30m' / '2h' / '1d' -> an epoch until_date Telegram accepts."""
    if not spec:
        return None
    m = re.fullmatch(r"(\d+)\s*([smhdw]?)", spec.strip().lower())
    if not m:
        return None
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2) or "m"]
    secs = min(max(int(m.group(1)) * mult, 60), 366 * 86400)
    return int(time.time()) + secs


async def _need_admin(chat, msg, user):
    if await _is_admin(chat.get("id"), user.get("id")):
        return True
    await _reply(chat, msg, "🚫 Only group admins can use this.")
    return False


# ------------------------------------------------------------- moderation ---
async def cmd_ban(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    uid, un, reason = _split_target(msg, args)
    if not uid:
        return await _reply(chat, msg, "Reply to a user, or give an @username.")
    try:
        await asyncio.to_thread(telegram.ban_chat_member, chat.get("id"), uid)
        await _reply(chat, msg, f"🔨 Banned {_mention(uid, un)}"
                    + (f" — {_esc(reason)}" if reason else ""))
    except Exception as e:
        await _reply(chat, msg, f"⚠️ Couldn't ban: {_esc(e)} — is the bot an admin "
                                f"with 'Ban users' rights?")


async def cmd_unban(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    uid, un, _ = _split_target(msg, args)
    if not uid:
        return await _reply(chat, msg, "Reply to a user, or give an @username.")
    try:
        await asyncio.to_thread(telegram.unban_chat_member, chat.get("id"), uid)
        await _reply(chat, msg, f"✅ Unbanned {_mention(uid, un)}")
    except Exception as e:
        await _reply(chat, msg, f"⚠️ Couldn't unban: {_esc(e)}")


async def cmd_kick(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    uid, un, reason = _split_target(msg, args)
    if not uid:
        return await _reply(chat, msg, "Reply to a user, or give an @username.")
    try:
        await asyncio.to_thread(telegram.ban_chat_member, chat.get("id"), uid)
        await asyncio.to_thread(telegram.unban_chat_member, chat.get("id"), uid)
        await _reply(chat, msg, f"👢 Kicked {_mention(uid, un)}"
                    + (f" — {_esc(reason)}" if reason else ""))
    except Exception as e:
        await _reply(chat, msg, f"⚠️ Couldn't kick: {_esc(e)}")


async def cmd_mute(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    uid, un, rest = _split_target(msg, args)
    if not uid:
        return await _reply(chat, msg, "Reply to a user, or give an @username.")
    until = _until(rest.split()[0]) if rest else None
    if not until:                       # default: 1 hour
        until = _until("1h")
    try:
        await asyncio.to_thread(telegram.restrict_chat_member, chat.get("id"), uid,
                                True, until)
        await _reply(chat, msg, f"🔇 Muted {_mention(uid, un)}")
    except Exception as e:
        await _reply(chat, msg, f"⚠️ Couldn't mute: {_esc(e)}")


async def cmd_unmute(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    uid, un, _ = _split_target(msg, args)
    if not uid:
        return await _reply(chat, msg, "Reply to a user, or give an @username.")
    try:
        await asyncio.to_thread(telegram.restrict_chat_member, chat.get("id"), uid, False)
        await _reply(chat, msg, f"🔊 Unmuted {_mention(uid, un)}")
    except Exception as e:
        await _reply(chat, msg, f"⚠️ Couldn't unmute: {_esc(e)}")


async def cmd_warn(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    uid, un, reason = _split_target(msg, args)
    if not uid:
        return await _reply(chat, msg, "Reply to a user, or give an @username.")
    count = db.add_warn(chat.get("id"), str(uid), reason, user.get("id"))
    if count >= MAX_WARNS:
        db.reset_warns(chat.get("id"), str(uid))
        try:
            await asyncio.to_thread(telegram.restrict_chat_member, chat.get("id"), uid,
                                    True, _until("1h"))
            return await _reply(chat, msg, f"⚠️ {_mention(uid, un)} reached {MAX_WARNS} "
                                f"warnings — muted for 1 hour. Warnings reset.")
        except Exception:
            pass
    await _reply(chat, msg, f"⚠️ Warned {_mention(uid, un)} ({count}/{MAX_WARNS})"
                + (f" — {_esc(reason)}" if reason else ""))


async def cmd_warns(chat, msg, user, args):
    uid, un, _ = _split_target(msg, args)
    if uid:
        rows = db.list_warns(chat.get("id"), str(uid))
        if not rows:
            return await _reply(chat, msg, f"{_mention(uid, un)} has no warnings.")
        lines = [f"⚠️ {_mention(uid, un)} — {len(rows)} warning(s):"]
        for i, w in enumerate(rows, 1):
            lines.append(f"{i}. {_esc(w.get('reason') or '(no reason)')}")
        return await _reply(chat, msg, "\n".join(lines))
    rows = db.list_warns(chat.get("id"))
    if not rows:
        return await _reply(chat, msg, "No warnings in this group.")
    per = {}
    for w in rows:
        per[w["user"]] = per.get(w["user"], 0) + 1
    lines = ["⚠️ <b>Warnings</b>"]
    for u, n in sorted(per.items(), key=lambda x: -x[1])[:20]:
        lines.append(f"• <code>{_esc(u)}</code>: {n}")
    await _reply(chat, msg, "\n".join(lines))


async def cmd_resetwarns(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    uid, un, _ = _split_target(msg, args)
    if not uid:
        return await _reply(chat, msg, "Reply to a user, or give an @username.")
    db.reset_warns(chat.get("id"), str(uid))
    await _reply(chat, msg, f"✅ Cleared warnings for {_mention(uid, un)}")


async def cmd_del(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    rt = msg.get("reply_to_message") or {}
    if not rt.get("message_id"):
        return await _reply(chat, msg, "Reply to the message you want deleted.")
    try:
        await asyncio.to_thread(telegram.delete_message, chat.get("id"), rt["message_id"])
        await asyncio.to_thread(telegram.delete_message, chat.get("id"), msg.get("message_id"))
    except Exception as e:
        await _reply(chat, msg, f"⚠️ Couldn't delete: {_esc(e)}")


async def _delete_ids(chat_id, ids):
    ok = 0
    for mid in ids:
        try:
            await asyncio.to_thread(telegram.delete_message, chat_id, mid)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)          # gentle: don't trip flood control
    return ok


async def cmd_purge(chat, msg, user, args):
    """Purge messages.

    Works in forum topics too: message ids are unique across the whole chat, so
    deleting the id range from the replied-to message up to this one only ever
    touches messages inside that same topic.

      /purge        reply -> delete from that message up to here
      /purge 20     delete the last 20 messages
    """
    if not await _need_admin(chat, msg, user):
        return
    rt = msg.get("reply_to_message") or {}
    end = int(msg.get("message_id") or 0)
    arg = (args or "").strip()
    if arg.isdigit():
        n = max(1, min(int(arg), MAX_PURGE))
        ids = list(range(max(1, end - n + 1), end + 1))   # exactly n messages
    elif rt.get("message_id"):
        start = int(rt["message_id"])
        if start > end:
            start, end = end, start
        ids = list(range(start, end + 1))
        if len(ids) > MAX_PURGE:
            ids = ids[-MAX_PURGE:]
    else:
        return await _reply(chat, msg, "Reply to the first message to purge from, "
                                       "or use <code>/purge 20</code>.")
    deleted = await _delete_ids(chat.get("id"), ids)
    log.info("purged %d/%d messages in %s", deleted, len(ids), chat.get("id"))
    try:
        await _reply(chat, msg, f"🧹 Deleted {deleted} message(s).")
    except Exception:
        pass


# ------------------------------------------------------------------ locks ---
async def cmd_lock(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    kind = (args or "").strip().lower().split()[0] if (args or "").strip() else ""
    if kind not in LOCK_TYPES:
        return await _reply(chat, msg, f"Usage: <code>/lock {'|'.join(LOCK_TYPES)}</code>")
    conf = db.get_chatconf(chat.get("id"))
    locks = set(conf.get("locks") or [])
    locks.add(kind)
    db.set_chatconf(chat.get("id"), {"locks": sorted(locks)})
    await _reply(chat, msg, f"🔒 Locked <b>{kind}</b>.")


async def cmd_unlock(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    kind = (args or "").strip().lower().split()[0] if (args or "").strip() else ""
    if kind not in LOCK_TYPES:
        return await _reply(chat, msg, f"Usage: <code>/unlock {'|'.join(LOCK_TYPES)}</code>")
    conf = db.get_chatconf(chat.get("id"))
    locks = set(conf.get("locks") or [])
    locks.discard(kind)
    db.set_chatconf(chat.get("id"), {"locks": sorted(locks)})
    await _reply(chat, msg, f"🔓 Unlocked <b>{kind}</b>.")


async def cmd_locks(chat, msg, user, args):
    conf = db.get_chatconf(chat.get("id"))
    locks = conf.get("locks") or []
    if not locks:
        return await _reply(chat, msg, "No locks set in this chat.")
    await _reply(chat, msg, "🔒 <b>Locked</b>: " + ", ".join(locks))


async def enforce_locks(chat, msg, user):
    """Delete a message that breaks an active lock. Returns True if deleted."""
    conf = db.get_chatconf(chat.get("id"))
    locks = conf.get("locks") or []
    if not locks:
        return False
    if await _is_admin(chat.get("id"), (user or {}).get("id")):
        return False
    text = msg.get("text") or msg.get("caption") or ""
    hit = None
    if "bots" in locks and (user or {}).get("is_bot"):
        hit = "bots"
    if not hit and "forward" in locks and (msg.get("forward_from") or msg.get("forward_origin")
                                            or msg.get("forward_from_chat")):
        hit = "forward"
    if not hit and "sticker" in locks and msg.get("sticker"):
        hit = "sticker"
    if not hit and "gif" in locks and msg.get("animation"):
        hit = "gif"
    if not hit and "media" in locks and any(msg.get(k) for k in
                                            ("photo", "video", "audio", "document", "voice")):
        hit = "media"
    if not hit and "link" in locks and re.search(r"(https?://|t\.me/|www\.)", text, re.I):
        hit = "link"
    if not hit and "commands" in locks and text.startswith("/"):
        hit = "commands"
    if hit:
        try:
            await asyncio.to_thread(telegram.delete_message, chat.get("id"), msg.get("message_id"))
            return True
        except Exception as e:
            log.warning("lock delete failed: %s", e)
    return False


async def cmd_pin(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    rt = msg.get("reply_to_message") or {}
    mid = rt.get("message_id") or msg.get("message_id")
    try:
        await asyncio.to_thread(telegram.pin_chat_message, chat.get("id"), mid, True)
        await _reply(chat, msg, "📌 Pinned.")
    except Exception as e:
        await _reply(chat, msg, f"⚠️ Couldn't pin: {_esc(e)}")


async def cmd_unpin(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    try:
        await asyncio.to_thread(telegram.unpin_chat_message, chat.get("id"))
        await _reply(chat, msg, "📌 Unpinned.")
    except Exception as e:
        await _reply(chat, msg, f"⚠️ Couldn't unpin: {_esc(e)}")


async def cmd_report(chat, msg, user, args):
    rt = msg.get("reply_to_message") or {}
    target = _mention((rt.get("from") or {}).get("id"), (rt.get("from") or {}).get("username"))
    try:
        r = await asyncio.to_thread(telegram.get_chat_administrators, chat.get("id"))
        admins = [a.get("user") or {} for a in (r.get("result") or [])]
        who = " ".join(_mention(a.get("id"), a.get("username"), a.get("first_name") or "admin")
                       for a in admins if not a.get("is_bot"))[:600]
    except Exception:
        who = ""
    text = (f"🚨 <b>Report</b> from {_mention(user.get('id'), user.get('username'))}"
            + (f" about {target}" if rt else ""))
    if who:
        text += "\n" + who
    await _reply(chat, msg, text)


# ------------------------------------------------------------------ notes ---
async def cmd_save(chat, msg, user, args):
    parts = (args or "").split(None, 1)
    if not parts:
        return await _reply(chat, msg, "Usage: <code>/save name content</code> "
                                       "(or reply to a message)")
    name = parts[0]
    rt = msg.get("reply_to_message") or {}
    content = parts[1] if len(parts) > 1 else (rt.get("text") or rt.get("caption") or "")
    if not content:
        return await _reply(chat, msg, "Give some content, or reply to a message to save.")
    db.save_note(chat.get("id"), name, content, user.get("id"))
    await _reply(chat, msg, f"💾 Saved note <code>{_esc(name)}</code>.")


async def cmd_get(chat, msg, user, args):
    name = (args or "").strip().split()[0] if args else ""
    if not name:
        return await _reply(chat, msg, "Usage: <code>/get name</code>")
    n = db.get_note(chat.get("id"), name)
    if not n:
        return await _reply(chat, msg, f"No note called <code>{_esc(name)}</code>.")
    await _reply(chat, msg, f"📄 <b>{_esc(n['name'])}</b>\n{_esc(n['content'])[:3500]}")


async def cmd_clear(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    name = (args or "").strip().split()[0] if args else ""
    if not name:
        return await _reply(chat, msg, "Usage: <code>/clear name</code>")
    ok = db.delete_note(chat.get("id"), name)
    await _reply(chat, msg, "🗑 Deleted." if ok else "No such note.")


async def cmd_notes(chat, msg, user, args):
    names = db.list_notes(chat.get("id"))
    if not names:
        return await _reply(chat, msg, "No notes yet. Save one with /save.")
    await _reply(chat, msg, "📒 <b>Notes</b>\n" +
                 ", ".join(f"<code>{_esc(n)}</code>" for n in sorted(names)[:60]))


# ------------------------------------------------------------ rules / welcome
async def cmd_setrules(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    rt = msg.get("reply_to_message") or {}
    text = args or (rt.get("text") or "")
    if not text:
        return await _reply(chat, msg, "Give the rules text, or reply to a message.")
    db.set_chatconf(chat.get("id"), {"rules": text})
    await _reply(chat, msg, "📜 Rules saved.")


async def cmd_rules(chat, msg, user, args):
    rules = db.get_chatconf(chat.get("id")).get("rules")
    await _reply(chat, msg, f"📜 <b>Rules</b>\n{_esc(rules)}" if rules
                 else "No rules set. An admin can use /setrules.")


async def cmd_setwelcome(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    rt = msg.get("reply_to_message") or {}
    text = args or (rt.get("text") or "")
    if not text:
        return await _reply(chat, msg, "Give the welcome text, or reply to a message. "
                                       "Use <code>{name}</code> for the member.")
    db.set_chatconf(chat.get("id"), {"welcome": text, "welcome_on": 1})
    await _reply(chat, msg, "👋 Welcome message saved (and turned on).")


async def cmd_welcome(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    on = (args or "").strip().lower() not in ("off", "0", "no")
    db.set_chatconf(chat.get("id"), {"welcome_on": 1 if on else 0})
    await _reply(chat, msg, f"👋 Welcome messages {'on' if on else 'off'}.")


async def cmd_setgoodbye(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    rt = msg.get("reply_to_message") or {}
    text = args or (rt.get("text") or "")
    if not text:
        return await _reply(chat, msg, "Give the goodbye text, or reply to a message.")
    db.set_chatconf(chat.get("id"), {"goodbye": text, "goodbye_on": 1})
    await _reply(chat, msg, "👋 Goodbye message saved.")


async def cmd_goodbye(chat, msg, user, args):
    if not await _need_admin(chat, msg, user):
        return
    on = (args or "").strip().lower() not in ("off", "0", "no")
    db.set_chatconf(chat.get("id"), {"goodbye_on": 1 if on else 0})
    await _reply(chat, msg, f"👋 Goodbye messages {'on' if on else 'off'}.")


async def welcome_new(chat, msg, members):
    conf = db.get_chatconf(chat.get("id"))
    if not conf.get("welcome_on") or not conf.get("welcome"):
        return
    for m in members:
        if (m or {}).get("is_bot"):
            continue
        name = (m.get("first_name") or m.get("username") or "there")
        text = str(conf["welcome"]).replace("{name}", _mention(m.get("id"), m.get("username"), name))
        try:
            await asyncio.to_thread(telegram.send_message, text, chat_id=chat.get("id"))
        except Exception as e:
            log.warning("welcome failed: %s", e)


# ------------------------------------------------------------------- info ---
async def cmd_id(chat, msg, user, args):
    rt = msg.get("reply_to_message") or {}
    frm = rt.get("from") or user or {}
    await _reply(chat, msg, f"🆔 <b>Chat</b>: <code>{chat.get('id')}</code>\n"
                            f"👤 <b>User</b>: <code>{frm.get('id')}</code>")


async def cmd_info(chat, msg, user, args):
    rt = msg.get("reply_to_message") or {}
    t = rt.get("from") or user or {}
    try:
        r = await asyncio.to_thread(telegram.get_chat_member, chat.get("id"), t.get("id"))
        status = (r.get("result") or {}).get("status", "?")
    except Exception:
        status = "?"
    name = " ".join(x for x in [t.get("first_name"), t.get("last_name")] if x) or "(no name)"
    await _reply(chat, msg, f"👤 <b>{_esc(name)}</b>\n"
                            f"id: <code>{t.get('id')}</code>\n"
                            f"username: {('@' + t['username']) if t.get('username') else '—'}\n"
                            f"status: {_esc(status)}")


async def cmd_admins(chat, msg, user, args):
    try:
        r = await asyncio.to_thread(telegram.get_chat_administrators, chat.get("id"))
        rows = r.get("result") or []
    except Exception as e:
        return await _reply(chat, msg, f"⚠️ {_esc(e)}")
    lines = ["👑 <b>Admins</b>"]
    for a in rows:
        u = a.get("user") or {}
        if u.get("is_bot"):
            continue
        lines.append(f"• {_mention(u.get('id'), u.get('username'), u.get('first_name') or 'admin')}")
    await _reply(chat, msg, "\n".join(lines[:50]))


# --------------------------------------------------------- special: /tag ----
async def cmd_tag(chat, msg, user, args):
    """Tag a user N times as N separate messages. Capped and rate-limited.

    Target resolution here is deliberately different from /ban & co: a bare
    number in a tag command is the COUNT ("tag 5"), not a user id. So the target
    comes from an explicit @username, else from the replied-to *user*.
    """
    cid = chat.get("id")
    args = (args or "").strip()
    rfrom = ((msg.get("reply_to_message") or {}).get("from") or {})
    if args.startswith("@"):
        head, _, tail = args.partition(" ")
        uid, un, rest = head, head.lstrip("@"), tail.strip()
    elif rfrom.get("id"):
        uid, un, rest = rfrom.get("id"), rfrom.get("username"), args
    else:
        return await _reply(chat, msg, "Usage: <code>/tag @user 5</code> — or reply "
                                       "to their message with <code>/tag 5</code>.")

    # never let it tag the bot itself (e.g. when the command replies to a result)
    try:
        from . import handlers
        if str(uid) == str(handlers._bot_identity.get("id")):
            return await _reply(chat, msg, "That's me — give a user's @username, or "
                                           "reply to *their* message.")
    except Exception:
        pass

    m = re.search(r"\b(\d{1,4})\b", rest or "")
    count = int(m.group(1)) if m else 1
    cap = _tag_cap()
    wanted = count
    count = max(1, min(count, cap))

    key = (str(cid), str(user.get("id")))
    now = time.time()
    if now - _tag_last.get(key, 0) < TAG_COOLDOWN:
        left = int(TAG_COOLDOWN - (now - _tag_last.get(key, 0)))
        return await _reply(chat, msg, f"⏳ Slow down — you can tag again in {left}s.")
    _tag_last[key] = now

    mention = _mention(uid, un)
    if count > 5:
        try:                        # the notice must never abort the command
            note = (f"⏳ Sending {count} tags. Telegram allows about 20 messages a "
                    f"minute per group, so this takes about "
                    f"{max(1, round(count / 20))} minute(s) — please wait.")
            if wanted > count:
                note = (f"⚠️ You asked for {wanted}, but the cap is {count} per "
                        f"command (Settings → Tag cap).\n" + note)
            await _reply(chat, msg, note)
        except Exception:
            pass

    sent = 0
    for _ in range(count):
        ok = False
        for _attempt in range(5):
            try:
                await _reply(chat, msg, mention)
                sent += 1
                ok = True
                break
            except telegram.RateLimited as rl:
                # Telegram told us exactly how long to wait - obey it, don't give up
                await asyncio.sleep(min(rl.retry_after + 1, 60))
            except Exception as e:
                log.warning("tag send failed after %d: %s", sent, e)
                break
        if not ok:
            break
        await asyncio.sleep(0.9)               # gentle pacing, under the per-group cap

    note = f"⚠️ Only {sent} of {count} tags went through (Telegram flood limit)."
    if sent < count:
        try:
            await _reply(chat, msg, note)
        except Exception:
            pass


# ------------------------------------------------------------------ help ----
HELP = (
    "🌹 <b>Bonki commands</b>\n\n"
    "<b>Moderation</b> (admins):\n"
    "/ban /unban /kick /mute [30m] /unmute\n"
    "/warn /warns /resetwarns\n"
    "/purge (reply) · /del (reply) · /pin · /unpin · /report\n\n"
    "<b>Notes</b>: /save name text · /get name · /clear name · /notes\n"
    "<b>Rules</b>: /setrules text · /rules\n"
    "<b>Welcome</b>: /setwelcome text ({name}) · /welcome on|off · /setgoodbye · /goodbye\n"
    "<b>Info</b>: /id · /info · /admins\n"
    "<b>Stickers</b>: /kang — reply to a sticker/image to add it to your pack\n"
    "/packs · /getsticker · /stickerid · /delsticker\n"
    "<b>Quotes</b>: /q — reply to a message to make a quote image\n"
    "<b>Locks</b>: /lock media|sticker|gif|link|forward|bots|commands · /unlock · /locks\n"
    "<b>Federation</b>: /newfed name · /joinfed id · /leavefed · /fedinfo · /fban · /unfban\n"
    "<b>Special</b>: /tag @user 5 — tags them 5 times, in 5 separate messages (max 20)\n\n"
    "Files: <i>Bonki, find apple music apk</i> · <i>Bonki, scan this group</i>"
)


async def cmd_indexstatus(chat, msg, user, args):
    """Show exactly what the bot can and cannot see here - answers
    "why hasn't it indexed anything?" without guesswork."""
    cid = chat.get("id")
    files = db.list_files(chat_id=cid)
    live = str(db.get_settings().get("index_live") or "1") == "1"

    bot_id, privacy = None, None
    try:
        from . import handlers
        bot_id = handlers._bot_identity.get("id")
    except Exception:
        pass
    try:
        r = (await asyncio.to_thread(telegram.get_me)).get("result") or {}
        privacy = r.get("can_read_all_group_messages")
        bot_id = bot_id or r.get("id")
    except Exception:
        pass

    status = "unknown"
    if bot_id:
        try:
            status = ((await asyncio.to_thread(telegram.get_chat_member, cid, bot_id))
                      .get("result") or {}).get("status") or "unknown"
        except Exception:
            status = "not a member?"

    last = max([f.get("date") or 0 for f in files], default=0)
    last_txt = time.strftime("%d %b %Y %H:%M", time.localtime(last)) if last else "—"
    is_admin = status in ("administrator", "creator")
    lines = [
        "🔎 <b>Indexing status for this chat</b>",
        f"• my role here: <b>{status}</b> " + ("✅" if is_admin else
                                              "— make me admin so I get every message"),
        "• receives all messages: " + ("✅ yes" if privacy else
                                       "❌ no — turn off privacy in @BotFather "
                                       "(/setprivacy → Disable), or make me admin"),
        "• live indexing: " + ("✅ on" if live else "❌ off (Settings → Live indexing)"),
        f"• files indexed here: <b>{len(files)}</b> · newest: {last_txt}",
        "",
        "<b>Old messages:</b> a bot cannot read them. Telegram gives bots no access "
        "to messages sent before they joined — admin rights do not change that. "
        "Only the user account at /userbot can backfill.",
        "<b>From now on:</b> anything posted here is indexed automatically.",
    ]
    await _reply(chat, msg, "\n".join(lines))


# ------------------------------------------------- remember / learn / dump ---
def _fmt_num(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—" if n in (None, "") else str(n)
    neg = n < 0
    n = abs(n)
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if n >= div:
            return ("-" if neg else "") + f"{n / div:.1f}{unit}".replace(".0", "")
    return ("-" if neg else "") + f"{int(n)}"


def _fmt_size(b):
    try:
        b = float(b or 0)
    except (TypeError, ValueError):
        b = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.1f} {unit}".replace(".0 ", " ")
        b /= 1024.0
    return f"{b:.1f} TB"


def _find_chat_row(target):
    """Resolve @handle / numeric id to a known chat row."""
    t = (target or "").strip().lstrip("@").lower()
    if not t:
        return None
    for g in db.list_groups():
        if (g.get("handle") or "").lstrip("@").lower() == t:
            return g
        if str(g.get("chat_id")) == t:
            return g
    return None


async def cmd_channelstat(chat, msg, user, args):
    """Native channel analytics — computed by Bonki, no third-party API."""
    cid = chat.get("id")
    title = chat.get("title") or ""
    handle = (chat.get("username") or "").lstrip("@")
    target = (args or "").strip()
    if target:
        row = _find_chat_row(target)
        if not row:
            return await _reply(chat, msg,
                                f"I'm not in “{telegram.esc(target)}”. "
                                f"Use <code>/catalog</code> to see where I am.")
        cid = row.get("chat_id")
        title = row.get("title") or ""
        handle = (row.get("handle") or "").lstrip("@")

    await _reply(chat, msg, "📊 Crunching the numbers…")
    try:
        d = await asyncio.to_thread(analytics.compute, cid)
    except Exception as e:
        return await _reply(chat, msg, f"⚠️ {telegram.esc(e)}")

    growth = d.get("growth")
    metrics = [
        ("members", _fmt_num(d.get("members"))),
        ("files indexed", _fmt_num(d.get("files"))),
        ("total size", _fmt_size(d.get("size"))),
        ("7-day growth", ("+" if (growth or 0) >= 0 else "") + _fmt_num(growth)
         if growth is not None else "—"),
    ]
    sub = ("@" + handle) if handle else ("id " + str(cid))
    try:
        img = await asyncio.to_thread(stickers.render_stats_card, title or str(cid),
                                      sub, metrics, d.get("daily") or None,
                                      "Bonki analytics",
                                      "files per day · last 14 days")
        png = await asyncio.to_thread(stickers.to_png, img)
    except Exception as e:
        return await _reply(chat, msg, f"⚠️ Couldn't draw the card: {telegram.esc(e)}")

    # a short text breakdown under the card
    lines = []
    if d.get("types"):
        lines.append("Top types: " + ", ".join(f"{e} {n}" for e, n in d["types"][:4]))
    if d.get("senders"):
        lines.append("Top sharers: " + ", ".join(f"{s} {n}" for s, n in d["senders"][:3]))
    if d.get("last_activity"):
        lines.append("Last activity: " +
                     time.strftime("%d %b %Y", time.localtime(d["last_activity"])))
    lines.append("<i>Views and ER need post view counts, which the Bot API does not "
                 "expose — so they are not shown rather than guessed.</i>")

    path = os.path.join(tempfile.gettempdir(),
                        f"stat_{cid}_{msg.get('message_id')}.png")
    try:
        with open(path, "wb") as fh:
            fh.write(png)
        await asyncio.to_thread(telegram.send_photo, path, "stats.png",
                                "\n".join(lines)[:900], chat.get("id"),
                                msg.get("message_id"))
    except Exception as e:
        await _reply(chat, msg, f"⚠️ Couldn't send the card: {telegram.esc(e)}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def cmd_catalog(chat, msg, user, args):
    """Browse every chat Bonki is in, ranked by how much it has indexed."""
    try:
        rows = await asyncio.to_thread(analytics.catalog, args or None)
    except Exception as e:
        return await _reply(chat, msg, f"⚠️ {telegram.esc(e)}")
    if not rows:
        return await _reply(chat, msg, "Nothing matches — I'm not in any such chat.")
    lines = [f"🗂 <b>Catalog</b> — {len(rows)} chat(s)"]
    for r in rows[:20]:
        bits = [f"{r['files']} files", _fmt_size(r.get("size"))]
        if r.get("members"):
            bits.append(_fmt_num(r["members"]) + " members")
        if r.get("handle"):
            bits.append("@" + r["handle"])
        if not r.get("approved"):
            bits.append("⏳")
        lines.append(f"• <b>{telegram.esc(r['title'])}</b> — " + " · ".join(bits))
    lines.append("\n<code>/channelstat @name</code> for the full card.")
    await _reply(chat, msg, "\n".join(lines)[:telegram.MAX_MESSAGE])


# ------------------------------------------------------- skills & decisions ---
SKILL_HELP = (
    "⏱ <b>Skills</b> — things I do on a schedule.\n\n"
    "<code>/skill every day 09:00 summarise this group</code>\n"
    "<code>/skill every mon 09:00 send the weekly digest</code>\n"
    "<code>/skill in 2h remind me to check the upload</code>\n"
    "<code>/skill at 18:30 stats</code>\n\n"
    "What I can actually run: <b>summarise</b>, <b>stats</b>, <b>digest</b>, "
    "<b>find &lt;query&gt;</b>, or a plain reminder.\n"
    "I have no OAuth connections, so I can't pull from GitHub or your inbox — "
    "that part of Mira needs their integrations, not mine.\n\n"
    "<code>/skill</code> lists them · <code>/skill del N</code> removes one."
)


async def cmd_skill(chat, msg, user, args):
    cid = chat.get("id")
    a = (args or "").strip()
    if not a or a.lower() in ("list", "ls", "help"):
        rows = db.list_skills(cid)
        if not rows:
            return await _reply(chat, msg, "No skills in this chat yet.\n\n" + SKILL_HELP)
        lines = [f"⏱ <b>Skills in this chat</b> ({len(rows)})"]
        for r in rows:
            lines.append(skills.describe(r))
        lines.append("\nRemove one with <code>/skill del N</code>")
        return await _reply(chat, msg, "\n".join(lines))

    low = a.lower()
    if low.startswith(("del ", "remove ", "stop ")):
        n = a.split(None, 1)[1].strip().lstrip("#")
        if n.isdigit() and db.delete_skill(int(n)):
            return await _reply(chat, msg, f"🗑 Skill #{n} removed.")
        return await _reply(chat, msg, "No such skill id.")

    row, err = skills.add(cid, a, by=(user or {}).get("id"))
    if err:
        return await _reply(chat, msg, err)
    await _reply(chat, msg, "✅ Skill added.\n" + skills.describe(row))


async def cmd_decide(chat, msg, user, args):
    text = (args or "").strip()
    if not text:
        return await _reply(chat, msg, "Usage: <code>/decide we ship on Friday</code>")
    db.add_decision(chat.get("id"), text, by=(user or {}).get("id"))
    memory.add("decision", chat.get("id"), text,
               {"tags": memory.keywords(text, limit=6)})
    await _reply(chat, msg, "📌 Recorded as a decision.")


async def cmd_decisions(chat, msg, user, args):
    rows = db.list_decisions(chat.get("id"), 15)
    if not rows:
        return await _reply(chat, msg, "No decisions recorded yet — use "
                                       "<code>/decide …</code> and I'll remember.")
    lines = ["📌 <b>Recent decisions</b>"]
    for r in rows:
        when = time.strftime("%d %b", time.localtime(r.get("t") or 0))
        lines.append(f"• {telegram.esc((r.get('text') or '')[:160])} — <i>{when}</i>")
    await _reply(chat, msg, "\n".join(lines))


# -------------------------------------------------------------------- ML -----
async def cmd_ml(chat, msg, user, args):
    """Show the ML layers and, for a query, the real component scores."""
    cid = chat.get("id")
    try:
        st = await asyncio.to_thread(ml.status, cid)
    except Exception as e:
        return await _reply(chat, msg, f"⚠️ {telegram.esc(e)}")

    ls = st.get("learn") or {}
    lines = ["🧠 <b>Machine learning — live status</b>", ""]
    lines.append(f"<b>Brain</b>: {telegram.esc(st.get('brain') or 'off')}")
    if st.get("embed_on"):
        lines.append(f"<b>Embeddings (deep learning)</b>: ✅ {telegram.esc(st.get('embed_model'))}"
                     f" · {st.get('embed_cached')} vectors cached")
    else:
        lines.append("<b>Embeddings (deep learning)</b>: ❌ off — add GEMINI_API_KEY or "
                     "OPENAI_API_KEY to switch it on")
    if st.get("ranker_trained"):
        lines.append(f"<b>Learned ranker</b>: ✅ logistic regression trained on "
                     f"{st.get('ranker_rows')} examples")
    else:
        lines.append(f"<b>Learned ranker</b>: ⏳ {st.get('ranker_rows')}/"
                     f"{st.get('ranker_needs')} examples — tap a result button to feed it")
    lines.append(f"<b>Feedback learned from</b>: {ls.get('examples', 0)} "
                 f"({ls.get('positive', 0)} ✅ / {ls.get('negative', 0)} ❌) · "
                 f"associations {ls.get('associations', 0)} · vocab {ls.get('vocab', 0)}")
    lines.append(f"<b>Memory records</b>: {st.get('memory', 0)}")

    q = (args or "").strip()
    if not q:
        lines.append("")
        lines.append("Send <code>/ml whatsapp</code> to see the actual score "
                     "breakdown for a query — that is where the ML shows itself.")
        return await _reply(chat, msg, "\n".join(lines)[:telegram.MAX_MESSAGE])

    try:
        e = await asyncio.to_thread(ml.explain, q, cid)
    except Exception as ex:
        return await _reply(chat, msg, f"⚠️ {telegram.esc(ex)}")
    if not e.get("rows"):
        lines.append(f"\nNo candidate matched “{telegram.esc(q)}” over "
                     f"{e.get('files', 0)} file(s).")
        return await _reply(chat, msg, "\n".join(lines)[:telegram.MAX_MESSAGE])

    lines.append("")
    lines.append(f"🔎 <b>“{telegram.esc(q)}”</b> — {e['files']} file(s) in this chat")
    for r in e["rows"]:
        bits = [f"fuzzy {r['base']}", f"tfidf {r['tfidf']}", f"learned {r['learned']}"]
        if e.get("embed_on"):
            bits.append(f"embed {r.get('embed', 0.0)}")
        if r.get("ranker") is not None:
            bits.append(f"ranker {r['ranker']}")
        lines.append(f"• <b>{telegram.esc(r['name'][:36])}</b>\n   "
                     + " · ".join(bits) + f" → <b>{r['final']}</b>")
    lines.append("")
    lines.append("<i>fuzzy = spelling · tfidf = word importance · learned = your "
                 "past choices · embed = meaning · ranker = trained model</i>")
    await _reply(chat, msg, "\n".join(lines)[:telegram.MAX_MESSAGE])


async def cmd_remember(chat, msg, user, args):
    """Reply to a file (or send text) and Bonki learns about it in the background."""
    rt = msg.get("reply_to_message") or {}
    doc = rt.get("document") or {}
    cid = chat.get("id")

    if doc.get("file_id"):
        name = doc.get("file_name") or f"file_{rt.get('message_id')}"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        frm = rt.get("from") or {}
        db.upsert_file(chat_id=cid, message_id=rt.get("message_id"), name=name, ext=ext,
                       size=doc.get("file_size") or 0, mime=doc.get("mime_type") or "",
                       date=rt.get("date") or 0, sender_id=frm.get("id"),
                       sender_name=frm.get("first_name") or "",
                       caption=rt.get("caption") or "", source="remember")
        asyncio.create_task(memory.enrich_bg(cid, rt.get("message_id"), name,
                                             rt.get("caption") or ""))
        return await _reply(chat, msg,
                            f"🧠 Saved <b>{telegram.esc(name)}</b>.\n"
                            f"I'm learning about it in the background — tags, a short "
                            f"description and its meaning — so you can find it later by "
                            f"describing it.")

    text = (rt.get("text") or rt.get("caption") or args or "").strip()
    if text:
        memory.learn_from_text(cid, text)
        return await _reply(chat, msg, "🧠 Noted — I'll remember that.")

    return await _reply(chat, msg,
                        "Reply to a file or message with <code>/remember</code>, or "
                        "send <code>/remember something to keep</code>.")


async def _purge_targets(msg, args):
    """(ids, error) - shared by /purge and /spurge."""
    rt = msg.get("reply_to_message") or {}
    end = int(msg.get("message_id") or 0)
    arg = (args or "").strip()
    if arg.isdigit():
        n = max(1, min(int(arg), MAX_PURGE))
        return list(range(max(1, end - n + 1), end + 1)), None
    if rt.get("message_id"):
        start = int(rt["message_id"])
        if start > end:
            start, end = end, start
        return list(range(start, end + 1))[-MAX_PURGE:], None
    return None, ("Reply to the first message to purge from, or use "
                  "<code>/purge 20</code>.")


async def cmd_spurge(chat, msg, user, args):
    """Silent purge: deletes the same way, but posts nothing back."""
    if not await _need_admin(chat, msg, user):
        return
    ids, err = await _purge_targets(msg, args)
    if err:
        return await _reply(chat, msg, err)
    deleted = await _delete_ids(chat.get("id"), ids)
    log.info("silent purge: %d/%d in %s", deleted, len(ids), chat.get("id"))


async def cmd_dump(chat, msg, user, args):
    """Owner only: copy messages from THIS chat into another chat.

    It does not depend on the index. Telegram gives bots no way to *list* history,
    but a bot that is a member of a chat can copy a message if it knows the id -
    and message ids are sequential. So we walk the ids backwards from the command
    message and copy every one that still exists, skipping the gaps (deleted
    messages, service messages, ids that were never ours to read).

    Usage:
        /dump @target           last 100 messages
        /dump @target 500       last 500 (max 500)
        /dump -1001234567890    by numeric id
    """
    from . import approval
    if not approval.is_owner((user or {}).get("id")):
        return await _reply(chat, msg, "🔒 <b>Owner only.</b> /dump moves data "
                                       "between chats, so it needs the bot owner.")

    parts = (args or "").split()
    if not parts:
        return await _reply(
            chat, msg,
            "Usage: <code>/dump @targetchat [count]</code>\n\n"
            "Type it in the <b>source</b> chat; the argument is the "
            "<b>destination</b>. I copy messages from here to there — I don't need "
            "anything indexed.\n"
            "Default 100 messages, max 500 per run.")

    target = parts[0]
    count = 100
    if len(parts) > 1 and parts[1].isdigit():
        count = max(1, min(int(parts[1]), 500))

    end_id = int(msg.get("message_id") or 0)
    if not end_id:
        return await _reply(chat, msg, "I need a real message to start from — "
                                       "run this as a normal message in the source chat.")
    start_id = max(1, end_id - count + 1)

    await _reply(chat, msg,
                 f"📦 Copying messages <b>{start_id}–{end_id}</b> from here to "
                 f"<code>{telegram.esc(target)}</code>…\n"
                 f"This takes about {max(1, round(count * 0.35 / 60))} minute(s).")

    copied = skipped = blocked = 0
    for mid in range(end_id, start_id - 1, -1):
        if mid == end_id:                      # don't copy the /dump command itself
            continue
        try:
            await asyncio.to_thread(telegram.copy_message,
                                    from_chat_id=chat.get("id"),
                                    message_id=mid, chat_id=target)
            copied += 1
        except Exception as e:
            text = str(e).lower()
            # a missing id is normal - deleted or never existed
            if "not found" in text or "message_id_invalid" in text:
                skipped += 1
            else:
                blocked += 1
                if blocked <= 3:
                    log.warning("dump %s failed: %s", mid, e)
        await asyncio.sleep(0.35)

    note = f"✅ Copied <b>{copied}</b> message(s) to {telegram.esc(target)}"
    if skipped:
        note += f"\n• {skipped} id(s) had no message (deleted or empty)"
    if blocked:
        note += (f"\n• {blocked} failed — am I an admin in the destination, and "
                 f"can I read this chat?")
    if end_id - count + 1 < 1 and start_id == 1:
        note += "\n<i>Reached the start of the chat.</i>"
    else:
        note += (f"\n<i>Older messages? Run again with a higher count, e.g. "
                 f"<code>/dump {telegram.esc(target)} 500</code> — Telegram caps a "
                 f"bot at 500 per run.</i>")
    await _reply(chat, msg, note)


async def cmd_knowledge(chat, msg, user, args):
    """Search what Bonki has learned."""
    q = (args or "").strip()
    if not q:
        return await _reply(chat, msg,
                            f"🧠 I've learned <b>{db.count_memory()}</b> thing(s).\n"
                            f"Ask me: <code>/knowledge whatsapp</code>")
    hits = await asyncio.to_thread(memory.search, q, 6)
    if not hits:
        return await _reply(chat, msg, f"Nothing learned about “{telegram.esc(q)}” yet.")
    lines = [f"🧠 <b>{len(hits)} memory match(es)</b> for “{telegram.esc(q)}”"]
    for m in hits:
        meta = m.get("meta") or {}
        name = meta.get("name") or (m.get("text") or "")[:60]
        tags = " ".join(f"#{t}" for t in (meta.get("tags") or [])[:5])
        summary = meta.get("summary") or ""
        lines.append(f"• <b>{telegram.esc(name)}</b> {tags}")
        if summary:
            lines.append(f"   <i>{telegram.esc(summary[:120])}</i>")
    await _reply(chat, msg, "\n".join(lines)[:telegram.MAX_MESSAGE])


async def cmd_chats(chat, msg, user, args):
    """Every chat the bot is in, with usernames and links (owner only)."""
    from . import approval
    if not approval.is_owner((user or {}).get("id")):
        return await cmd_help(chat, msg, user, args)
    groups = db.list_groups()
    if not groups:
        return await _reply(chat, msg, "I'm not in any chat yet — add me to a "
                                       "group or channel.")

    def _handle(g):
        h = (g.get("handle") or "").lstrip("@").strip()
        return h if h and not h.lstrip("-").isdigit() else ""

    def block(title, rows, icon):
        if not rows:
            return
        lines.append("")
        lines.append(f"{icon} <b>{title}</b> ({len(rows)})")
        for g in rows:
            n = db.count_files(g.get("chat_id"))
            bits = []
            h = _handle(g)
            if h:
                bits.append(f"@{h}")
                bits.append(f'<a href="https://t.me/{h}">open</a>')
            else:
                bits.append(f"<code>{g.get('chat_id')}</code>")
            bits.append("✅" if g.get("approved") else "⏳ pending")
            bits.append(f"{n} files")
            if g.get("enabled") == 0:
                bits.append("⏸ off")
            lines.append(f"• <b>{telegram.esc(g.get('title') or g.get('chat_id'))}</b>"
                         f" — " + " · ".join(bits))

    lines = [f"📋 <b>Chats I'm in</b> — {len(groups)} total"]
    chans = [g for g in groups if (g.get("chat_type") == "channel")]
    grps = [g for g in groups if (g.get("chat_type") != "channel")]
    block("Channels", chans, "📢")
    block("Groups", grps, "👥")
    lines.append("")
    lines.append("<i>Pending chats need your approval before I index them.</i>")
    await _reply(chat, msg, "\n".join(lines)[:telegram.MAX_MESSAGE])


FULL_HELP = (
    "👑 <b>Bonki-Intelligence — owner help</b>\n\n"
    "<b>Files</b>\n"
    "• <i>Bonki, find &lt;file&gt;</i> · <i>Bonki scan this group</i>\n"
    "• <i>Bonki list apks</i> · <i>how many zips</i> · <i>group stats</i> · "
    "<i>summarise</i>\n\n"
    "<b>Moderation</b>\n"
    "• /ban · /unban · /kick · /mute · /unmute\n"
    "• /warn · /warns · /resetwarns\n"
    "• /del · /purge [n] · /pin · /unpin · /report\n"
    "• /lock · /unlock · /locks\n\n"
    "<b>Chat settings</b>\n"
    "• /save · /get · /clear · /notes\n"
    "• /setrules · /rules · /setwelcome · /welcome · /setgoodbye · /goodbye\n"
    "• /id · /info · /admins\n\n"
    "<b>Stickers &amp; quotes</b>\n"
    "• /q · /kang · /packs · /getsticker · /stickerid · /delsticker\n\n"
    "<b>Federations</b>\n"
    "• /newfed · /joinfed · /leavefed · /fedinfo · /fedlist · /fedadmins\n"
    "• /fban · /unfban · /fedbans · /addfedadmin · /rmfedadmin · /delfed\n\n"
    "<b>Special</b>\n"
    "• /tag @user N — N separate messages\n"
    "• /userbot — connect an account to scan history\n"
    "• /indexstatus — what I can and cannot see in a chat"
    "\n• /groups — every chat I'm in, with usernames"
    "\n• /remember — reply to a file; I learn about it in the background"
    "\n• /channelstat &lt;@chat&gt; — analytics card from my own data"
    "\n• /catalog — every chat I am in, ranked by activity"
    "\n• /ml [query] — see the ML layers and the real score breakdown"
    "\n• /skill — scheduled tasks (daily/weekly/in 2h)"
    "\n• /decide &lt;text&gt; · /decisions — remember what the group decided"
    "\n• /knowledge &lt;query&gt; — search what I have learned"
    "\n• /spurge — purge with no confirmation message"
    "\n• /dump &lt;@chat&gt; [count] — copy messages from here to there (owner)"
    "\n• /joinfed &lt;id&gt; @a @b — attach several chats at once"
    "\n• /fedauto on|off — auto-attach every new chat to one fed"
    "\n• /fbanlist csv|json · /importfbans — Rose-compatible export/import"
    "\n• /rosefed create|join — drive Rose itself via your user account (owner)"
    "\n• /fedsync on|off|push|pull — mirror this fed with a Rose fed"
    "\n• /fedbridge — account-free Rose bridge (publish + auto-import)"
)

PUBLIC_HELP = (
    "🤖 <b>Bonki-Intelligence</b> — group commands\n\n"
    "<b>Moderation</b> (admins only)\n"
    "• /ban · /unban · /kick · /mute · /unmute\n"
    "• /warn · /warns · /resetwarns\n"
    "• /del · /purge [n] · /pin · /unpin · /report\n\n"
    "<b>Info</b>\n"
    "• /id · /info · /admins · /rules\n\n"
    "<b>Notes</b>\n"
    "• /get · /notes\n\n"
    "<b>Search</b>\n"
    "• <i>Bonki, find &lt;file name&gt;</i> · <i>Bonki list files</i>\n\n"
    "🔒 Quotes, sticker packs, federations, tagging, history scanning and "
    "settings are reserved for the bot owner."
)


async def cmd_help(chat, msg, user, args):
    """Owner sees every command; everyone else sees the group command set and
    who to ask for the rest."""
    from . import approval
    if approval.is_owner((user or {}).get("id")):
        return await _reply(chat, msg, FULL_HELP)
    body = PUBLIC_HELP
    oid = approval.owner_id()
    if oid:
        body += (f"\n\n👑 <b>Owner ID</b>: <code>{oid}</code>\n"
                 f"Contact the owner to enable the full feature set.")
    else:
        body += "\n\n👑 No owner is configured for this bot yet."
    await _reply(chat, msg, body)


async def cmd_start(chat, msg, user, args):
    """Private welcome. Owner gets the control-panel greeting."""
    from . import approval
    if not approval.is_owner((user or {}).get("id")):
        return await cmd_help(chat, msg, user, args)
    try:
        from . import llm, userbot
        brain = llm.describe()
        ub = "on" if userbot.configured() else "off"
    except Exception:
        brain, ub = "?", "?"
    body = ("👋 <b>Welcome back, owner.</b>\n\n"
            f"📦 <b>{db.count_files()}</b> file(s) indexed\n"
            f"👥 <b>{len(db.list_groups())}</b> group(s)\n"
            f"🧠 brain: {telegram.esc(brain)}\n"
            f"👤 user account: {ub}\n\n"
            "Use /help for every command.\n"
            "Connect an account with /userbot to scan old messages.")
    await _reply(chat, msg, body)


COMMANDS = {
    "ban": cmd_ban, "unban": cmd_unban, "kick": cmd_kick,
    "mute": cmd_mute, "unmute": cmd_unmute,
    "warn": cmd_warn, "warns": cmd_warns, "resetwarns": cmd_resetwarns,
    "del": cmd_del, "purge": cmd_purge, "pin": cmd_pin, "unpin": cmd_unpin,
    "report": cmd_report,
    "save": cmd_save, "get": cmd_get, "clear": cmd_clear, "notes": cmd_notes,
    "setrules": cmd_setrules, "rules": cmd_rules,
    "setwelcome": cmd_setwelcome, "welcome": cmd_welcome,
    "setgoodbye": cmd_setgoodbye, "goodbye": cmd_goodbye,
    "id": cmd_id, "info": cmd_info, "admins": cmd_admins,
    "tag": cmd_tag, "help": cmd_help, "start": cmd_start,
    "indexstatus": cmd_indexstatus,
    "groups": cmd_chats, "chats": cmd_chats, "channels": cmd_chats,
    "remember": cmd_remember, "save": cmd_remember, "learn": cmd_remember,
    "channelstat": cmd_channelstat, "chstat": cmd_channelstat,
    "catalog": cmd_catalog, "channels": cmd_chats,
    "skill": cmd_skill, "skills": cmd_skill,
    "decide": cmd_decide, "decisions": cmd_decisions,
    "ml": cmd_ml, "mlstatus": cmd_ml, "brain": cmd_ml,
    "spurge": cmd_spurge, "silentpurge": cmd_spurge,
    "dump": cmd_dump, "knowledge": cmd_knowledge, "memory": cmd_knowledge,
}


# locks, federations, quote + sticker commands
COMMANDS.update({
    "lock": cmd_lock, "unlock": cmd_unlock, "locks": cmd_locks,
    "newfed": federation.cmd_newfed, "joinfed": federation.cmd_joinfed,
    "leavefed": federation.cmd_leavefed, "fedinfo": federation.cmd_fedinfo,
    "fedlist": federation.cmd_fedlist, "fedadmins": federation.cmd_fedadmins,
    "fban": federation.cmd_fban, "unfban": federation.cmd_unfban,
    "delfed": federation.cmd_delfed,
    "addfedadmin": federation.cmd_addfedadmin,
    "rmfedadmin": federation.cmd_rmfedadmin,
    "fedbans": federation.cmd_fedbans,
    "fedauto": federation.cmd_fedauto,
    "fbanlist": federation.cmd_fbanlist, "fbanexport": federation.cmd_fbanlist,
    "importfbans": federation.cmd_importfbans, "fedimport": federation.cmd_importfbans,
    "rosefed": federation.cmd_rosefed, "rose": federation.cmd_rosefed,
    "fedsync": federation.cmd_fedsync, "fedmirror": federation.cmd_fedsync,
    "fedbridge": federation.cmd_fedbridge,
    "q": stickers.cmd_quote, "quote": stickers.cmd_quote, "quotly": stickers.cmd_quote,
    "kang": stickers.cmd_kang, "getsticker": stickers.cmd_getsticker,
    "stickerid": stickers.cmd_stickerid, "packs": stickers.cmd_packs,
    "delsticker": stickers.cmd_delsticker,
})


async def handle_command(chat, msg, user, cmd, args):
    """Run a Rose command. Returns True if it was one of ours."""
    handler = COMMANDS.get((cmd or "").lower())
    if not handler:
        return False
    try:
        await handler(chat, msg, user or {}, args or "")
    except Exception as e:
        log.warning("rose /%s failed: %s", cmd, e)
        try:
            await _reply(chat, msg, f"⚠️ /{cmd} failed: {_esc(e)}")
        except Exception:
            pass
    return True
