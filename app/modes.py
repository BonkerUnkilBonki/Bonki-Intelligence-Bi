"""Two bots in one: Bonki Find and Bonki Leech, chosen per chat.

  FIND   everything Bonki already does - index files, search, share, moderate.
  LEECH  watch a GitHub repo and dump every new release (changelog, tag, type,
         APK) into a chosen channel topic.

The mode is per chat, so the same bot can be a finder in one group and a leecher
in a channel. When the bot is added somewhere it asks which one you want; it can be
changed later from the DM with /mode.
"""
import asyncio
import logging

from . import db, telegram

log = logging.getLogger("modes")

FIND = "find"
LEECH = "leech"
MODES = {
    FIND: ("🔎 Bonki Find", "search, share and manage files"),
    LEECH: ("📦 Bonki Leech", "auto-post GitHub releases to a topic"),
}


def get_mode(chat_id):
    """'find' or 'leech'. Defaults to find - the behaviour that already existed."""
    m = str((db.get_chatconf(chat_id) or {}).get("mode") or FIND).lower()
    return m if m in MODES else FIND


def set_mode(chat_id, mode):
    db.set_chatconf(chat_id, {"mode": mode if mode in MODES else FIND})
    return get_mode(chat_id)


def is_leech(chat_id):
    return get_mode(chat_id) == LEECH


def keyboard(chat_id=None):
    cur = get_mode(chat_id) if chat_id else None
    rows = []
    for key, (label, desc) in MODES.items():
        mark = "✅ " if cur == key else ""
        rows.append([{"text": f"{mark}{label}", "callback_data": f"mode:{key}"}])
    return {"inline_keyboard": rows}


def ask_text(chat_title=""):
    return ("🤖 <b>How should I work here?</b>" +
            (f"\n\nChat: <b>{telegram.esc(chat_title)}</b>" if chat_title else "") +
            "\n\n<b>🔎 Bonki Find</b> — index files, search and share them.\n"
            "<b>📦 Bonki Leech</b> — watch a GitHub repo and post every new release "
            "into a channel topic.\n\n"
            "<i>You can change this any time with /mode.</i>")


async def ask(chat_id, title=""):
    """Ask which mode to use (inline buttons). Safe to call repeatedly."""
    try:
        await asyncio.to_thread(telegram.send_message, ask_text(title),
                                chat_id=chat_id, reply_markup=keyboard(chat_id))
        return True
    except Exception as e:
        log.info("mode prompt failed in %s: %s", chat_id, e)
        return False


# --------------------------------------------------------------- DM config --
async def cmd_mode(chat, msg, user, args):
    """Show or change the mode for a chat."""
    from . import approval, rose
    cid = chat.get("id")
    want = (args or "").strip().lower()

    if want in MODES:
        if not await rose._is_admin(cid, (user or {}).get("id"), chat.get("type")):
            return await rose._reply(chat, msg, "Admins only.")
        m = set_mode(cid, want)
        await asyncio.to_thread(db.flush, True)
        label, desc = MODES[m]
        extra = ""
        if m == LEECH:
            extra = ("\n\nNext: <code>/leech repo owner/name</code>, then run "
                     "<code>/leech here</code> inside the topic you want releases in.")
        return await rose._reply(chat, msg,
                                 f"✅ This chat is now <b>{label}</b> — {desc}.{extra}")

    if not approval.is_owner((user or {}).get("id")) and \
            not await rose._is_admin(cid, (user or {}).get("id"), chat.get("type")):
        return await rose._reply(chat, msg, "Admins only.")

    label, desc = MODES[get_mode(cid)]
    await asyncio.to_thread(
        telegram.send_message,
        f"🤖 <b>Current mode: {label}</b>\n<i>{desc}</i>\n\nTap to change:",
        chat_id=cid, reply_to=msg.get("message_id"), reply_markup=keyboard(cid))
