"""Owner approval - the verify gate.

Anyone can add a bot to a group, so nothing happens in a new group until the
**owner** approves it. The approval button lives *inside Telegram*: it is posted
in the group and DMed to the owner, so the owner can verify without ever opening
the dashboard. Only the owner's tap counts; anyone else gets refused.
"""
import asyncio
import logging
import os

from . import db, telegram

log = logging.getLogger("approval")


def owner_id():
    raw = (db.get_settings().get("owner_id") or os.environ.get("OWNER_ID") or "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


def is_owner(uid):
    o = owner_id()
    try:
        return o is not None and uid is not None and int(uid) == o
    except (TypeError, ValueError):
        return False


def require_approval():
    return str(db.get_settings().get("require_approval") or "1") == "1"


def gate(chat_id, username=None):
    """True if this chat still needs approval before Bonki will work there.

    Deliberately returns False when no owner is configured - otherwise the button
    is owner-only and nobody could ever approve, leaving the bot permanently
    silent in every group. Verification only bites once an Owner ID is set.
    """
    if not require_approval() or owner_id() is None:
        return False
    return not db.is_approved(chat_id, username)


def keyboard(gid):
    """The inline Approve / Reject buttons (owner-only in effect).

    Keyed by the group's record id, which is stable regardless of whether the
    group is stored under a numeric id or an @username.
    """
    return {"inline_keyboard": [[
        {"text": "✅ Approve this group", "callback_data": f"ap|y|{gid}"},
        {"text": "🚫 Reject", "callback_data": f"ap|n|{gid}"},
    ]]}


async def notify_owner(text, reply_markup=None):
    """DM the owner. Needs the owner to have sent the bot /start at least once."""
    o = owner_id()
    if not o:
        return False
    try:
        await asyncio.to_thread(telegram.send_message, text, chat_id=o,
                                reply_markup=reply_markup)
        return True
    except Exception as e:
        log.warning("could not DM the owner (%s): %s", o, e)
        return False


async def announce(chat, by=""):
    """Tell the group + the owner that this group needs approving (once)."""
    cid = chat.get("id")
    title = chat.get("title") or str(cid)
    g = db.get_group_by_chat(cid, chat.get("username"))
    if not g:
        g = db.get_group(db.add_group(cid, title, chat.get("username") or ""))
    if not g or g.get("approval_notified"):
        return
    db.update_group(g["id"], {"approval_notified": 1})
    db.log_approval(cid, title, chat.get("username") or "", "request", by=by)
    await asyncio.to_thread(db.flush, True)

    try:
        await asyncio.to_thread(
            telegram.send_message,
            "🛡️ <b>Approval needed.</b> This group isn't verified yet, so I won't "
            "index or answer here. Only the bot's owner can approve it — tap below.",
            chat_id=cid, reply_markup=keyboard(g["id"]))
    except Exception as e:
        log.warning("could not post approval prompt in %s: %s", cid, e)

    await notify_owner(
        f"🛡️ <b>Bonki was added to a new group</b>\n"
        f"<b>{telegram.esc(title)}</b>\n<code>{cid}</code>\n\n"
        f"Approve it to start indexing?", reply_markup=keyboard(g["id"]))
