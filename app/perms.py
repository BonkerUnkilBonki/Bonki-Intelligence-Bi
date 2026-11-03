"""Does the bot have admin rights here? Cached, because it decides what to skip.

Without admin a bot can still index, search and share files in a group - it just
cannot delete, mute, ban or purge. Rather than firing API calls that are certain
to fail (and filling the log), Bonki checks once, remembers, and simply runs in
read-only mode.
"""
import asyncio
import logging
import time

from . import db, telegram

log = logging.getLogger("perms")

_TTL = 600                     # re-check every 10 minutes
_CACHE = {}                    # chat_id -> (is_admin, checked_at)


async def bot_admin(chat_id, force=False):
    """True if the bot is an administrator in this chat (cached)."""
    key = str(chat_id)
    now = time.time()
    hit = _CACHE.get(key)
    if hit and not force and (now - hit[1]) < _TTL:
        return hit[0]
    try:
        from . import handlers
        bot_id = handlers._bot_identity.get("id")
        if not bot_id:
            return False
        r = await asyncio.to_thread(telegram.get_chat_member, chat_id, bot_id)
        status = (r.get("result") or {}).get("status")
        ok = status in ("administrator", "creator")
    except Exception as e:
        log.info("admin check failed for %s: %s", chat_id, e)
        ok = False
    _CACHE[key] = (ok, now)
    return ok


def forget(chat_id):
    _CACHE.pop(str(chat_id), None)


async def needs_admin(chat, msg, what="that"):
    """True when the bot lacks admin - and has told the user once."""
    cid = chat.get("id")
    if await bot_admin(cid):
        return False
    try:
        from . import rose
        await rose._reply(
            chat, msg,
            f"🔓 <b>{what}</b> needs me to be an admin here.\n"
            f"Everything else still works — searching, sharing files and "
            f"<code>/diag</code>. Add me as an admin (or disable privacy mode in "
            f"@BotFather) to unlock moderation.")
    except Exception:
        pass
    return True
