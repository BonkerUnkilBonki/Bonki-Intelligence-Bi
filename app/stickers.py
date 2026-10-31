"""Quote maker + sticker-pack maker (Stickerkang-style).

  /q  (or /quote, /quotly)   reply to a message -> a rendered quote image
  /kang [emoji]              reply to a sticker / photo / image -> your pack
  /getsticker                reply to a sticker -> its raw PNG
  /stickerid                 reply to a sticker -> its file_id
  /packs                     link to your pack
  /delsticker                reply to a sticker -> remove it from your pack

Honest scope: static stickers only. Animated (.tgs) and video (.webm) stickers
need extra conversion tooling and are rejected with a clear message. The quote
renderer draws a clean card (name, text, timestamp) - it is not QuotLy's exact
layout, and it has no avatar images (a coloured initial is drawn instead).
"""
import asyncio
import io
import logging
import os
import re
import tempfile
import time

from . import db, telegram

log = logging.getLogger("stickers")

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:                                    # pragma: no cover
    Image = None

_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/system/fonts/Roboto-Bold.ttf",
]


def _font(size):
    for p in _FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


async def _reply(chat, msg, text):
    return await asyncio.to_thread(telegram.send_message, text, chat_id=chat.get("id"),
                                   reply_to=msg.get("message_id"))


def _bot_username():
    try:
        from . import handlers
        return handlers._bot_identity.get("username") or "bot"
    except Exception:
        return "bot"


# ------------------------------------------------------------------ quote ---
def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in (text or "").split():
        trial = (cur + " " + word).strip()
        try:
            width = draw.textlength(trial, font=font)
        except Exception:
            width = len(trial) * 14
        if width <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def render_quote(sender, text, footer=""):
    """Render a quote card as PNG bytes."""
    if Image is None:
        raise RuntimeError("image support is missing - add Pillow to requirements")
    W, pad = 900, 36
    f_name, f_text, f_foot = _font(34), _font(30), _font(22)
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = _wrap(probe, text, f_text, W - 2 * pad - 20)[:40]
    line_h = 40
    H = pad * 2 + 56 + line_h * len(lines) + 48

    img = Image.new("RGB", (W, H), "#0f172a")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, W - 8, H - 8], radius=26, fill="#1f2937",
                        outline="#334155", width=2)
    d.rounded_rectangle([8, 12, 18, H - 12], radius=6, fill="#7c5cff")

    cx, cy, r = pad + 28, pad + 26, 26
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#7c5cff")
    d.text((cx, cy), (sender or "?")[:1].upper(), font=f_name, fill="#ffffff", anchor="mm")
    d.text((pad + 68, pad + 6), (sender or "Unknown")[:38], font=f_name, fill="#a78bfa")

    y = pad + 58
    for ln in lines:
        d.text((pad, y), ln, font=f_text, fill="#e5e7eb")
        y += line_h
    if footer:
        d.text((pad, H - pad - 24), footer, font=f_foot, fill="#94a3b8")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


async def cmd_quote(chat, msg, user, args):
    rt = msg.get("reply_to_message")
    if not rt:
        return await _reply(chat, msg, "Reply to a message and use /q to quote it.")
    frm = rt.get("from") or {}
    sender = frm.get("first_name") or frm.get("username") or "Unknown"
    text = rt.get("text") or rt.get("caption") or ""
    if not text:
        text = "[media]"
    ts = rt.get("date")
    footer = time.strftime("%d %b %Y · %H:%M", time.localtime(ts)) if ts else ""
    try:
        png = await asyncio.to_thread(render_quote, sender, text, footer)
    except Exception as e:
        return await _reply(chat, msg, f"⚠️ Couldn't render the quote: {telegram.esc(e)}")

    path = os.path.join(tempfile.gettempdir(), f"q_{chat.get('id')}_{msg.get('message_id')}.png")
    try:
        with open(path, "wb") as fh:
            fh.write(png)
        await asyncio.to_thread(telegram.send_photo, path, "quote.png", "",
                                chat.get("id"), msg.get("message_id"))
    except Exception as e:
        await _reply(chat, msg, f"⚠️ Couldn't send the quote: {telegram.esc(e)}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------- stickers ---
def to_sticker_png(raw):
    """Any image bytes -> a static-sticker PNG (<=512px, <=512KB)."""
    if Image is None:
        raise RuntimeError("image support is missing - add Pillow to requirements")
    im = Image.open(io.BytesIO(raw))
    if im.mode not in ("RGBA", "RGB"):
        im = im.convert("RGBA")
    w, h = im.size
    scale = min(1.0, 512.0 / max(w, h))
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    if len(data) > 512 * 1024:                       # too big -> flatten + retry
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "PNG", optimize=True)
        data = buf.getvalue()
    return data


def _sticker_file_id(rt):
    st = rt.get("sticker") or {}
    if st.get("file_id"):
        return st.get("file_id"), st
    photos = rt.get("photo") or []
    if photos:
        return photos[-1].get("file_id"), {}
    doc = rt.get("document") or {}
    if str(doc.get("mime_type", "")).startswith("image/"):
        return doc.get("file_id"), {}
    return None, {}


async def cmd_kang(chat, msg, user, args):
    rt = msg.get("reply_to_message") or {}
    file_id, st = _sticker_file_id(rt)
    if not file_id:
        return await _reply(chat, msg, "Reply to a sticker, photo or image to add it "
                                       "to your pack.")
    if st.get("is_animated") or st.get("is_video"):
        return await _reply(chat, msg, "Only static stickers and images for now — "
                                       "animated/video stickers need extra conversion.")

    emoji = (args or "").strip().split()[0] if (args or "").strip() else ""
    emoji = emoji or st.get("emoji") or "🤔"

    try:
        info = await asyncio.to_thread(telegram.get_file, file_id)
        fpath = (info.get("result") or {}).get("file_path")
        if not fpath:
            raise RuntimeError("Telegram didn't return a file path")
        raw = await asyncio.to_thread(telegram.download_file, fpath)
        png = await asyncio.to_thread(to_sticker_png, raw)
    except Exception as e:
        return await _reply(chat, msg, f"⚠️ Couldn't fetch that image: {telegram.esc(e)}")

    uid = user.get("id")
    uname = _bot_username()
    name = f"b{uid}_by_{uname}"
    title = f"{(user.get('first_name') or 'My')[:40]}'s Bonki pack"
    rec = db.get_stickerpack(uid)

    try:
        if not rec:
            await asyncio.to_thread(telegram.create_new_sticker_set, uid, name, title, png, emoji)
            db.set_stickerpack(uid, name, title, 1)
        else:
            name = rec.get("name") or name
            title = rec.get("title") or title
            await asyncio.to_thread(telegram.add_sticker_to_set, uid, name, png, emoji)
            db.set_stickerpack(uid, name, title, int(rec.get("count") or 0) + 1)
    except Exception as e:
        err = str(e)
        low = err.lower()
        if "stickerset_invalid" in low or "invalid" in low:
            try:                                      # pack was deleted -> recreate
                await asyncio.to_thread(telegram.create_new_sticker_set,
                                        uid, name, title, png, emoji)
                db.set_stickerpack(uid, name, title, 1)
            except Exception as e2:
                return await _reply(chat, msg, f"⚠️ Couldn't create the pack: "
                                               f"{telegram.esc(e2)}")
        elif "too_much" in low or "too much" in low or "120" in err:
            return await _reply(chat, msg, "That pack is full (120 stickers).")
        else:
            return await _reply(chat, msg, f"⚠️ {telegram.esc(err)}")

    rec = db.get_stickerpack(uid) or {}
    await _reply(chat, msg,
                 f"✅ Added to your pack ({rec.get('count', '?')} stickers)\n"
                 f"https://t.me/addstickers/{name}")


async def cmd_getsticker(chat, msg, user, args):
    rt = msg.get("reply_to_message") or {}
    file_id, st = _sticker_file_id(rt)
    if not file_id:
        return await _reply(chat, msg, "Reply to a sticker to get its PNG.")
    if st.get("is_animated") or st.get("is_video"):
        return await _reply(chat, msg, "Animated/video stickers can't be converted to PNG here.")
    try:
        info = await asyncio.to_thread(telegram.get_file, file_id)
        fpath = (info.get("result") or {}).get("file_path")
        raw = await asyncio.to_thread(telegram.download_file, fpath)
        png = await asyncio.to_thread(to_sticker_png, raw)
        path = os.path.join(tempfile.gettempdir(), f"st_{msg.get('message_id')}.png")
        with open(path, "wb") as fh:
            fh.write(png)
        try:
            await asyncio.to_thread(telegram.send_document, path, "sticker.png", "",
                                    chat.get("id"), msg.get("message_id"))
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    except Exception as e:
        await _reply(chat, msg, f"⚠️ {telegram.esc(e)}")


async def cmd_stickerid(chat, msg, user, args):
    rt = msg.get("reply_to_message") or {}
    st = rt.get("sticker") or {}
    if not st.get("file_id"):
        return await _reply(chat, msg, "Reply to a sticker to get its file id.")
    await _reply(chat, msg,
                 f"🆔 <code>{telegram.esc(st['file_id'])}</code>\n"
                 f"emoji: {telegram.esc(st.get('emoji') or '—')}\n"
                 f"set: {telegram.esc(st.get('set_name') or '—')}")


async def cmd_packs(chat, msg, user, args):
    rec = db.get_stickerpack(user.get("id"))
    if not rec:
        return await _reply(chat, msg, "You don't have a pack yet — reply to a sticker "
                                       "with /kang to make one.")
    await _reply(chat, msg,
                 f"📦 <b>{telegram.esc(rec.get('title') or 'Your pack')}</b>\n"
                 f"{rec.get('count', 0)} stickers\n"
                 f"https://t.me/addstickers/{rec.get('name')}")


async def cmd_delsticker(chat, msg, user, args):
    rt = msg.get("reply_to_message") or {}
    st = rt.get("sticker") or {}
    if not st.get("file_id"):
        return await _reply(chat, msg, "Reply to a sticker from your pack to remove it.")
    rec = db.get_stickerpack(user.get("id"))
    if not rec or st.get("set_name") != rec.get("name"):
        return await _reply(chat, msg, "That sticker isn't in your pack.")
    try:
        await asyncio.to_thread(telegram.delete_sticker_from_set, st["file_id"])
        db.set_stickerpack(user.get("id"), rec.get("name"), rec.get("title"),
                           max(0, int(rec.get("count") or 0) - 1))
        await _reply(chat, msg, "🗑 Removed from your pack.")
    except Exception as e:
        await _reply(chat, msg, f"⚠️ {telegram.esc(e)}")
