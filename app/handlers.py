"""Route incoming Telegram updates.

For every update the bot:
  * notices when it is added to a group and asks the owner to verify it
    (approval lives inside Telegram - buttons in the group and in the owner's DM)
  * refuses to index, scan or answer in a group that is not yet approved
  * indexes files posted in an approved group (live capture)
  * checks whether Bonki is being addressed and hands the instruction to the agent
  * serves the owner's DM menu (/pending, /approve, /reject)

All Telegram / state-store calls are offloaded to a thread, so a slow network
call can never freeze the bot and stop it replying.
"""
import asyncio
import logging
import re

from . import agent, approval, db, federation, indexer, rose, telegram

log = logging.getLogger("handlers")

_bot_identity = {"username": None, "id": None}


async def init_identity():
    """Fetch the bot's own username/id once at startup (blocking call -> thread)."""
    try:
        me = await asyncio.to_thread(lambda: telegram.get_me().get("result") or {})
        _bot_identity["username"] = me.get("username")
        _bot_identity["id"] = me.get("id")
    except Exception as e:
        log.warning("getMe failed: %s", e)


def _identity():
    return _bot_identity


async def _send(text, chat_id, reply_to=None, reply_markup=None):
    return await asyncio.to_thread(telegram.send_message, text, chat_id=chat_id,
                                   reply_to=reply_to, reply_markup=reply_markup)


def _wake_words():
    raw = db.get_settings().get("wake_words") or ""
    return [w.strip().lower() for w in re.split(r"[,\s]+", raw) if w.strip()]


def is_addressed(msg, text):
    s = db.get_settings()
    ident = _identity()
    rt = msg.get("reply_to_message") or {}
    if ident["id"] and (rt.get("from") or {}).get("id") == ident["id"]:
        return True
    low = (text or "").lower()
    if ident["username"] and f"@{ident['username'].lower()}" in low:
        return True
    if str(s.get("require_mention") or "1") != "1":
        return bool(low.strip())
    for w in _wake_words():
        if re.search(r"(^|[^a-z0-9])" + re.escape(w) + r"([^a-z0-9]|$)", low):
            return True
    return False


def strip_address(text):
    """Remove the wake word / @mention / leading command punctuation."""
    out = text or ""
    ident = _identity()
    if ident["username"]:
        out = re.sub(r"@" + re.escape(ident["username"]), " ", out, flags=re.I)
    for w in _wake_words():
        out = re.sub(r"(?i)(^|[^a-z0-9])" + re.escape(w) + r"([^a-z0-9]|$)", " ", out)
    out = re.sub(r"^[\s,.:;!?\-]+", "", out)
    return re.sub(r"\s+", " ", out).strip()


def _allowed_dm(user_id):
    ids = [x.strip() for x in (db.get_settings().get("allow_user_ids") or "").split(",") if x.strip()]
    return user_id is not None and str(user_id) in ids


async def _send_public_dm(chat, msg):
    """Answer a non-owner DM. Tries a threaded reply, then a plain send."""
    oid = approval.owner_id()
    tail = (f"\U0001F451 <b>Owner ID</b>: <code>{oid}</code> - contact the owner to "
            f"unlock the full feature set." if oid
            else "\U0001F451 No owner is configured for this bot yet.")
    body = ("\U0001F916 <b>Bonki-Intelligence</b>\n" + tail + "\n\n"
            "In a group I can:\n"
            "- find files: <i>Bonki, find app apk</i>\n"
            "- moderate: /ban /mute /warn /purge /pin\n"
            "- notes, rules, info: /get /notes /rules /id\n\n"
            "Use /help to see everything available to you.")
    for extra in ({"reply_to": msg.get("message_id")}, {}):
        try:
            await _send(body, chat.get("id"), **extra)
            return
        except Exception as e:
            log.info("public DM send failed (%s), retrying", e)


# ------------------------------------------------------------- membership ---
async def _notify_owner(text):
    """DM the bot owner (best effort - never raises)."""
    oid = approval.owner_id()
    if not oid:
        return
    try:
        await asyncio.to_thread(telegram.send_message, text, chat_id=oid)
    except Exception as e:
        log.info("owner DM failed: %s", e)


async def _handle_membership(cm):
    """The bot was added to (or removed from) a chat."""
    chat = cm.get("chat") or {}
    if chat.get("type") not in ("group", "supergroup", "channel"):
        return
    title = chat.get("title") or str(chat.get("id"))
    status = ((cm.get("new_chat_member") or {}).get("status") or "")
    who = cm.get("from") or {}
    by = who.get("first_name") or who.get("username") or str(who.get("id") or "")

    if status in ("member", "administrator", "creator"):
        is_new = not db.get_group_by_chat(chat.get("id"), chat.get("username"))
        if is_new:
            row = db.add_group(chat.get("id"), chat.get("title") or "",
                               chat.get("username") or "")
            try:
                db.update_group(row, {"chat_type": chat.get("type") or ""})
            except Exception:
                pass
        if chat.get("type") == "channel":
            # greet in the channel itself
            try:
                await asyncio.to_thread(
                    telegram.send_message,
                    "✅ <b>Bonki-Intelligence</b> is now in this channel.\n"
                    "Post files here and I'll index them automatically.",
                    chat_id=chat.get("id"))
            except Exception as e:
                log.info("channel greeting failed: %s", e)
            if is_new:
                await _notify_owner(f"📢 Added to <b>channel</b> “{telegram.esc(title)}” "
                                    f"by {telegram.esc(by)}.")
        elif is_new:
            await _notify_owner(f"➕ Added to <b>group</b> “{telegram.esc(title)}” "
                                f"by {telegram.esc(by)}.")
        # auto-attach every new chat to the federation the owner nominated
        try:
            auto = (db.get_settings().get("auto_fed") or "").strip()
            if auto and is_new:
                fed = db.get_fed(auto)
                if fed:
                    groups = [str(g) for g in (fed.get("groups") or [])]
                    if str(chat.get("id")) not in groups:
                        groups.append(str(chat.get("id")))
                        db.update_fed(fed["id"], {"groups": groups})
                        await _notify_owner(
                            f"🔗 Auto-attached “{telegram.esc(title)}” to "
                            f"<b>{telegram.esc(fed['name'])}</b>.")
        except Exception as e:
            log.info("auto-fed attach failed: %s", e)

        if approval.gate(chat.get("id"), chat.get("username")):
            await approval.announce(chat, by=by)   # verify button + DM owner
    elif status in ("left", "kicked"):
        g = db.get_group_by_chat(chat.get("id"), chat.get("username"))
        if g:
            db.update_group(g["id"], {"enabled": 0})
            await asyncio.to_thread(db.flush, True)
        await _notify_owner(f"➖ Removed from “{telegram.esc(title)}” ({status}).")


# ------------------------------------------------------------- callbacks ----
async def _handle_approval(cq):
    parts = (cq.get("data") or "").split("|", 2)
    if len(parts) < 3:
        return
    act, cid = parts[1], parts[2]
    m = cq.get("message") or {}
    chat = m.get("chat") or {}
    if not approval.is_owner((cq.get("from") or {}).get("id")):
        await asyncio.to_thread(telegram.answer_callback_query, cq.get("id"),
                                "Only the bot owner can approve this group.", True)
        return
    g = db.get_group(int(cid)) if str(cid).isdigit() else db.get_group_by_chat(cid)
    if act == "y":
        if g:
            db.update_group(g["id"], {"approved": 1, "enabled": 1})
        await asyncio.to_thread(telegram.answer_callback_query, cq.get("id"), "Approved ✅")
        if m.get("message_id"):
            try:
                await asyncio.to_thread(telegram.edit_message_reply_markup,
                                        chat.get("id"), m.get("message_id"), None)
            except Exception:
                pass
        if g and str(db.get_settings().get("announce_approval") or "1") == "1":
            try:
                await asyncio.to_thread(
                    telegram.send_message,
                    "✅ <b>Verified.</b> I can work here now — say "
                    "<i>“Bonki, scan this group”</i> to index its files.",
                    chat_id=g.get("chat_id"))
            except Exception as e:
                log.warning("could not announce approval in %s: %s", cid, e)
        db.log_activity("approve", (g or {}).get("chat_id") or cid, "group approved by owner")
        db.log_approval((g or {}).get("chat_id") or cid, (g or {}).get("title") or cid,
                        (g or {}).get("handle") or "", "approve",
                        by=str((cq.get("from") or {}).get("id") or "owner"))
    else:
        if g:
            db.update_group(g["id"], {"approved": 0, "enabled": 0})
        await asyncio.to_thread(telegram.answer_callback_query, cq.get("id"), "Rejected 🚫")
        if m.get("message_id"):
            try:
                await asyncio.to_thread(telegram.edit_message_reply_markup,
                                        chat.get("id"), m.get("message_id"), None)
            except Exception:
                pass
        if g:
            try:                                 # leave the group we were pushed into
                await asyncio.to_thread(telegram.leave_chat, g.get("chat_id"))
            except Exception as e:
                log.warning("could not leave %s: %s", cid, e)
        db.log_activity("reject", (g or {}).get("chat_id") or cid, "group rejected by owner")
        db.log_approval((g or {}).get("chat_id") or cid, (g or {}).get("title") or cid,
                        (g or {}).get("handle") or "", "reject",
                        by=str((cq.get("from") or {}).get("id") or "owner"))
    await asyncio.to_thread(db.flush, True)


async def _handle_callback(cq):
    data = cq.get("data") or ""
    if data.startswith("ap|"):
        await _handle_approval(cq)
        return
    if not data.startswith("fb|"):
        return
    parts = data.split("|")
    await asyncio.to_thread(telegram.answer_callback_query, cq.get("id"), "Learning from that…")
    m = cq.get("message") or {}
    chat = m.get("chat") or {}
    if parts[1] == "n":
        await agent.handle_feedback(parts[2], -1, False, chat)
    else:
        try:
            idx = int(parts[3])
        except (IndexError, ValueError):
            idx = -1
        await agent.handle_feedback(parts[2], idx, True, chat)
    if m.get("message_id"):                      # clear the keyboard
        try:
            await asyncio.to_thread(telegram.edit_message_reply_markup,
                                    chat.get("id"), m.get("message_id"), None)
        except Exception:
            pass


# --------------------------------------------------------- owner DM menu ----
async def _owner_command(text, chat, msg):
    t = (text or "").strip()
    low = t.lower()
    if low.startswith(("/pending", "/groups", "/verify")):
        pend = [g for g in db.list_groups() if not g.get("approved")]
        if not pend:
            await _send("No groups are waiting for approval.", chat.get("id"))
            return
        await _send(f"🛡️ <b>{len(pend)} group(s) waiting for approval</b> — tap to verify:",
                    chat.get("id"))
        for g in pend:
            await _send(f"• <b>{telegram.esc(g.get('title') or g.get('chat_id'))}</b>\n"
                        f"<code>{g.get('chat_id')}</code>",
                        chat.get("id"), reply_markup=approval.keyboard(g.get("id")))
        return
    if low.startswith(("/approve", "/reject")):
        parts = t.split()
        if len(parts) < 2:
            await _send("Usage: <code>/approve &lt;chat_id&gt;</code>", chat.get("id"))
            return
        g = db.get_group_by_chat(parts[1])
        if not g:
            await _send(f"No group with id <code>{telegram.esc(parts[1])}</code>. "
                        f"Use /pending to list them.", chat.get("id"))
            return
        ok = low.startswith("/approve")
        db.update_group(g["id"], {"approved": 1 if ok else 0, "enabled": 1 if ok else 0})
        db.log_approval(g.get("chat_id"), g.get("title"), g.get("handle") or "",
                        "approve" if ok else "reject", by="owner (DM)")
        await asyncio.to_thread(db.flush, True)
        await _send(("✅ Approved " if ok else "🚫 Rejected ")
                    + telegram.esc(g.get("title") or parts[1]), chat.get("id"))
        return
    await _send("👑 <b>Owner menu</b>\n"
                "/pending — groups waiting for approval (with verify buttons)\n"
                "/approve &lt;chat_id&gt; · /reject &lt;chat_id&gt;\n\n"
                "When I'm added to a new group I'll message you here to verify it.",
                chat.get("id"))


# --------------------------------------------------------------- routing ----
async def handle_update(update):
    if update.get("my_chat_member"):
        try:
            await _handle_membership(update["my_chat_member"])
        except Exception as e:
            log.warning("membership handling failed: %s", e)
        return
    if update.get("callback_query"):
        try:
            await _handle_callback(update["callback_query"])
        except Exception as e:
            log.warning("callback failed: %s", e)
        return

    msg = (update.get("message") or update.get("edited_message")
           or update.get("channel_post"))
    if not msg:
        return
    chat = msg.get("chat") or {}
    frm = msg.get("from") or {}
    ctype = chat.get("type")
    text = msg.get("text") or msg.get("caption") or ""

    if frm.get("is_bot"):
        return                              # never react to bots (incl. itself)

    if ctype == "private":
        # A DM must NEVER be silent: a non-owner messaging the bot and getting
        # nothing at all reads as a broken bot - which is exactly what happened.
        try:
            if text.startswith("/"):
                parts = text.split(None, 1)
                cmd = parts[0].lstrip("/").split("@")[0].lower()
                args = parts[1] if len(parts) > 1 else ""
                if await rose.handle_command(chat, msg, frm, cmd, args):
                    return
                if approval.is_owner(frm.get("id")):
                    await _owner_command(text, chat, msg)      # /pending, /verify
                    return
                await _send_public_dm(chat, msg)
                return
            if approval.is_owner(frm.get("id")):
                await _send("Use /help for every command, /groups for the chats I'm "
                            "in, or /start for a summary.", chat.get("id"),
                            reply_to=msg.get("message_id"))
            else:
                await _send_public_dm(chat, msg)
        except Exception as e:
            log.warning("private handler failed: %s", e)
            try:
                await _send("Sorry - something went wrong. Try /start.", chat.get("id"))
            except Exception:
                pass
        return

    if ctype in ("group", "supergroup", "channel"):
        if not db.get_group_by_chat(chat.get("id"), chat.get("username")):
            db.add_group(chat.get("id"), chat.get("title") or "",
                         chat.get("username") or "")

        if approval.gate(chat.get("id"), chat.get("username")):   # not verified -> do nothing
            await approval.announce(chat)
            if ctype in ("group", "supergroup") and text and is_addressed(msg, text):
                await _send("🛡️ This group isn't verified yet — the bot's owner has "
                            "to approve it before I can index or answer here.",
                            chat.get("id"), reply_to=msg.get("message_id"))
            return

        # moderation first: federation bans and active locks can remove the
        # message before we index or answer it.
        try:
            if await federation.enforce(chat, msg, frm):
                return
            if await rose.enforce_locks(chat, msg, frm):
                return
        except Exception as e:
            log.warning("moderation check failed: %s", e)

        # A Rose ban list dropped into a federated chat imports itself - this is
        # the hands-free Rose interop that needs no user account.
        doc = msg.get("document") or {}
        if doc.get("file_id"):
            try:
                if await federation.auto_import_doc(chat, msg, frm, doc):
                    return
            except Exception as e:
                log.info("auto banlist import failed: %s", e)

        if str(db.get_settings().get("index_live") or "1") == "1":
            try:
                indexer.capture_from_message(chat, msg)
                asyncio.create_task(asyncio.to_thread(db.flush))   # don't block the reply
            except Exception as e:
                log.warning("live index failed: %s", e)

        # Rose-style commands need NO wake word - that's what makes it behave
        # like a normal group-management bot in every approved chat.
        if ctype in ("group", "supergroup") and text.startswith("/"):
            parts = text.split(None, 1)
            cmd = parts[0].lstrip("/").split("@")[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            if await rose.handle_command(chat, msg, frm, cmd, args):
                return
            try:                       # /scan, /find, /list, /stats still work
                await agent.handle(chat, msg, frm, text)
            except Exception as e:
                log.warning("agent (command) failed: %s", e)
            return

        # welcome new members
        members = msg.get("new_chat_members") or []
        if members:
            try:
                await rose.welcome_new(chat, msg, members)
            except Exception as e:
                log.warning("welcome failed: %s", e)

        if ctype in ("group", "supergroup") and text and is_addressed(msg, text):
            clean = strip_address(text) or text
            try:
                await agent.handle(chat, msg, frm, clean)
            except Exception as e:
                log.warning("agent failed: %s", e)
                await _send(f"Sorry, something went wrong: {telegram.esc(e)}",
                            chat.get("id"), reply_to=msg.get("message_id"))
        return
