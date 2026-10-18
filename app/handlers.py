"""Route incoming Telegram updates.

For every message the bot receives it:
  * auto-registers the group so it shows up on the dashboard
  * indexes any file posted (live capture), when enabled
  * checks whether Bonki is being addressed (wake word / @mention / reply to bot)
    and, if so, hands the instruction to the agent

All Telegram / state-store calls are offloaded to a thread, so a slow network
call can never freeze the bot and stop it replying.
"""
import asyncio
import logging
import re

from . import agent, db, indexer, telegram

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


async def _send(text, chat_id, reply_to=None):
    return await asyncio.to_thread(telegram.send_message, text, chat_id=chat_id,
                                   reply_to=reply_to)


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


async def handle_update(update):
    msg = (update.get("message") or update.get("edited_message")
           or update.get("channel_post"))
    if not msg:
        return
    chat = msg.get("chat") or {}
    frm = msg.get("from") or {}
    ctype = chat.get("type")

    if frm.get("is_bot"):
        return                              # never react to bots (incl. itself)

    if ctype in ("group", "supergroup", "channel"):
        if not db.get_group_by_chat(chat.get("id")):
            db.add_group(chat.get("id"), chat.get("title") or "",
                         chat.get("username") or "")

    if ctype in ("group", "supergroup", "channel") \
            and str(db.get_settings().get("index_live") or "1") == "1":
        try:
            indexer.capture_from_message(chat, msg)
            await asyncio.to_thread(db.flush)
        except Exception as e:
            log.warning("live index failed: %s", e)

    text = msg.get("text") or msg.get("caption") or ""

    if ctype in ("group", "supergroup"):
        if text and is_addressed(msg, text):
            clean = strip_address(text) or text
            try:
                await agent.handle(chat, msg, frm, clean)
            except Exception as e:
                log.warning("agent failed: %s", e)
                await _send(f"Sorry, something went wrong: {telegram.esc(e)}",
                            chat.get("id"), reply_to=msg.get("message_id"))
        return

    if ctype == "private":
        if not _allowed_dm(frm.get("id")):
            await _send("Hi! Add your Telegram user id under Settings → Allowed DM "
                        "ids, or use me inside a group. (Get your id from @userinfobot.)",
                        chat.get("id"), reply_to=msg.get("message_id"))
            return
        if text:
            try:
                await agent.handle(chat, msg, frm, strip_address(text) or text)
            except Exception as e:
                log.warning("agent(DM) failed: %s", e)
                await _send(f"Sorry, something went wrong: {telegram.esc(e)}",
                            chat.get("id"), reply_to=msg.get("message_id"))
