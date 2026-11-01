"""Anti-spam: join CAPTCHA, word blocklist, antiflood.

These are the three protections every serious group bot ships and Bonki was
missing. All local - no API, no external service.

  * **CAPTCHA** - a new member is muted on join and must tap a button. Bots can't
    tap, so join-floods stop before they post anything. Members who never verify
    are kicked after the timeout.
  * **Blocklist** - case-insensitive word/phrase triggers; admins are exempt.
  * **Antiflood** - N messages inside T seconds gets the sender muted.

Config lives in the per-chat settings (`chatconf`), so each group is independent.
"""
import asyncio
import logging
import time

from . import db, telegram

log = logging.getLogger("security")

CAPTCHA_TIMEOUT = 300          # seconds before an unverified joiner is kicked
_flood_state = {}              # (chat_id, user_id) -> [timestamps]


# ------------------------------------------------------------------ captcha --
def captcha_on(chat_id):
    return bool((db.get_chatconf(chat_id) or {}).get("captcha"))


async def start_captcha(chat, msg, user):
    """Mute a new joiner and post the verify button."""
    cid, uid = chat.get("id"), (user or {}).get("id")
    if not cid or not uid:
        return False
    if not captcha_on(cid):
        return False
    try:
        await asyncio.to_thread(telegram.restrict_chat_member, cid, uid,
                                True, int(time.time()) + 3600)
    except Exception as e:
        log.info("captcha mute failed (am I admin?): %s", e)
        return False

    name = user.get("first_name") or "there"
    try:
        await asyncio.to_thread(
            telegram.send_message,
            f"👋 Welcome, <b>{telegram.esc(name)}</b>.\n"
            f"Tap the button below within {CAPTCHA_TIMEOUT // 60} minutes so I know "
            f"you're human.",
            chat_id=cid,
            reply_markup={"inline_keyboard": [[
                {"text": "✅ I'm human", "callback_data": f"cap:{uid}"}]]})
    except Exception as e:
        log.info("captcha message failed: %s", e)

    asyncio.create_task(_captcha_timeout(cid, uid))
    return True


async def _captcha_timeout(cid, uid):
    await asyncio.sleep(CAPTCHA_TIMEOUT)
    if not db.get_chatconf(cid).get("captcha_pending"):
        return
    pending = db.get_chatconf(cid).get("captcha_pending") or []
    if str(uid) not in [str(x) for x in pending]:
        return
    try:
        await asyncio.to_thread(telegram.ban_chat_member, cid, uid)
        await asyncio.to_thread(telegram.unban_chat_member, cid, uid)   # kick
    except Exception as e:
        log.info("captcha kick failed: %s", e)
    mark_pending(cid, uid, False)


def mark_pending(cid, uid, add=True):
    conf = db.get_chatconf(cid)
    pending = [str(x) for x in (conf.get("captcha_pending") or [])]
    if add and str(uid) not in pending:
        pending.append(str(uid))
    elif not add:
        pending = [x for x in pending if x != str(uid)]
    db.set_chatconf(cid, {"captcha_pending": pending})


async def verify_captcha(chat_id, user_id):
    """Unmute after the button tap. Returns True if it was pending."""
    conf = db.get_chatconf(chat_id)
    pending = [str(x) for x in (conf.get("captcha_pending") or [])]
    if str(user_id) not in pending:
        return False
    try:
        await asyncio.to_thread(telegram.restrict_chat_member, chat_id, user_id,
                                False, None)
    except Exception as e:
        log.info("captcha unmute failed: %s", e)
    mark_pending(chat_id, user_id, False)
    return True


# ---------------------------------------------------------------- blocklist --
def _blocklist(chat_id):
    return [str(w).lower() for w in ((db.get_chatconf(chat_id) or {}).get("blocklist") or [])]


async def check_blocklist(chat, msg, user):
    """Delete (and mute) a message containing a blocked word. Admins exempt."""
    cid = chat.get("id")
    words = _blocklist(cid)
    if not words:
        return False
    text = (msg.get("text") or msg.get("caption") or "").lower()
    if not text:
        return False
    hit = next((w for w in words if w and w in text), None)
    if not hit:
        return False
    from . import rose
    if await rose._is_admin(cid, (user or {}).get("id")):
        return False
    try:
        await asyncio.to_thread(telegram.delete_message, cid, msg.get("message_id"))
    except Exception as e:
        log.info("blocklist delete failed: %s", e)
    mode = (db.get_chatconf(cid) or {}).get("blocklist_mode") or "delete"
    if mode.startswith("mute") and (user or {}).get("id"):
        minutes = 60
        if mode[4:].strip().isdigit():
            minutes = int(mode[4:].strip())
        try:
            await asyncio.to_thread(telegram.restrict_chat_member, cid,
                                    user.get("id"), True,
                                    int(time.time()) + minutes * 60)
        except Exception as e:
            log.info("blocklist mute failed: %s", e)
    try:
        await asyncio.to_thread(telegram.send_message,
                                f"🚫 <b>{telegram.esc(user.get('first_name') or 'User')}</b> "
                                f"that word isn't allowed here.",
                                chat_id=cid)
    except Exception:
        pass
    return True


# ---------------------------------------------------------------- antiflood --
async def check_flood(chat, msg, user):
    """Mute someone sending N messages within T seconds."""
    cid = chat.get("id")
    conf = db.get_chatconf(cid) or {}
    limit = int(conf.get("flood_limit") or 0)
    window = int(conf.get("flood_window") or 10)
    if limit <= 0:
        return False
    uid = (user or {}).get("id")
    if not uid:
        return False
    from . import rose
    if await rose._is_admin(cid, uid):
        return False

    key = (str(cid), str(uid))
    now = time.time()
    stamps = [t for t in _flood_state.get(key, []) if now - t <= window]
    stamps.append(now)
    _flood_state[key] = stamps
    if len(stamps) < limit:
        return False

    _flood_state[key] = []
    minutes = int(conf.get("flood_mute_minutes") or 10)
    try:
        await asyncio.to_thread(telegram.restrict_chat_member, cid, uid,
                                True, int(now) + minutes * 60)
        await asyncio.to_thread(
            telegram.send_message,
            f"🌊 <b>{telegram.esc(user.get('first_name') or 'User')}</b> is muted for "
            f"{minutes}m — that was {limit} messages in {window}s.",
            chat_id=cid)
    except Exception as e:
        log.info("flood mute failed: %s", e)
    return True
