"""Browse every indexed file from a DM and pull the ones you want.

A chat list is fine for finding something you remember, but you cannot click a
message in a group you are not in. This gives a paged menu in the DM: tap a file
and Bonki copies it to you.

Privacy rule: a file is only sent if the person asking is a member of the chat it
came from (or is the bot owner). Membership is checked with the Bot API at the
moment of the tap, not cached, so leaving a group revokes access immediately.
"""
import asyncio
import logging

from . import db, telegram

log = logging.getLogger("browse")

PAGE = 8


def _all_files(chat_id=None):
    files = db.list_files(chat_id=chat_id)
    files.sort(key=lambda f: -(f.get("date") or 0))
    return files


def _label(f, i):
    name = (f.get("name") or "file")[:34]
    return f"{i}. {name}"


def _allowed_chats(user_id):
    """Chat ids this user may see files from (owner: all). Cached per call."""
    from . import approval
    if approval.is_owner(user_id):
        return None                      # None = no restriction
    chats = {f.get("chat_id") for f in _all_files()}
    ok = set()
    for c in list(chats)[:15]:           # bounded: a handful of API calls
        if _is_member(c, user_id):
            ok.add(c)
    return ok


def menu(chat_id=None, page=0, query="", user_id=None):
    """(text, keyboard) for one page of the file browser."""
    files = _all_files(chat_id)
    if user_id is not None:
        allow = _allowed_chats(user_id)
        if allow is not None:
            files = [f for f in files if f.get("chat_id") in allow]
    if query:
        q = query.lower()
        files = [f for f in files if q in (f.get("name") or "").lower()]
    if not files:
        return ("📂 No files indexed" + (" here" if chat_id else "") +
                ". Post some and I'll add them.", None)

    pages = max(1, (len(files) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE
    chunk = files[start:start + PAGE]

    rows = []
    for i, f in enumerate(chunk, start=start + 1):
        rows.append([{
            "text": _label(f, i),
            "callback_data": f"file:{f.get('chat_id')}:{f.get('message_id')}",
        }])

    nav = []
    if page > 0:
        nav.append({"text": "⬅️ Prev", "callback_data": f"browse:{page - 1}"})
    nav.append({"text": f"{page + 1}/{pages}", "callback_data": "browse:noop"})
    if page < pages - 1:
        nav.append({"text": "Next ➡️", "callback_data": f"browse:{page + 1}"})
    if len(nav) > 1:
        rows.append(nav)

    head = "📂 <b>Files</b>"
    if query:
        head += f" matching “{telegram.esc(query)}”"
    head += f" — {len(files)} file(s)\n<i>Tap one and I'll send it here.</i>"
    return head, {"inline_keyboard": rows}


def _is_member(chat_id, user_id):
    try:
        r = telegram.get_chat_member(chat_id, user_id)
        status = (r.get("result") or {}).get("status") or ""
        return status in ("creator", "administrator", "member", "restricted")
    except Exception as e:
        log.info("membership check failed for %s/%s: %s", chat_id, user_id, e)
        return False


async def send_file(cq, chat_id, message_id):
    """Copy one indexed file into the DM, if the user is allowed to see it."""
    from . import approval
    user = (cq.get("from") or {})
    uid = user.get("id")
    dm = ((cq.get("message") or {}).get("chat") or {}).get("id")

    allowed = approval.is_owner(uid)
    if not allowed:
        allowed = await asyncio.to_thread(_is_member, chat_id, uid)
    if not allowed:
        await asyncio.to_thread(telegram.answer_callback_query, cq.get("id"),
                                "You're not in that group, so I can't send it.", True)
        return

    try:
        await asyncio.to_thread(telegram.copy_message, chat_id, message_id, dm)
        await asyncio.to_thread(telegram.answer_callback_query, cq.get("id"), "Sent ✅")
    except Exception as e:
        log.info("file send failed: %s", e)
        await asyncio.to_thread(
            telegram.answer_callback_query, cq.get("id"),
            "Couldn't send that file — it may have been deleted.", True)


async def cmd_files(chat, msg, user, args):
    """List indexed files as buttons (works in DM and in a group)."""
    from . import rose
    cid = chat.get("id")
    q = (args or "").strip()
    # in a group, show that group's files; in a DM, show everything
    scope = None if chat.get("type") == "private" else cid
    text, kb = menu(scope, 0, q, user_id=(user or {}).get("id"))
    await asyncio.to_thread(telegram.send_message, text, chat_id=cid,
                            reply_to=msg.get("message_id"), reply_markup=kb)
