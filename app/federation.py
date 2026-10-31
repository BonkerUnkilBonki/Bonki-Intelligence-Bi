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
import csv
import io
import json
import logging
import os
import re
import tempfile
import time

from . import db, telegram, userbot

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
    """Attach one or MANY chats to a federation.

        /joinfed 1                  this chat
        /joinfed 1 @chan @other     several by username
        /joinfed 1 -100123 -100456  several by id
    """
    parts = (args or "").split()
    if not parts:
        return await _reply(chat, msg, "Usage: <code>/joinfed &lt;id&gt; [@chat @chat2 …]</code>")
    fed = db.get_fed(parts[0]) or db.get_fed_by_name(parts[0])
    if not fed:
        return await _reply(chat, msg, "No such federation.")
    if not _is_fed_admin(fed, user.get("id")):
        return await _reply(chat, msg, "Only that federation's admins can attach chats.")

    # the chat you are typing in is always included (unless this is a DM),
    # plus any extra chats you name
    targets = list(parts[1:])
    if chat.get("type") in ("group", "supergroup", "channel"):
        targets.insert(0, str(chat.get("id")))
    if not targets:
        return await _reply(chat, msg, "Name at least one chat: "
                                       "<code>/joinfed 1 @mychannel</code>")
    groups = [str(g) for g in (fed.get("groups") or [])]
    added, already, unknown = [], [], []
    for t in targets:
        key = t.lstrip("@")
        known = None
        for g in db.list_groups():
            if (g.get("handle") or "").lstrip("@").lower() == key.lower():
                known = g
                break
            if str(g.get("chat_id")) == key:
                known = g
                break
        cid = str(known.get("chat_id")) if known else key
        if cid in groups:
            already.append(cid)
            continue
        groups.append(cid)
        added.append(cid)

    db.update_fed(fed["id"], {"groups": groups})
    lines = [f"🔗 <b>{fed['name']}</b> now has {len(groups)} chat(s)."]
    if added:
        lines.append("Added: " + ", ".join(f"<code>{telegram.esc(a)}</code>" for a in added[:15]))
    if already:
        lines.append("Already in: " + ", ".join(f"<code>{telegram.esc(a)}</code>" for a in already[:10]))
    if unknown:
        lines.append("Not found: " + ", ".join(telegram.esc(u) for u in unknown[:10]))
    await _reply(chat, msg, "\n".join(lines))


async def cmd_fedauto(chat, msg, user, args):
    """Auto-attach every NEW chat to one federation."""
    fed = db.get_fed_for_chat(chat.get("id"))
    arg = (args or "").strip().lower()
    if arg not in ("on", "off", ""):
        return await _reply(chat, msg, "Usage: <code>/fedauto on</code> or "
                                       "<code>/fedauto off</code>")
    if not fed:
        return await _reply(chat, msg, "This group isn't in a federation — "
                                       "/joinfed &lt;id&gt; first.")
    if not _is_fed_admin(fed, user.get("id")):
        return await _reply(chat, msg, "Only fed admins can change this.")
    if arg == "":
        cur = db.get_settings().get("auto_fed") or ""
        state = f"<b>{cur}</b>" if cur else "off"
        return await _reply(chat, msg, f"Auto-attach to new chats: {state}")
    if arg == "off":
        db.set_settings({"auto_fed": ""})
        return await _reply(chat, msg, "⏹ New chats will no longer be auto-attached.")
    db.set_settings({"auto_fed": str(fed["id"])})
    await _reply(chat, msg,
                 f"✅ Every new chat I'm added to will join <b>{fed['name']}</b> "
                 f"automatically.")


async def cmd_fbanlist(chat, msg, user, args):
    """Export the fed's ban list in Rose's own formats, so it can be imported there."""
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return await _reply(chat, msg, "This group isn't in a federation.")
    if not (_is_fed_admin(fed, user.get("id"))
            or await _is_group_admin(chat.get("id"), user.get("id"))):
        return await _reply(chat, msg, "Only fed admins can export the ban list.")
    bans = fed.get("bans") or []
    if not bans:
        return await _reply(chat, msg, "No bans to export.")

    fmt = (args or "").strip().lower()
    if fmt not in ("csv", "json", "minicsv"):
        fmt = "csv"

    if fmt == "json":
        body = "\n".join(json.dumps({"user_id": b.get("user"), "reason": b.get("reason") or ""},
                                    ensure_ascii=False) for b in bans)
        fname = f"{fed['name']}_fbanlist.json"
    else:
        buf = io.StringIO()
        w = csv.writer(buf)
        if fmt == "csv":
            w.writerow(["id", "firstname", "lastname", "username", "reason"])
            for b in bans:
                u = str(b.get("user") or "")
                if u.startswith("@"):
                    w.writerow([u, "", "", u.lstrip("@"), b.get("reason") or ""])
                else:
                    w.writerow([u, "", "", "", b.get("reason") or ""])
        else:                                   # minicsv: id,reason only
            for b in bans:
                w.writerow([b.get("user"), b.get("reason") or ""])
        body = buf.getvalue()
        fname = f"{fed['name']}_fbanlist.csv"

    path = os.path.join(tempfile.gettempdir(), f"fb_{fed['id']}_{int(asyncio.get_event_loop().time())}")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        await asyncio.to_thread(telegram.send_document, path, fname,
                                f"{len(bans)} ban(s) · import into Rose with "
                                f"/importfbans", chat.get("id"), msg.get("message_id"))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def cmd_importfbans(chat, msg, user, args):
    """Import bans from a Rose-format CSV/JSON document (reply to the file)."""
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return await _reply(chat, msg, "This group isn't in a federation.")
    if not (_is_fed_admin(fed, user.get("id"))
            or await _is_group_admin(chat.get("id"), user.get("id"))):
        return await _reply(chat, msg, "Only fed admins can import bans.")
    rt = msg.get("reply_to_message") or {}
    doc = rt.get("document") or {}
    if not doc.get("file_id"):
        return await _reply(chat, msg,
                            "Reply to a <b>.csv</b> or <b>.json</b> ban list with "
                            "<code>/importfbans</code>.\n"
                            "Rose's <code>/fbanlist csv</code> output imports directly.")
    try:
        info = await asyncio.to_thread(telegram.get_file, doc["file_id"])
        fpath = (info.get("result") or {}).get("file_path")
        raw = await asyncio.to_thread(telegram.download_file, fpath)
        text = raw.decode("utf-8", "ignore")
    except Exception as e:
        return await _reply(chat, msg, f"⚠️ Couldn't fetch the file: {telegram.esc(e)}")

    rows = _parse_banlist(text)
    if not rows:
        return await _reply(chat, msg, "⚠️ That file had no readable ban rows.")

    imported = skipped = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        uid = (r.get("user_id") or r.get("id") or r.get("user")
               or r.get("userid") or "").strip() if isinstance(
                   r.get("user_id") or r.get("id") or r.get("user") or "", str) else (
                   r.get("user_id") or r.get("id") or r.get("user"))
        if not uid:
            skipped += 1
            continue
        reason = r.get("reason") or "imported"
        db.fed_ban(fed["id"], str(uid), str(reason), user.get("id"))
        imported += 1

    await _reply(chat, msg,
                 f"✅ Imported <b>{imported}</b> ban(s) into <b>{fed['name']}</b>"
                 + (f"\n• {skipped} row(s) had no id and were skipped" if skipped else ""))


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
    if sync_on():
        db.queue_fed_push(fed["id"], str(uid), reason or "no reason")
    if db.get_settings().get("fed_sync_chat"):
        # account-free path: keep the published file current
        try:
            await publish_banlist(db.get_fed(fed["id"]) or fed)
        except Exception as e:
            log.info("auto-publish after fban failed: %s", e)

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


async def cmd_addfedadmin(chat, msg, user, args):
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return await _reply(chat, msg, "This group isn't in a federation.")
    if str(fed.get("owner")) != str(user.get("id")):
        return await _reply(chat, msg, "Only the federation owner can add admins.")
    uid, _ = _target(msg, args)
    if not uid:
        return await _reply(chat, msg, "Reply to a user or give @username.")
    admins = [str(a) for a in (fed.get("admins") or [])]
    if str(uid) not in admins:
        admins.append(str(uid))
        db.update_fed(fed["id"], {"admins": admins})
    await _reply(chat, msg, f"👑 {_mention(uid, _chat_username(msg))} is now a fed admin.")


async def cmd_rmfedadmin(chat, msg, user, args):
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return await _reply(chat, msg, "This group isn't in a federation.")
    if str(fed.get("owner")) != str(user.get("id")):
        return await _reply(chat, msg, "Only the federation owner can remove admins.")
    uid, _ = _target(msg, args)
    if not uid:
        return await _reply(chat, msg, "Reply to a user or give @username.")
    if str(uid) == str(fed.get("owner")):
        return await _reply(chat, msg, "The owner can't be removed.")
    admins = [str(a) for a in (fed.get("admins") or []) if str(a) != str(uid)]
    db.update_fed(fed["id"], {"admins": admins})
    await _reply(chat, msg, "✅ Fed admin removed.")


async def cmd_fedbans(chat, msg, user, args):
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return await _reply(chat, msg, "This group isn't in a federation.")
    bans = fed.get("bans") or []
    if not bans:
        return await _reply(chat, msg, f"<b>{fed['name']}</b> has no bans.")
    lines = [f"🔨 <b>{fed['name']}</b> — {len(bans)} ban(s)"]
    for b in bans[:30]:
        lines.append(f"• <code>{telegram.esc(str(b.get('user')))}</code> — "
                     f"{telegram.esc(str(b.get('reason') or 'no reason'))[:60]}")
    await _reply(chat, msg, "\n".join(lines))


# ------------------------------------------------------- Rose bridge --------
# Rose publishes no API, so a bot cannot create or join a Rose federation
# directly. What IS possible: your connected user account can talk to
# @MissRose_bot exactly as you would, and Bonki relays her answer. That is
# automation of your own account, not an integration - and it may breach Rose's
# terms, so it is owner-only, rate-limited, and off unless /userbot is connected.
ROSE = "@MissRose_bot"
_ROSE_LAST = {"t": 0.0}
_ROSE_COOLDOWN = 20          # seconds between commands sent to Rose


async def cmd_rosefed(chat, msg, user, args):
    from . import approval
    if not approval.is_owner((user or {}).get("id")):
        return await _reply(chat, msg, "🔒 Owner only — this drives your user account.")
    if not userbot.is_running():
        return await _reply(
            chat, msg,
            "🔌 <b>This needs a user account.</b>\n\n"
            "Telegram blocks bots from messaging other bots (403: <i>bot can't send "
            "messages to bots</i>), and Rose publishes no API — so without an account "
            "there is no way for me to send <code>/newfed</code> or "
            "<code>/joinfed</code> to Rose. That is a platform limit, not a setting.\n\n"
            "<b>What works with no account at all:</b>\n"
            "• Export from Rose with <code>/fbanlist csv</code>, then forward that "
            "file here — I import it into your federation <b>automatically</b>.\n"
            "• <code>/importfbans</code> does the same by hand.\n"
            "• <code>/fbanlist csv</code> here produces a file Rose can import.\n"
            "• Bonki's own federations need no account at all.")

    parts = (args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    commands = {
        "create": f"/newfed {rest}",
        "new": f"/newfed {rest}",
        "join": f"/joinfed {rest}",
        "leave": "/leavefed",
        "info": "/fedinfo",
        "admins": "/fedadmins",
        "banlist": f"/fbanlist {rest}".strip(),
        "rules": "/fedrules",
        "raw": rest,
    }
    if sub not in commands:
        return await _reply(
            chat, msg,
            "🇷🇺 <b>Rose federation bridge</b> (owner, needs /userbot)\n\n"
            "<code>/rosefed create &lt;name&gt;</code> — create a Rose fed\n"
            "<code>/rosefed join &lt;fed_id&gt;</code> — join this chat to a Rose fed\n"
            "<code>/rosefed leave</code> — leave the current fed\n"
            "<code>/rosefed info</code> · <code>admins</code> · <code>rules</code>\n"
            "<code>/rosefed banlist [csv|json]</code> — export from Rose\n"
            "<code>/rosefed raw &lt;any command&gt;</code> — escape hatch\n\n"
            "I send the command to @MissRose_bot from your account and relay her "
            "reply. One command every 20s, because Rose rate-limits hard.")

    cmd = commands[sub]
    if not cmd or cmd in ("/newfed", "/joinfed"):
        return await _reply(chat, msg, "That needs an argument — see /rosefed.")

    now = time.time()
    if now - _ROSE_LAST["t"] < _ROSE_COOLDOWN:
        left = int(_ROSE_COOLDOWN - (now - _ROSE_LAST["t"]))
        return await _reply(chat, msg, f"⏳ Rose rate limit — try again in {left}s.")
    _ROSE_LAST["t"] = now

    await _reply(chat, msg, f"📨 Sending <code>{telegram.esc(cmd)}</code> to Rose…")
    try:
        reply = await userbot.ask(ROSE, cmd, wait=10.0)
    except Exception as e:
        return await _reply(chat, msg, f"⚠️ Couldn't reach Rose: {telegram.esc(e)}")
    if not reply:
        return await _reply(chat, msg,
                            "⚠️ Rose didn't answer in time. She may be rate-limiting "
                            "you, or the account may not have started her — open "
                            f"{ROSE} once and press Start.")
    await _reply(chat, msg, "🇷🇺 <b>Rose says:</b>\n" + telegram.esc(reply[:1200]))


async def auto_import_doc(chat, msg, user, doc):
    """Auto-import a Rose-exported ban list dropped into a federated chat.

    This is the hands-free half of Rose interop that needs no user account: you
    export from Rose once, forward the file here, and it lands in your federation.
    Only the owner or a fed admin can trigger it, so a random member cannot
    fedban people by posting a file.
    """
    name = (doc.get("file_name") or "").lower()
    if not re.search(r"(fban|ban)[^/]*\.(csv|json)$", name):
        return False
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return False
    uid = (user or {}).get("id")
    if not (_is_fed_admin(fed, uid) or await _is_group_admin(chat.get("id"), uid)):
        return False
    try:
        info = await asyncio.to_thread(telegram.get_file, doc["file_id"])
        fpath = (info.get("result") or {}).get("file_path")
        raw = await asyncio.to_thread(telegram.download_file, fpath)
        text = raw.decode("utf-8", "ignore")
    except Exception as e:
        await _reply(chat, msg, f"⚠️ Couldn't read that ban list: {telegram.esc(e)}")
        return True

    rows = _parse_banlist(text)
    if not rows:
        return False
    imported = 0
    for r in rows:
        val = r.get("user_id") or r.get("id") or r.get("user")
        if not val:
            continue
        db.fed_ban(fed["id"], str(val), str(r.get("reason") or "imported"), uid)
        imported += 1
    await _reply(chat, msg,
                 f"✅ Auto-imported <b>{imported}</b> Rose ban(s) into "
                 f"<b>{fed['name']}</b>.")
    log.info("auto-imported %d bans into fed %s", imported, fed["id"])
    return True


def _parse_banlist(text):
    """Parse Rose's CSV or NDJSON ban list into a list of dicts."""
    stripped = (text or "").strip()
    if not stripped:
        return []
    rows = []
    if stripped.startswith("{") or stripped.startswith("["):
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, list):
                rows += [o for o in obj if isinstance(o, dict)]
            elif isinstance(obj, dict):
                rows.append(obj)
        return rows
    try:
        return [r for r in csv.DictReader(io.StringIO(stripped)) if isinstance(r, dict)]
    except Exception:
        return []


# ------------------------------------------------- Rose two-way mirror ------
# Bonki and Rose can share one ban list, but the transport is constrained: bots
# cannot message bots (403), so every hop goes through the connected user account.
#   Bonki -> Rose : /fban <id> <reason> sent from your account
#   Rose  -> Bonki: /fbanlist json asked for, the file she replies with imported
# Without /userbot neither direction can move automatically.
def sync_on():
    return str(db.get_settings().get("fed_sync_rose") or "") == "1"


async def push_pending(fed):
    """Send this fed's queued bans to Rose."""
    pending = db.list_fed_pushes(fed["id"])
    sent = 0
    for row in pending[:10]:                 # small batches, Rose rate-limits
        cmd = f"/fban {row['user']} {row.get('reason') or 'no reason'}".strip()
        try:
            await userbot.ask(ROSE, cmd, wait=6.0)
            db.drop_fed_push(fed["id"], row["user"])
            sent += 1
        except Exception as e:
            log.warning("push to Rose failed for %s: %s", row.get("user"), e)
            break
        await asyncio.sleep(4)
    return sent


async def pull_from_rose(fed):
    """Ask Rose for her ban list and import it into this fed."""
    text, blob = await userbot.ask_document(ROSE, "/fbanlist json", wait=20.0)
    payload = ""
    if blob:
        payload = blob.decode("utf-8", "ignore")
    elif text and text.strip().startswith(("{", "[")):
        payload = text
    if not payload:
        return 0
    rows = _parse_banlist(payload)
    imported = 0
    for r in rows:
        val = r.get("user_id") or r.get("id") or r.get("user")
        if not val:
            continue
        db.fed_ban(fed["id"], str(val), str(r.get("reason") or "rose"), "rose")
        imported += 1
    if imported:
        db.set_settings({"fed_sync_last_pull": str(int(time.time()))})
    return imported


async def cmd_fedsync(chat, msg, user, args):
    """Mirror this federation with a Rose federation."""
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return await _reply(chat, msg, "This chat isn't in a federation — "
                                       "/joinfed &lt;id&gt; first.")
    if not _is_fed_admin(fed, user.get("id")):
        return await _reply(chat, msg, "Only fed admins can change sync.")

    arg = (args or "").strip().lower()
    if arg in ("on", "off"):
        db.set_settings({"fed_sync_rose": "1" if arg == "on" else ""})
        if arg == "off":
            return await _reply(chat, msg, "⏹ Mirroring with Rose stopped.")
        if not userbot.is_running():
            return await _reply(
                chat, msg,
                "⚠️ Sync is switched on, but it can't move yet.\n\n"
                "Bots can't message bots (403), so both directions run through your "
                "user account. Connect it with /userbot and it will start within "
                "15 minutes.")
        return await _reply(chat, msg,
                            "🔁 Mirroring with Rose is on.\n"
                            "• Bonki bans → sent to Rose\n"
                            "• Rose bans → pulled every 15 minutes\n"
                            "Rose rate-limits hard, so a batch is 10 per cycle.")

    if arg == "pull":
        if not userbot.is_running():
            return await _reply(chat, msg, "🔌 Needs /userbot to reach Rose.")
        await _reply(chat, msg, "📥 Asking Rose for her ban list…")
        try:
            n = await pull_from_rose(fed)
        except Exception as e:
            return await _reply(chat, msg, f"⚠️ {telegram.esc(e)}")
        return await _reply(chat, msg, f"✅ Imported <b>{n}</b> ban(s) from Rose.")

    if arg == "push":
        if not userbot.is_running():
            return await _reply(chat, msg, "🔌 Needs /userbot to reach Rose.")
        await _reply(chat, msg, "📤 Sending queued bans to Rose…")
        try:
            n = await push_pending(fed)
        except Exception as e:
            return await _reply(chat, msg, f"⚠️ {telegram.esc(e)}")
        return await _reply(chat, msg, f"✅ Sent <b>{n}</b> ban(s) to Rose.")

    queued = len(db.list_fed_pushes(fed["id"]))
    last = db.get_settings().get("fed_sync_last_pull") or "never"
    try:
        last = time.strftime("%d %b %H:%M", time.localtime(int(last)))
    except (TypeError, ValueError):
        pass
    await _reply(chat, msg,
                 f"🔁 <b>Rose mirror — {fed['name']}</b>\n"
                 f"• sync: {'✅ on' if sync_on() else '⏹ off'}\n"
                 f"• user account: {'✅ connected' if userbot.is_running() else '❌ needed'}\n"
                 f"• queued to send: {queued}\n"
                 f"• last pull from Rose: {last}\n\n"
                 f"<code>/fedsync on|off</code> · <code>push</code> · <code>pull</code>")


async def sync_loop():
    """Background: push queued bans and pull Rose's list, every 15 minutes."""
    log.info("fed sync loop started")
    while True:
        try:
            if sync_on() and userbot.is_running():
                for g in db.list_groups():
                    fed = db.get_fed_for_chat(g.get("chat_id"))
                    if not fed:
                        continue
                    if db.list_fed_pushes(fed["id"]):
                        await push_pending(fed)
                    await pull_from_rose(fed)
                    break                    # one fed per cycle, keep it gentle
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("fed sync loop error: %s", e)
        await asyncio.sleep(900)


# ------------------------------------- Account-free bridge: publish + import --
# With no user account the ONLY channel between two bots is a file a human moves.
# So Bonki maximises what it can do alone:
#   * it publishes its ban list as a Rose-importable file, automatically, whenever
#     a ban changes - into a chat you nominate
#   * it imports a Rose file the moment one is forwarded in (auto_import_doc)
# The one remaining human step is forwarding that file to Rose and tapping
# /importfbans there. Telegram's bot-to-bot block makes that unavoidable.
def _banlist_bytes(fed, fmt="csv"):
    bans = fed.get("bans") or []
    if fmt == "json":
        body = "\n".join(json.dumps({"user_id": b.get("user"),
                                     "reason": b.get("reason") or ""},
                                    ensure_ascii=False) for b in bans)
        return body.encode("utf-8"), f"{fed['name']}_fbanlist.json"
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "firstname", "lastname", "username", "reason"])
    for b in bans:
        u = str(b.get("user") or "")
        if u.startswith("@"):
            w.writerow([u, "", "", u.lstrip("@"), b.get("reason") or ""])
        else:
            w.writerow([u, "", "", "", b.get("reason") or ""])
    return buf.getvalue().encode("utf-8"), f"{fed['name']}_fbanlist.csv"


async def publish_banlist(fed, target=None):
    """Post the fed's ban list as a file Rose can import."""
    target = target or (db.get_settings().get("fed_sync_chat") or "")
    if not target:
        return False
    data, fname = _banlist_bytes(fed, "csv")
    path = os.path.join(tempfile.gettempdir(),
                        f"pub_{fed['id']}_{int(time.time())}.csv")
    try:
        with open(path, "wb") as fh:
            fh.write(data)
        await asyncio.to_thread(
            telegram.send_document, path, fname,
            f"{len(fed.get('bans') or [])} ban(s) — forward to Rose and reply "
            f"/importfbans there", target)
        return True
    except Exception as e:
        log.warning("publish banlist failed: %s", e)
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def cmd_fedbridge(chat, msg, user, args):
    """Account-free bridge: publish Bonki's list, auto-import Rose's."""
    fed = db.get_fed_for_chat(chat.get("id"))
    if not fed:
        return await _reply(chat, msg, "This chat isn't in a federation — "
                                       "/joinfed &lt;id&gt; first.")
    if not _is_fed_admin(fed, user.get("id")):
        return await _reply(chat, msg, "Only fed admins can configure the bridge.")

    parts = (args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("channel", "set"):
        if not rest:
            return await _reply(chat, msg, "Usage: <code>/fedbridge channel "
                                           "@mylogchat</code>")
        db.set_settings({"fed_sync_chat": rest})
        return await _reply(chat, msg,
                            f"📤 Ban-list updates will be posted to "
                            f"<code>{telegram.esc(rest)}</code>.\n"
                            f"Forward that file to Rose and reply "
                            f"<code>/importfbans</code> there.")

    if sub in ("file", "now", "publish"):
        if not (db.get_settings().get("fed_sync_chat") or ""):
            return await _reply(chat, msg, "Set a target first: "
                                           "<code>/fedbridge channel @mylogchat</code>")
        ok = await publish_banlist(fed)
        return await _reply(chat, msg, "✅ Ban list published."
                            if ok else "⚠️ Couldn't publish — am I admin there?")

    if sub == "off":
        db.set_settings({"fed_sync_chat": ""})
        return await _reply(chat, msg, "⏹ Auto-publishing stopped.")

    target = db.get_settings().get("fed_sync_chat") or "not set"
    await _reply(chat, msg,
                 f"🌉 <b>Account-free Rose bridge — {fed['name']}</b>\n\n"
                 f"<b>Bonki → Rose</b>\n"
                 f"• publishing to: <code>{telegram.esc(target)}</code>\n"
                 f"• set it: <code>/fedbridge channel @chat</code>\n"
                 f"• post now: <code>/fedbridge file</code>\n"
                 f"• every new fedban re-publishes the list automatically\n\n"
                 f"<b>Rose → Bonki</b>\n"
                 f"• forward any <code>*fban*.csv</code> / <code>.json</code> here\n"
                 f"  and it imports itself, no command needed\n\n"
                 f"<i>The last hop to Rose is one tap: forward the file to her and "
                 f"reply /importfbans. Telegram blocks bots from messaging bots "
                 f"(403), so no bot can do that hop for you.</i>")


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
