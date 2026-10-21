"""Thin wrapper over the Telegram Bot API (what Bonki uses to *speak*)."""
import html
import json
import logging
import os

import requests

log = logging.getLogger("telegram")

MAX_CAPTION = 1024
MAX_MESSAGE = 4096


def _token():
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return tok


def _base():
    return os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")


def _url(method):
    return f"{_base()}/bot{_token()}/{method}"


def _post(method, data, **kw):
    r = requests.post(_url(method), data=data, timeout=kw.pop("timeout", 30), **kw)
    if not r.ok:
        raise RuntimeError(f"{method} failed: {r.status_code} {r.text[:300]}")
    return r.json()


def esc(s):
    return html.escape(str(s or ""), quote=True)


def send_message(text, chat_id, reply_to=None, thread_id=None, silent=False,
                 disable_preview=True, parse_mode="HTML", reply_markup=None):
    data = {
        "chat_id": chat_id,
        "text": (text or "")[:MAX_MESSAGE],
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
        "disable_notification": bool(silent),
    }
    if reply_to:
        data["reply_to_message_id"] = reply_to
        data["allow_sending_without_reply"] = True
    if thread_id:
        data["message_thread_id"] = thread_id
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    return _post("sendMessage", data)


def answer_callback_query(callback_query_id, text=None, show_alert=False):
    data = {"callback_query_id": callback_query_id}
    if text:
        data["text"] = text[:200]
        data["show_alert"] = bool(show_alert)
    return _post("answerCallbackQuery", data)


def delete_message(chat_id, message_id):
    return _post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def leave_chat(chat_id):
    return _post("leaveChat", {"chat_id": chat_id})


def edit_message_reply_markup(chat_id, message_id, reply_markup=None):
    data = {"chat_id": chat_id, "message_id": message_id,
            "reply_markup": json.dumps(reply_markup or {"inline_keyboard": []})}
    return _post("editMessageReplyMarkup", data)


def send_document(file_path, filename, caption="", chat_id=None, reply_to=None,
                  thread_id=None, silent=False):
    """Upload a local file as a document, optionally as a reply."""
    data = {
        "chat_id": chat_id,
        "caption": (caption or "")[:MAX_CAPTION],
        "parse_mode": "HTML",
        "disable_notification": bool(silent),
    }
    if reply_to:
        data["reply_to_message_id"] = reply_to
        data["allow_sending_without_reply"] = True
    if thread_id:
        data["message_thread_id"] = thread_id
    with open(file_path, "rb") as f:
        files = {"document": (filename, f, "application/octet-stream")}
        r = requests.post(_url("sendDocument"), data=data, files=files, timeout=3600)
    if not r.ok:
        raise RuntimeError(f"sendDocument failed: {r.status_code} {r.text[:300]}")
    return r.json()


def copy_message(from_chat_id, message_id, chat_id, caption=None, reply_to=None,
                 thread_id=None, silent=False):
    """Copy a message server-side (no 'Forwarded from' header, no re-upload)."""
    data = {
        "chat_id": chat_id,
        "from_chat_id": from_chat_id,
        "message_id": message_id,
        "disable_notification": bool(silent),
    }
    if reply_to:
        data["reply_to_message_id"] = reply_to
        data["allow_sending_without_reply"] = True
    if thread_id:
        data["message_thread_id"] = thread_id
    if caption is not None:
        data["caption"] = caption[:MAX_CAPTION]
        data["parse_mode"] = "HTML"
    return _post("copyMessage", data)


def send_chat_action(chat_id, action="typing"):
    try:
        return _post("sendChatAction", {"chat_id": chat_id, "action": action})
    except Exception:
        return None


def chat_link(chat_id, username=None):
    """A tappable link to a chat: @username when public, else the t.me/c/ form
    (which opens for members). Returns None if there's nothing linkable."""
    if username:
        return f"https://t.me/{str(username).lstrip('@')}"
    cid = str(chat_id or "").strip()
    if cid.startswith("@"):
        return f"https://t.me/{cid[1:]}"
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}"
    return None


def get_me():
    return _post("getMe", {})


def get_chat(chat_id):
    return _post("getChat", {"chat_id": chat_id})


def get_chat_member(chat_id, user_id):
    return _post("getChatMember", {"chat_id": chat_id, "user_id": user_id})


def get_file(file_id):
    return _post("getFile", {"file_id": file_id})


def download_file(file_path):
    """Return the bytes of a file by its Telegram file_path (<=20 MB for bots)."""
    r = requests.get(f"{_base()}/file/bot{_token()}/{file_path}", timeout=120)
    r.raise_for_status()
    return r.content


def set_webhook(url, secret_token=None):
    data = {"url": url,
            "allowed_updates": '["message","edited_message","channel_post","callback_query","my_chat_member"]'}
    if secret_token:
        data["secret_token"] = secret_token
    return _post("setWebhook", data)


def delete_webhook():
    return _post("deleteWebhook", {})


def get_updates(offset=None, timeout=25):
    data = {"timeout": timeout,
            "allowed_updates": '["message","edited_message","channel_post","callback_query","my_chat_member"]'}
    if offset is not None:
        data["offset"] = offset
    return _post("getUpdates", data, timeout=timeout + 10)


# ------------------------------------------------ text entities -> HTML ----
_TAGS = {
    "bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"), "underline": ("<u>", "</u>"),
    "strikethrough": ("<s>", "</s>"), "code": ("<code>", "</code>"),
    "pre": ("<pre>", "</pre>"), "spoiler": ("<tg-spoiler>", "</tg-spoiler>"),
}


def _u16_to_index(text, u16):
    i = count = 0
    while count < u16 and i < len(text):
        count += 2 if ord(text[i]) > 0xFFFF else 1
        i += 1
    return i


def entities_to_html(text, entities):
    """Rebuild a message's text as HTML, keeping links and formatting."""
    text = text or ""
    if not entities:
        return html.escape(text, quote=False)
    spans = []
    for e in entities:
        start = _u16_to_index(text, e.get("offset", 0))
        end = _u16_to_index(text, e.get("offset", 0) + e.get("length", 0))
        t = e.get("type")
        if t == "text_link" and e.get("url"):
            spans.append((start, end, f'<a href="{html.escape(e["url"], quote=True)}">', "</a>"))
        elif t == "url":
            spans.append((start, end, f'<a href="{html.escape(text[start:end], quote=True)}">', "</a>"))
        elif t in _TAGS:
            o, c = _TAGS[t]
            spans.append((start, end, o, c))
    events = {}
    for start, end, o, c in spans:
        events.setdefault(start, []).append(("open", o))
        events.setdefault(end, []).append(("close", c))
    out = []
    for i in range(len(text) + 1):
        for kind, tag in events.get(i, []):
            if kind == "close":
                out.append(tag)
        for kind, tag in events.get(i, []):
            if kind == "open":
                out.append(tag)
        if i < len(text):
            out.append(html.escape(text[i], quote=False))
    return "".join(out)


def mention_html(user):
    """A clickable mention of a Telegram user (works even without a username)."""
    user = user or {}
    name = esc((user.get("first_name") or "") +
               (" " + user.get("last_name") if user.get("last_name") else "")) or "there"
    if user.get("username"):
        return f"@{esc(user['username'])}"
    if user.get("id"):
        return f'<a href="tg://user?id={user["id"]}">{name}</a>'
    return name
