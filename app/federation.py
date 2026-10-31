"""Federations - bans shared across a set of groups.

  /newfed <name>        create a federation (you become its owner)
  /joinfed <id|name>    attach THIS group to a federation (fed admin only)
  /leavefed             detach this group
  /fedinfo [id|name]    details: owner, groups, ban count
  /fedlist              federations you own or admin
  /fedadmins            admins of this group's federation
  /fban <user> [reason] ban a user across every group in the federation
  /unfban <user>        lift a federation ban
  /delfed <name>        delete a federation you own

Enforcement: when a federation-banned user posts in any group attached to that
federation, they are banned there and the message is removed.

IMPORTANT - honest limit: this is Bonki's **own** federation system. It cannot
interoperate with Rose's federations. Rose does not publish any API or export for
its federation data, so there is no supported way for another bot to read, join
or share Rose feds. Anyone claiming otherwise would be scraping private data.
"""
import asyncio
import logging
import re

from . import db, telegram

log = logging.getLogger("federation")

_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")


async def _reply(chat, msg, text):
    return await asyncio.to_thread(telegram.send_message, text, chat_id=chat.get("id"),
                                   reply_to=msg.get("message_id"))


async def _is_group_admin(chat_id, user_id):
    try:
        from . import approval
        if approval.is_owner(user_id):
            return True
    except Exception:
        pass
    try:
        r = await asyncio.to_thread(telegram.get_chat_member, chat_id, user_id)
        return (r.get("result") or {}).get("status") in ("administrator", "creator")
    except Exception:
        return False


def _is_fed_admin(fed, user_id):
    if not fed:
        return False
    uid = str(user_id)
    return uid == str(fed.get("owner")) or uid in [str(a) for a in (fed.get("admins") or [])]


def _target(msg, args):
    """(user_id_or_username, reason) from an explicit mention or a reply."""
    parts = (args or "").split(None, 1)
    if parts:
        a = parts[0]
        if a.startswith("@") or re.fullmatch(r"-?\d+", a.lstrip("-")):
            uid = int(a) if a.lstrip("-").isdigit() else a
            return uid, (parts[1] if len(parts) > 1 else "").strip()
    frm = ((msg.get("reply_to_message") or {}).get("from") or {})
    if frm.get("id"):
        return frm.get("id"), (args or "").strip()
    return None, ""


def _mention(uid, uname=None):
    if uname:
        return "@" + str(uname).lstrip("@")
    if isinstance(uid, str) and uid.startswith("@"):
        return uid                                  # already a username
    if uid:
        return f'<a href="tg://user?id={uid}">user</a>'
    return "user"


def _chat_username(msg):
    return ((msg.get("reply_to_message") or {}).get("from") or {}).get("username")


# ---------------------------------------------------------------- commands ---
async def cmd_newfed(chat, msg, user, args):
    name = (args or "").strip().split()[0] if (args or "").strip() else ""
    if not _NAME_RE.match(name):
        return await _reply(chat, msg, "Usage: <code>/newfed my_fed</code> "
                                       "(3–30 letters, digits or underscores).")
    fed = db.add_fed(name, user.get("id"))
    if not fed:
        return await _reply(chat, msg, f"A federation called <b>{name}</b> already exists.")
    await _reply(chat, msg,
                 f"✅ Federation <b>{name}</b> created (id <code>{fed['id']}</code>).\n"
                 f"Attach a group with <code>/joinfed {fed['id']}</code>.")


async def cmd_joinfed(chat, msg, user, args):
    key = (args or "").strip().split()[0] if (args or "").strip() else ""
    if not key:
        return await _reply(chat, msg, "Usage: <code>/joinfed &lt;id or name&gt;</code>")
    fed = db.get_fed(key) or db.get_fed_by_name(key)
    if not fed:
        return await _reply(chat, msg, "No such federation.")
    if not _is_fed_admin(fed, user.get("id")):
        return await _reply(chat, msg, "Only that federation's admins can attach a group.")
    if db.get_fed_for_chat(chat.get("id")):
        return await _reply(chat, msg, "This group is already in a federation — "
                                       "/leavefed first.")
    groups = list(fed.get("groups") or [])
    if str(chat.get("id")) not in [str(g) for g in groups]:
        groups.append(str(chat.get("id")))
    db.update_fed(fed["id"], {"groups": groups})
    await _reply(chat, msg, f"🔗 This group joined <b>{fed['name']}</b>.")


async def cmd_leavefed(chat, msg, user, args):
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return await _reply(chat, msg, "This group isn't in a federation.")
    if not (_is_fed_admin(fed, user.get("id"))
            or await _is_group_admin(chat.get("id"), user.get("id"))):
        return await _reply(chat, msg, "Only group admins or fed admins can do that.")
    groups = [str(g) for g in (fed.get("groups") or []) if str(g) != str(chat.get("id"))]
    db.update_fed(fed["id"], {"groups": groups})
    await _reply(chat, msg, f"🔓 This group left <b>{fed['name']}</b>.")


async def cmd_fedinfo(chat, msg, user, args):
    key = (args or "").strip().split()[0] if (args or "").strip() else ""
    fed = (db.get_fed(key) or db.get_fed_by_name(key)) if key else db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return await _reply(chat, msg, "No federation here. Use /newfed to make one.")
    await _reply(chat, msg,
                 f"🏛 <b>{fed['name']}</b> (id <code>{fed['id']}</code>)\n"
                 f"owner: <code>{fed.get('owner')}</code>\n"
                 f"admins: {len(fed.get('admins') or [])} · groups: "
                 f"{len(fed.get('groups') or [])} · bans: {len(fed.get('bans') or [])}")


async def cmd_fedlist(chat, msg, user, args):
    mine = [f for f in db.list_feds() if _is_fed_admin(f, user.get("id"))]
    if not mine:
        return await _reply(chat, msg, "You don't own or admin any federation.")
    lines = ["🏛 <b>Your federations</b>"]
    for f in mine[:20]:
        lines.append(f"• <b>{f['name']}</b> — id <code>{f['id']}</code>, "
                     f"{len(f.get('groups') or [])} groups, {len(f.get('bans') or [])} bans")
    await _reply(chat, msg, "\n".join(lines))


async def cmd_fedadmins(chat, msg, user, args):
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return await _reply(chat, msg, "This group isn't in a federation.")
    admins = fed.get("admins") or []
    await _reply(chat, msg, "👑 <b>Fed admins</b>\n" +
                 "\n".join(f"• <code>{a}</code>" for a in admins[:30]))


async def cmd_fban(chat, msg, user, args):
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return await _reply(chat, msg, "This group isn't in a federation.")
    if not (_is_fed_admin(fed, user.get("id"))
            or await _is_group_admin(chat.get("id"), user.get("id"))):
        return await _reply(chat, msg, "Only fed admins or group admins can fedban.")
    uid, reason = _target(msg, args)
    if not uid:
        return await _reply(chat, msg, "Reply to a user or give @username.")
    db.fed_ban(fed["id"], str(uid), reason or "no reason", user.get("id"))

    done, failed = 0, 0
    for gid in (fed.get("groups") or []):
        try:
            await asyncio.to_thread(telegram.ban_chat_member, gid, uid)
            done += 1
        except Exception as e:
            failed += 1
            log.warning("fedban failed in %s: %s", gid, e)
    await _reply(chat, msg,
                 f"🔨 Fedbanned {_mention(uid, _chat_username(msg))} in "
                 f"<b>{fed['name']}</b>\n"
                 f"banned in {done} group(s)"
                 + (f", {failed} failed (bot not admin there)" if failed else "")
                 + (f"\nreason: {telegram.esc(reason)}" if reason else ""))


async def cmd_unfban(chat, msg, user, args):
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return await _reply(chat, msg, "This group isn't in a federation.")
    if not (_is_fed_admin(fed, user.get("id"))
            or await _is_group_admin(chat.get("id"), user.get("id"))):
        return await _reply(chat, msg, "Only fed admins or group admins can do that.")
    uid, _ = _target(msg, args)
    if not uid:
        return await _reply(chat, msg, "Reply to a user or give @username.")
    ok = db.fed_unban(fed["id"], str(uid))
    for gid in (fed.get("groups") or []):
        try:
            await asyncio.to_thread(telegram.unban_chat_member, gid, uid)
        except Exception:
            pass
    await _reply(chat, msg, "✅ Unbanned from the federation." if ok
                 else "That user had no federation ban.")


async def cmd_delfed(chat, msg, user, args):
    name = (args or "").strip().split()[0] if (args or "").strip() else ""
    fed = db.get_fed_by_name(name)
    if not fed:
        return await _reply(chat, msg, "No such federation.")
    if str(fed.get("owner")) != str(user.get("id")):
        return await _reply(chat, msg, "Only the federation owner can delete it.")
    db.delete_fed(fed["id"])
    await _reply(chat, msg, f"🗑 Federation <b>{fed['name']}</b> deleted.")


# ------------------------------------------------------------- enforcement ---
async def enforce(chat, msg, user):
    """Ban a federation-banned user in this group. True if they were removed."""
    uid = (user or {}).get("id")
    if not uid:
        return False
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return False
    # a fedban may have been recorded by numeric id OR by @username - match both
    keys = {str(uid).lower()}
    un = (user or {}).get("username")
    if un:
        keys.add("@" + str(un).lstrip("@").lower())
    if not any(db.fed_is_banned(fed["id"], k) for k in keys):
        return False
    if await _is_group_admin(chat.get("id"), uid):
        return False
    try:
        await asyncio.to_thread(telegram.ban_chat_member, chat.get("id"), uid)
        log.info("fedban enforced in %s for %s", chat.get("id"), uid)
    except Exception as e:
        log.warning("fedban enforcement failed: %s", e)
    try:
        await asyncio.to_thread(telegram.delete_message, chat.get("id"), msg.get("message_id"))
    except Exception:
        pass
    return True
