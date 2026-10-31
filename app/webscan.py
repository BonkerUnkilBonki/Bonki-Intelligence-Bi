"""Read a PUBLIC channel's posts without any user account.

Telegram's Bot API has no history method at all - bots only receive messages
going forward. But Telegram publishes public channels at https://t.me/s/<name>,
and that page can be read. So for a **public channel** we can index what was
posted before the bot arrived, with no Telethon login.

Hard limits, stated plainly:
  * **public channels only** - private groups/channels have no preview at all,
    and nothing here can change that (it is a Telegram permission boundary)
  * the preview shows text and photos but **not downloadable documents**, so we
    index the *file name* (from the document title or the caption) and link to
    the post - we cannot re-send the file itself
  * only the recent window the preview exposes (about the last 20 posts)

Ported from the original Bonki bot's public-preview reader.
"""
import html as htmlmod
import logging
import re

import requests

log = logging.getLogger("webscan")

_UA = {"User-Agent": "Mozilla/5.0 (compatible; bonki-bot/1.0)"}

_EXT_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9 _.\-\[\]()+]{0,80}?\.(?:apk|xapk|apks|zip|rar|7z|tar|gz|"
    r"obb|ipa|exe|msi|deb|iso|img|bin|pdf|docx?|xlsx?|pptx?|txt|json|xml|csv|"
    r"mp3|mp4|mkv|srt|epub))\b", re.I)

_SIZE_RE = re.compile(r"([\d.]+)\s*(B|KB|MB|GB)", re.I)
_UNIT = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}


def normalize_handle(text):
    """Accept @name, t.me/name, https://t.me/s/name -> 'name'."""
    t = (text or "").strip().rstrip("/")
    m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]+)", t)
    if m:
        return m.group(1)
    if t.startswith("@"):
        return t[1:]
    return t


def is_public_handle(handle):
    h = normalize_handle(handle)
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", h or ""))


def _strip_tags(fragment):
    fragment = re.sub(r"<br\s*/?>", "\n", fragment or "")
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return htmlmod.unescape(fragment).strip()


def parse(page):
    """Pull the messages out of a t.me/s/ page."""
    posts = []
    anchors = list(re.finditer(r'data-post="([^"/]+)/(\d+)"', page))
    for i, m in enumerate(anchors):
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(page)
        block = page[m.start():end]
        mid = int(m.group(2))
        tm = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                       block, re.S)
        text = _strip_tags(tm.group(1)) if tm else ""
        dn = re.search(r'tgme_widget_message_document_title[^>]*>(.*?)</div>', block, re.S)
        ds = re.search(r'tgme_widget_message_document_extra[^>]*>(.*?)</div>', block, re.S)
        dt = re.search(r'<time datetime="([^"]+)"', block)
        posts.append({
            "id": mid,
            "text": text,
            "doc_name": _strip_tags(dn.group(1)) if dn else "",
            "doc_size": _strip_tags(ds.group(1)) if ds else "",
            "datetime": dt.group(1) if dt else "",
        })
    return posts


def fetch_posts(handle, timeout=25):
    handle = normalize_handle(handle)
    r = requests.get(f"https://t.me/s/{handle}", headers=_UA, timeout=timeout)
    r.raise_for_status()
    return parse(r.text)


def _size_bytes(s):
    m = _SIZE_RE.search(s or "")
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * _UNIT[m.group(2).upper()])
    except (KeyError, ValueError):
        return 0


def _filename_from_text(text):
    m = _EXT_RE.search(text or "")
    if not m:
        return ""
    name = m.group(1).strip(" -–—•\n\t")
    return name[-120:]


def posts_to_files(posts):
    """Turn preview posts into index entries (name-based)."""
    out = []
    for p in posts:
        name = (p.get("doc_name") or "").strip() or _filename_from_text(p.get("text"))
        if not name:
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        out.append({
            "message_id": p.get("id"),
            "name": name,
            "ext": ext,
            "size": _size_bytes(p.get("doc_size")),
            "mime": "",
            "date": 0,
            "sender_id": None,
            "sender_name": "",
            "caption": (p.get("text") or "")[:300],
            "source": "web",
        })
    return out


def scan_public(handle, exts=None, limit=100):
    """Fetch a public channel's preview and return file entries (no account)."""
    posts = fetch_posts(handle)
    files = posts_to_files(posts)
    if exts:
        files = [f for f in files if not f["ext"] or f["ext"] in exts]
    return files[:limit]
