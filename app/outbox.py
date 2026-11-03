"""Holds replies that Telegram refused, and delivers them when it calms down.

The failure this fixes: after a big tag run the bot gets flood-limited. A send
raises 429, the reply is dropped, and the bot looks completely dead - even for
"find X", where the search worked fine and only the delivery failed.

Queued messages are retried in the background for up to 30 minutes.
"""
import asyncio
import logging
import time

from . import telegram

log = logging.getLogger("outbox")

MAX_ITEMS = 200
MAX_AGE = 30 * 60          # give up after 30 minutes
RETRY_EVERY = 45


def _q():
    return telegram._OUTBOX


def enqueue(method, data, err=""):
    """Park a message that could not be delivered."""
    q = _q()
    if len(q) >= MAX_ITEMS:
        q.pop(0)                       # drop the oldest rather than grow forever
    q.append({"method": method, "data": data, "ts": time.time(),
              "tries": 0, "err": str(err)[:120]})
    log.info("queued a %s for retry (%d waiting)", method, len(q))


def size():
    return len(_q())


def _notify_owner(text):
    """DM the owner - a different chat, so a group restriction does not block it."""
    try:
        from . import approval
        oid = approval.owner_id()
        if oid:
            telegram._post("sendMessage", {"chat_id": oid, "text": text,
                                           "parse_mode": "HTML"})
    except Exception as e:
        log.info("owner notify failed: %s", e)


async def drain_once():
    """Try every queued message once. Returns how many got through."""
    q = _q()
    if not q:
        return 0
    now = time.time()
    sent = 0
    keep = []
    for item in q:
        if now - item["ts"] > MAX_AGE:
            continue                    # too old, drop it
        try:
            await asyncio.to_thread(telegram._post, item["method"], item["data"])
            sent += 1
        except telegram.RateLimited:
            item["tries"] += 1
            keep.append(item)           # still restricted, keep it
        except Exception as e:
            item["tries"] += 1
            item["err"] = str(e)[:120]
            if item["tries"] < 3:       # give a real error a couple of chances
                keep.append(item)
    _q()[:] = keep
    return sent


async def loop():
    log.info("outbox drainer started")
    warned = False
    while True:
        await asyncio.sleep(RETRY_EVERY)
        try:
            if not size():
                warned = False
                continue
            # tell the owner once, not every cycle
            if not warned:
                warned = True
                _notify_owner(
                    f"⚠️ <b>Telegram is rate-limiting me</b>\n"
                    f"• {size()} repl(y/ies) queued, not lost\n"
                    f"• I will send them as soon as the limit lifts\n"
                    f"• cause: usually a very large /tag run\n\n"
                    f"I'm still running — this is a delivery limit, not a crash.")
            n = await drain_once()
            if n:
                log.info("outbox delivered %d message(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("outbox error: %s", e)
