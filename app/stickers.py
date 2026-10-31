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
# Layout mirrors QuotLy (LyoSU/quote-bot v2): 512-wide canvas, ink-trimmed height,
# #292232 background, and the sender's name tinted with their own accent colour.
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

# Telegram-ish accent palette; a sender always gets the same colour.
_ACCENTS = ("#e17076", "#7bc862", "#e5ca77", "#65aadd", "#a695e7",
            "#ee7aae", "#6ec9cb", "#faa774")

BG = "#292232"                    # QuotLy DEFAULT_BACKGROUND
BG_EDGE = "#3a3244"
TEXT = "#e9e9f0"
DIM = "#a89fb3"

RENDER_W = 1024                   # 2x of QuotLy's 512 logical width
PAD = 52


def accent_for(sender, sender_id=None):
    key = str(sender_id) if sender_id not in (None, "") else (sender or "?")
    return _ACCENTS[sum(ord(c) for c in key) % len(_ACCENTS)]


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
    """Drop glyphs this font cannot draw (emoji, CJK) instead of drawing boxes."""
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


def render_quote_card(sender, text, footer="", sender_id=None, reply_ctx=None):
    """QuotLy-style card. `reply_ctx` = (name, snippet) of the replied-to message."""
    if Image is None:
        raise RuntimeError("image support is missing - add Pillow to requirements")

    f_name, f_text = _font(40), _font(36)
    f_small, f_foot = _font(30), _font(26)
    accent = accent_for(sender, sender_id)

    text = _clean_text(text, f_text)
    sender = _clean_text(sender, f_name).strip() or "Unknown"

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    indent = 0
    body_w = RENDER_W - 2 * PAD
    lines = _wrap(probe, text, f_text, body_w)[:60]

    ctx_lines = []
    if reply_ctx:
        rname = _clean_text(reply_ctx[0], f_small).strip() or "Reply"
        rsnippet = _clean_text(reply_ctx[1], f_small).strip()
        ctx_lines = [(rname, True)]
        ctx_lines += [(l, False) for l in
                      _wrap(probe, rsnippet, f_small, body_w - 30)[:3]]

    line_h, small_h = 52, 38
    H = (PAD * 2 + 60 + len(ctx_lines) * small_h
         + len(lines) * line_h + (30 if footer else 0))

    img = Image.new("RGBA", (RENDER_W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, RENDER_W - 2, H - 2], radius=44, fill=BG,
                        outline=BG_EDGE, width=2)

    # sender name, tinted with their accent colour (QuotLy does this per sender)
    d.text((PAD, PAD - 4), sender[:40], font=f_name, fill=accent)
    y = PAD + 58

    if ctx_lines:                       # reply block: dim text with a left rule
        top = y
        for txt, is_name in ctx_lines:
            d.text((PAD + 26, y), txt, font=f_small,
                   fill=DIM if is_name else "#8d8497")
            y += small_h
        d.rounded_rectangle([PAD + 4, top + 4, PAD + 10, y - 10],
                            radius=3, fill=BG_EDGE)
        y += 10

    for ln in lines:
        if ln:
            d.text((PAD, y), ln, font=f_text, fill=TEXT)
        y += line_h

    if footer:
        d.text((PAD, H - PAD - 20), footer, font=f_foot, fill=DIM)

    # ink-trim: drop fully transparent rows so the card hugs its content
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    return img


def to_sticker(img, size=512):
    """Fit the card into a 512-square sticker, preserving aspect. (png, scale)."""
    scale = min(size / img.width, size / img.height)
    w, h = max(1, int(img.width * scale)), max(1, int(img.height * scale))
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

    # reply-context block, like QuotLy's
    reply_ctx = None
    inner = rt.get("reply_to_message") or {}
    if inner:
        ifrm = inner.get("from") or {}
        rname = ifrm.get("first_name") or ifrm.get("username") or "Reply"
        rsnippet = (inner.get("text") or inner.get("caption")
                    or "[media]")[:160]
        reply_ctx = (rname, rsnippet)

    try:
        img = await asyncio.to_thread(render_quote_card, sender, text, "",
                                      frm.get("id"), reply_ctx)
        sticker_png, scale = await asyncio.to_thread(to_sticker, img)
        photo_png = await asyncio.to_thread(to_png, img)
    except Exception as e:
        return await _reply(chat, msg, f"⚠️ Couldn't render the quote: {telegram.esc(e)}")

    base = os.path.join(tempfile.gettempdir(),
                        f"q_{chat.get('id')}_{msg.get('message_id')}")
    try:
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


# ------------------------------------------------------- channel stats card ---
def render_stats_card(title, subtitle, metrics, series=None, footer="via Telemetr.io"):
    """Telemetr.io-style statistics card. metrics = [(label, value), ...]."""
    if Image is None:
        raise RuntimeError("image support is missing - add Pillow to requirements")

    W, PAD = 1024, 56
    f_title, f_sub = _font(46), _font(30)
    f_lab, f_val = _font(26), _font(48)
    f_foot = _font(24)

    title = _clean_text(title, f_title).strip() or "Channel"
    subtitle = _clean_text(subtitle, f_sub).strip()
    accent = accent_for(title, subtitle)

    rows = [(l, v) for l, v in (metrics or []) if v not in (None, "", "—")][:6]
    cols = 2 if len(rows) > 1 else 1
    grid_rows = (len(rows) + cols - 1) // cols
    cell_h = 96
    spark_h = 150 if (series and len(series) > 1) else 0

    H = (PAD * 2 + 66 + (36 if subtitle else 0)
         + grid_rows * cell_h + (spark_h + 20 if spark_h else 0) + 34)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, W - 2, H - 2], radius=44, fill=BG, outline=BG_EDGE, width=2)

    d.text((PAD, PAD - 6), title[:34], font=f_title, fill=accent)
    y = PAD + 60
    if subtitle:
        d.text((PAD, y), subtitle[:40], font=f_sub, fill=DIM)
        y += 36
    y += 14

    col_w = (W - 2 * PAD) // cols
    for i, (label, value) in enumerate(rows):
        r, c = divmod(i, cols)
        x = PAD + c * col_w
        yy = y + r * cell_h
        d.text((x, yy), _clean_text(str(label), f_lab)[:22], font=f_lab, fill=DIM)
        d.text((x, yy + 32), _clean_text(str(value), f_val)[:14], font=f_val, fill=TEXT)

    y += grid_rows * cell_h
    if spark_h and series:
        pts = [float(v) for v in series if v is not None]
        lo, hi = min(pts), max(pts)
        span = (hi - lo) or 1.0
        x0, x1 = PAD, W - PAD
        top, bot = y + 16, y + spark_h - 16
        coords = []
        for i, v in enumerate(pts):
            px = x0 + (x1 - x0) * (i / max(1, len(pts) - 1))
            py = bot - (bot - top) * ((v - lo) / span)
            coords.append((px, py))
        if len(coords) > 1:
            d.line(coords, fill=accent, width=5, joint="curve")
            d.ellipse([coords[-1][0] - 8, coords[-1][1] - 8,
                       coords[-1][0] + 8, coords[-1][1] + 8], fill=accent)
        d.text((x0, y), "subscribers", font=f_foot, fill=DIM)

    if footer:
        d.text((PAD, H - PAD - 14), footer, font=f_foot, fill=DIM)

    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    return img


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
