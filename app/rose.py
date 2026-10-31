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
import re
import time

from . import db, federation, stickers, telegram

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
    "• /userbot — connect an account to scan history"
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
