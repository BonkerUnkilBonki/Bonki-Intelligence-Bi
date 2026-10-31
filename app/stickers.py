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


_ASSET_FONT = os.path.join(os.path.dirname(__file__), "assets", "DejaVuSans.ttf")


def _font(size):
    # bundled font first, so the card looks the same on every host
    for p in [_ASSET_FONT] + _FONT_PATHS:
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
# Telegram's own clients keep paragraph breaks. Collapsing them into one blob was
# the single biggest visual difference from the quote bots people compare us to.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"      # emoji / pictographs
    "\U00002600-\U000027BF"       # misc symbols, dingbats
    "\U0001F1E6-\U0001F1FF"       # flags
    "\U00002B00-\U00002BFF"
    "\U00002300-\U000023FF"
    "\U0000FE00-\U0000FE0F"       # variation selectors
    "\U0000200D"                  # zero-width joiner
    "\U000020E3"                  # combining keycap
    "\U00002190-\U000021FF"       # arrows
    "\U0000FFFD]+")


_NOTDEF = {}


def _notdef_sig(font):
    """Bitmap FreeType returns for a glyph the font does not have (.notdef)."""
    key = id(font)
    if key not in _NOTDEF:
        try:
            _NOTDEF[key] = bytes(font.getmask("\uffff"))
        except Exception:
            _NOTDEF[key] = None
    return _NOTDEF[key]


def _can_draw(font, ch):
    try:
        mask = bytes(font.getmask(ch))
    except Exception:
        return False
    sig = _notdef_sig(font)
    return sig is None or mask != sig


def _clean_text(t, font=None):
    """Drop glyphs this font cannot draw.

    Emoji AND scripts outside the font's coverage (e.g. CJK) otherwise render as
    empty boxes. Comparing each glyph's bitmap against the font's .notdef bitmap
    detects both without needing a 16 MB CJK font bundled in.
    """
    t = _EMOJI_RE.sub("", (t or "").replace("\r", ""))
    if font is None:
        return t
    return "".join(ch if (ch in "\n\t " or _can_draw(font, ch)) else "" for ch in t)


def _wrap(draw, text, font, max_w):
    """Wrap text but KEEP blank lines and paragraph breaks."""
    lines = []
    for para in (text or "").split("\n"):
        if not para.strip():
            lines.append("")
            continue
        cur = ""
        for word in para.split():
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


RENDER_W = 1024          # draw at 2x, then fit into a 512x512 sticker square
PAD = 48


def render_quote_card(sender, text, footer=""):
    """The full-resolution card (RGBA)."""
    if Image is None:
        raise RuntimeError("image support is missing - add Pillow to requirements")
    f_name, f_text, f_foot = _font(42), _font(36), _font(26)
    text = _clean_text(text, f_text)
    sender = _clean_text(sender, f_name).strip() or "Unknown"
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = _wrap(probe, text, f_text, RENDER_W - 2 * PAD - 40)[:60]
    line_h = 50
    H = PAD * 2 + 60 + line_h * len(lines) + (38 if footer else 0)

    img = Image.new("RGBA", (RENDER_W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, RENDER_W - 4, H - 4], radius=30,
                        fill="#1c1c26", outline="#34343f", width=3)
    d.rounded_rectangle([4, 14, 18, H - 14], radius=7, fill="#8b5cf6")
    d.text((PAD + 14, PAD - 6), (sender or "Unknown")[:40], font=f_name, fill="#5eead4")

    y = PAD + 58
    for ln in lines:
        if ln:
            d.text((PAD + 14, y), ln, font=f_text, fill="#e9e9f0")
        y += line_h
    if footer:
        d.text((PAD + 14, H - PAD - 24), footer, font=f_foot, fill="#8b8b9e")
    return img


def to_sticker(img, size=512):
    """Centre the card in a transparent 512x512 square. Returns (png, scale)."""
    scale = min(size / img.width, size / img.height)
    w = max(1, int(img.width * scale))
    h = max(1, int(img.height * scale))
    small = img.resize((w, h), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(small, ((size - w) // 2, (size - h) // 2), small)
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    return buf.getvalue(), scale


def to_png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_quote(sender, text, footer=""):
    """Backwards-compatible: PNG bytes of the card."""
    return to_png(render_quote_card(sender, text, footer))


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
        img = await asyncio.to_thread(render_quote_card, sender, text, footer)
        sticker_png, scale = await asyncio.to_thread(to_sticker, img)
        photo_png = await asyncio.to_thread(to_png, img)
    except Exception as e:
        return await _reply(chat, msg, f"⚠️ Couldn't render the quote: {telegram.esc(e)}")

    base = os.path.join(tempfile.gettempdir(),
                        f"q_{chat.get('id')}_{msg.get('message_id')}")
    try:
        # Short and medium quotes go out as a STICKER, which is what the quote
        # bots people compare us to send. A long one would be unreadable at
        # 512px, so it goes out as a full-size photo instead.
        if scale >= 0.45:
            path = base + ".png"
            with open(path, "wb") as fh:
                fh.write(sticker_png)
            try:
                await asyncio.to_thread(telegram.send_sticker, path,
                                        chat.get("id"), msg.get("message_id"))
                return
            except Exception as e:
                log.info("sticker send failed, falling back to photo: %s", e)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        path = base + "_full.png"
        with open(path, "wb") as fh:
            fh.write(photo_png)
        await asyncio.to_thread(telegram.send_photo, path, "quote.png", "",
                                chat.get("id"), msg.get("message_id"))
    except Exception as e:
        await _reply(chat, msg, f"⚠️ Couldn't send the quote: {telegram.esc(e)}")
    finally:
        for p in (base + ".png", base + "_full.png"):
            try:
                os.remove(p)
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
