"""The group file index.

Three ways files get into the index - so no manual scan is ever required:

  * live capture  - whenever a file is posted in a group the bot can see, it is
    indexed straight away (indexer.capture_from_message)
  * auto-scan     - a background timer walks each group's history for new files
    and indexes them (main._auto_index_loop -> indexer.scan_all)
  * manual scan   - "Bonki, scan this group" (indexer.scan, full pass)

All three funnel into db.upsert_file, which de-duplicates, so a file is never
indexed twice.
"""
import asyncio
import logging
import time

from . import db, userbot

log = logging.getLogger("indexer")


def parse_exts(raw):
    out = set()
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip().lstrip(".").lower()
        if part:
            out.add(part)
    return out


def index_exts():
    return parse_exts(db.get_settings().get("index_extensions") or "")


def _msg_is_file(msg):
    """A Bot API message carries a real file if it has `document` and isn't a
    video/audio/voice/animation/sticker (Telegram stores those as documents)."""
    if not msg.get("document"):
        return False
    for k in ("video", "audio", "voice", "animation", "video_note", "sticker"):
        if msg.get(k):
            return False
    return True


def capture_from_message(chat, msg):
    """Index a file from a live Bot API message. Returns True if indexed."""
    if not _msg_is_file(msg):
        return False
    doc = msg["document"]
    name = doc.get("file_name") or f"file_{msg.get('message_id', '')}"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext and index_exts() and ext not in index_exts():
        return False
    frm = msg.get("from") or {}
    sender_name = " ".join(x for x in [frm.get("first_name"), frm.get("last_name")] if x)
    added = db.upsert_file(
        chat_id=chat.get("id"), message_id=msg.get("message_id"), name=name, ext=ext,
        size=doc.get("file_size") or 0, mime=doc.get("mime_type") or "",
        date=msg.get("date") or 0, sender_id=frm.get("id"), sender_name=sender_name,
        caption=msg.get("caption") or "", source="live",
    )
    if added:
        log.info("indexed live file %s in %s", name, chat.get("id"))
    return added


def _newest_known(chat_id):
    return max((f.get("message_id") or 0 for f in db.list_files(chat_id=chat_id)), default=0)


async def scan(group_row, full=False):
    """Scan one group's history with the user account and index its files.

    Incremental by default: only messages newer than the newest we already have
    are fetched. Pass full=True for a complete re-walk (the manual scan button).
    """
    handle = group_row.get("handle") or group_row.get("chat_id")
    chat_id = group_row.get("chat_id") or handle
    depth = int(db.get_settings().get("scan_depth") or 500)
    exts = index_exts()
    min_id = 0 if full else _newest_known(chat_id)
    try:
        files = await userbot.scan(handle, limit=depth, exts=exts, min_id=min_id)
    except Exception as e:
        db.update_group(group_row["id"], {"last_status": f"scan failed: {e}"})
        await asyncio.to_thread(db.flush, True)
        return {"group": handle, "error": str(e), "added": 0, "found": 0}
    added = 0
    for f in files:
        if db.upsert_file(chat_id=chat_id, message_id=f["message_id"], name=f["name"],
                          ext=f["ext"], size=f["size"], mime=f["mime"], date=f["date"],
                          sender_id=f.get("sender_id"), sender_name=f.get("sender_name"),
                          caption=f.get("caption"), source="history"):
            added += 1
    db.update_group(group_row["id"], {
        "last_scan": time.time(),
        "last_status": (f"scanned {len(files)} new message(s), {added} file(s) added"
                        if not full else
                        f"full scan: {len(files)} file(s), {added} new"),
    })
    await asyncio.to_thread(db.flush, True)
    if added:
        db.log_activity("scan", chat_id, f"{added} new file(s) indexed")
    return {"group": handle, "added": added, "found": len(files)}


async def scan_all(full=False):
    if db.is_paused():
        return []
    out = []
    for g in db.list_groups():
        if g.get("enabled") and g.get("approved"):      # verified groups only
            try:
                out.append(await scan(g, full=full))
            except Exception as e:
                log.warning("scan %s failed: %s", g.get("chat_id"), e)
    return out
