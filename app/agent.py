"""The agent - Bonki's intelligence.

Given a plain-English instruction from a group ("scan this group and can you tag
apple music apk?"), it works out the intent, searches the group's file index,
picks the file that best fits, and replies the way the user asked for: re-posting
the file, @mentioning whoever asked, and adding a hashtag.

The brain is optional. With no LLM key it still works, using deterministic
keyword matching for both intent parsing and file ranking.
"""
import difflib
import logging
import os
import re
import tempfile
import time

from . import db, indexer, llm, telegram, userbot

log = logging.getLogger("agent")

INTENTS = ("find", "list", "count", "stats", "summarise", "scan", "help", "unknown")

# words that carry no search value once we're matching a file name
_STOP = {
    "the", "a", "an", "please", "pls", "can", "you", "could", "would", "me", "my",
    "i", "want", "need", "get", "give", "send", "show", "find", "search", "for",
    "of", "to", "is", "it", "this", "that", "from", "in", "on", "and", "or", "any",
    "there", "do", "have", "has", "with", "tag", "tagged", "scan", "group", "here",
    "file", "files", "the", "latest", "new", "newest", "old", "oldest", "app",
    "apps", "kindly", "bro", "bhai", "yaar", "plz",
}
# words that hint at a file type rather than a name
_EXT_WORDS = {"apk", "apks", "xapk", "xapks", "zip", "zips", "7z", "rar", "obb", "ipa"}


def canon_ext(word):
    """'apks' -> 'apk', 'zips' -> 'zip', else the word unchanged."""
    w = (word or "").lower().lstrip(".")
    if w.endswith("s") and w[:-1] in _EXT_WORDS:
        return w[:-1]
    return w

_SYSTEM = (
    "You are Bonki Intel, a Telegram group assistant that keeps an index of the "
    "files (APKs, zips, etc.) shared in a group and helps people find them. "
    "You turn a short instruction into JSON. Reply with ONLY JSON, no prose."
)


# ------------------------------------------------------------- text utils ---
def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9#. ]+", " ", (s or "").lower())).strip()


def tokens(s):
    return [t for t in norm(s).split() if t and t not in _STOP]


def _camel(words):
    out = ""
    for w in words:
        w = re.sub(r"[^a-z0-9]", "", w.lower())
        if w:
            out += w[0].upper() + w[1:]
    return out


def make_hashtag(query, name=""):
    """#AppleMusic from 'apple music apk' (or from the file name)."""
    base = query or name or ""
    words = [w for w in tokens(base) if w not in _EXT_WORDS]
    if not words and name:
        words = [w for w in tokens(name.rsplit(".", 1)[0]) if w not in _EXT_WORDS]
    tag = _camel(words[:4])
    return "#" + (tag or "File")


def normalize_hashtag(raw, query="", name=""):
    raw = (raw or "").strip()
    if raw:
        raw = raw.split()[0].lstrip("#")
        # already a clean CamelCase token? keep it exactly as given
        if re.fullmatch(r"[A-Za-z0-9]{1,40}", raw) and any(c.isupper() for c in raw):
            return "#" + raw
        camel = _camel(re.split(r"[\s_\-]+", raw))
        if camel:
            return "#" + camel
    return make_hashtag(query, name)


# ------------------------------------------------------------- intent parse -
def parse(text):
    """Return {intent, query, hashtag, scan, reply}."""
    hint = _llm_parse(text)
    if hint:
        return hint
    return _heuristic_parse(text)


def _llm_parse(text):
    if not llm.enabled():
        return None
    files = db.list_files()
    sample = "\n".join(f"- {f['name']} [{f.get('ext')}] tags={f.get('tags')}"
                       for f in files[:25]) or "(index is empty)"
    prompt = (
        "Intents:\n"
        "- find: the user wants one specific file (get/send/show/give/find/tag a named file)\n"
        "- list: show the files (optionally filtered, e.g. 'list apks')\n"
        "- count: how many files (optionally filtered)\n"
        "- stats: breakdown of the group's files (by type/sender)\n"
        "- summarise: summarise what's been shared recently\n"
        "- scan: (re)scan the group's history\n"
        "- help: what can you do\n"
        "- unknown\n\n"
        f"Some files already indexed in this group:\n{sample}\n\n"
        f'User instruction: "{text}"\n\n'
        "Reply with ONLY this JSON:\n"
        '{"intent":"...","query":"<the file/thing they want, or the filter; empty if none>",'
        '"hashtag":"<#Tag the user asked for, else a short CamelCase tag for the file, else empty>",'
        '"scan":<true if they asked to scan/index the group>,'
        '"reply":"<one short friendly sentence to say while working, else empty>"}'
    )
    data = llm.complete_json(prompt, system=_SYSTEM)
    if not isinstance(data, dict):
        return None
    intent = str(data.get("intent") or "unknown").lower().strip()
    if intent not in INTENTS:
        intent = "unknown"
    scan = bool(data.get("scan")) or intent == "scan" or "scan" in norm(text)
    return {
        "intent": "scan" if intent == "scan" else intent,
        "query": str(data.get("query") or "").strip(),
        "hashtag": normalize_hashtag(data.get("hashtag"), str(data.get("query") or "")),
        "scan": scan,
        "reply": str(data.get("reply") or "").strip(),
    }


def _heuristic_parse(text):
    low = norm(text)
    hashtag_m = re.search(r"#([A-Za-z0-9_]+)", text or "")
    scan = bool(re.search(r"\b(scan|index|rescan)\b", low))
    if re.search(r"\b(help|what can you do|how do you work|commands?)\b", low):
        intent = "help"
    elif re.search(r"\b(summar|recap|tl;?dr|what.s been shared)\b", low):
        intent = "summarise"
    elif re.search(r"\b(how many|count|number of|total)\b", low):
        intent = "count"
    elif re.search(r"\b(stats?|breakdown|top senders|by type)\b", low):
        intent = "stats"
    elif re.search(r"\b(list|show all|what files|which files|all apks?|all files?)\b", low) \
            and not re.search(r"\b(get|send|give|find|tag)\b", low):
        intent = "list"
    elif re.search(r"\b(get|send|give|find|search|tag|upload|share|need|want|looking for)\b", low):
        intent = "find"
    elif scan:
        intent = "scan"
    else:
        intent = "find" if tokens(text) else "unknown"

    # pull the query out of the instruction
    words = [w for w in re.split(r"\s+", (text or "").strip()) if w]
    qwords = [w for w in words if norm(w) not in _STOP and not w.startswith("#")
              and not w.startswith("@")]
    query = " ".join(qwords).strip()
    hashtag = normalize_hashtag(hashtag_m.group(0) if hashtag_m else "", query)
    return {"intent": intent, "query": query, "hashtag": hashtag, "scan": scan, "reply": ""}


# ------------------------------------------------------------- file match ---
def rank(query, files, exts=None):
    """Score index entries against a query. Returns [(score, file)] best first."""
    q = norm(query)
    qtok = tokens(query)
    ext_wanted = {canon_ext(t) for t in qtok if canon_ext(t) in _EXT_WORDS}
    scored = []
    for f in files:
        if exts and f.get("ext") not in exts:
            continue
        hay = norm(f"{f.get('name','')} {f.get('caption','')} {' '.join(f.get('tags') or [])}")
        hit = sum(1 for t in qtok if t in hay)
        token_score = (hit / len(qtok)) if qtok else 0.0
        ratio = difflib.SequenceMatcher(None, q, hay).ratio() if q else 0.0
        score = token_score * 0.7 + ratio * 0.3
        if ext_wanted and f.get("ext") in ext_wanted:
            score += 0.15
        if score > 0:
            scored.append((score, f))
    scored.sort(key=lambda x: (-x[0], -(x[1].get("date") or 0)))
    return scored


def _llm_pick(query, candidates):
    """Let the brain choose among the top fuzzy candidates."""
    if not llm.enabled() or len(candidates) < 2:
        return candidates[0][1] if candidates else None
    listing = "\n".join(
        f"{i}. {f['name']} [{f.get('ext')}] size={human_size(f.get('size'))} "
        f"tags={f.get('tags')} caption={(f.get('caption') or '')[:60]}"
        for i, (_, f) in enumerate(candidates[:12])
    )
    prompt = (
        f'The user wants: "{query}"\n\n'
        f"Candidate files (pick the single best match):\n{listing}\n\n"
        'Reply with ONLY JSON: {"index": <number of the best match, or -1 if none really fit>, '
        '"why": "<a few words>"}'
    )
    data = llm.complete_json(prompt, system=_SYSTEM)
    if isinstance(data, dict):
        try:
            i = int(data.get("index"))
        except (TypeError, ValueError):
            i = -1
        if 0 <= i < len(candidates):
            return candidates[i][1]
    return candidates[0][1]


def find_best(query, chat_id):
    files = db.list_files(chat_id=chat_id)
    if not files:
        return None, []
    ranked = rank(query, files)
    if not ranked:
        return None, []
    best = _llm_pick(query, ranked)
    return best, [f for _, f in ranked[:5]]


# --------------------------------------------------------------- replies ----
def _link(chat, message_id):
    uname = chat.get("username")
    if uname:
        return f"https://t.me/{uname}/{message_id}"
    cid = str(chat.get("id") or "")
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return None


def _footer():
    return (db.get_settings().get("reply_footer") or "").strip()


def _with_footer(text, limit=telegram.MAX_CAPTION):
    f = _footer()
    if not f:
        return text[:limit]
    budget = limit - len(f) - 2
    body = text if len(text) <= budget else text[:budget].rstrip() + "…"
    return f"{body}\n\n{f}" if body else f


def _caption(file_row, user, hashtag):
    s = db.get_settings()
    lines = [f"📦 <b>{telegram.esc(file_row.get('name'))}</b>"]
    if str(s.get("add_hashtag") or "1") == "1" and hashtag:
        lines.append(telegram.esc(hashtag))
    if str(s.get("mention_requester") or "1") == "1" and user:
        lines.append(f"👤 Requested by {telegram.mention_html(user)}")
    if file_row.get("size"):
        lines.append(f"📁 {human_size(file_row['size'])}")
    cap = file_row.get("caption")
    if cap:
        lines.append("")
        lines.append(telegram.esc(cap[:200]))
    return _with_footer("\n".join(lines))


async def _reply_with_file(chat, msg, user, file_row, hashtag):
    """Re-post the found file as a reply, or send a link if we can't fetch it."""
    s = db.get_settings()
    chat_id = chat.get("id")
    reply_to = msg.get("message_id")
    limit_bytes = float(s.get("max_upload_mb") or 50) * 1024 * 1024
    size = file_row.get("size") or 0
    caption = _caption(file_row, user, hashtag)
    want_file = str(s.get("reply_with_file") or "1") == "1"

    if want_file and userbot.is_running() and (not size or size <= limit_bytes):
        handle = (db.get_group_by_chat(chat_id) or {}).get("handle") or chat_id
        tmp = os.path.join(tempfile.gettempdir(),
                           f"bonki_{file_row.get('message_id')}_{int(time.time())}")
        try:
            path = await userbot.download(handle, file_row.get("message_id"), tmp)
            if path:
                telegram.send_document(path, file_row.get("name") or "file",
                                       caption=caption, chat_id=chat_id, reply_to=reply_to)
                _cleanup(path)
                return "file"
        except Exception as e:
            log.warning("couldn't re-post file, falling back to link: %s", e)
            _cleanup(tmp)
    elif want_file and userbot.is_running() and size > limit_bytes:
        # too big to re-upload as the bot - forward it from the account instead
        try:
            await userbot.forward(handle_of(chat), file_row.get("message_id"),
                                  chat_id, reply_to=reply_to, caption=caption)
            return "forwarded"
        except Exception as e:
            log.warning("forward fallback failed: %s", e)

    # last resort: a message with the file's details and a link to the original
    link = _link(chat, file_row.get("message_id"))
    body = [f"📦 <b>{telegram.esc(file_row.get('name'))}</b>"]
    if hashtag:
        body.append(telegram.esc(hashtag))
    if user:
        body.append(f"👤 {telegram.mention_html(user)}, I found it here:")
    if size:
        body.append(f"📁 {human_size(size)}")
    if link:
        body.append(f'🔗 <a href="{link}">open the original message</a>')
    telegram.send_message(_with_footer("\n".join(body), telegram.MAX_MESSAGE),
                          chat_id=chat_id, reply_to=reply_to)
    return "link"


def handle_of(chat):
    return (db.get_group_by_chat(chat.get("id")) or {}).get("handle") or chat.get("id")


def _cleanup(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ------------------------------------------------------------- dispatchers --
async def handle(chat, msg, user, text):
    """Handle one addressed group instruction end-to-end."""
    s = db.get_settings()
    chat_id = chat.get("id")
    if db.is_paused():
        return
    if not _group_allowed(chat_id):
        return

    parsed = parse(text)
    intent, query, hashtag, scan = (parsed["intent"], parsed["query"],
                                    parsed["hashtag"], parsed["scan"])
    log.info("agent: intent=%s query=%r scan=%s chat=%s", intent, query, scan, chat_id)

    telegram.send_chat_action(chat_id)

    if intent == "help":
        return _help(chat, msg)
    if intent == "scan":
        return await _do_scan(chat, msg)
    if intent == "summarise":
        return _summarise(chat, msg)
    if intent in ("list", "count", "stats"):
        return _list_like(intent, chat, msg, query)
    if intent == "find" or (intent == "unknown" and query):
        return await _do_find(chat, msg, user, query, hashtag, scan)
    return _help(chat, msg)


def _group_allowed(chat_id):
    raw = (db.get_settings().get("allowed_groups") or "").strip()
    if not raw:
        return True
    allowed = {x.strip() for x in raw.split(",") if x.strip()}
    return str(chat_id) in allowed


async def _do_scan(chat, msg):
    row = db.get_group_by_chat(chat.get("id"))
    if not row:
        row_id = db.add_group(chat.get("id"), chat.get("title") or "",
                              chat.get("username") or "")
        row = db.get_group(row_id)
    if not userbot.is_running():
        telegram.send_message(
            "I can't read the group's history without the user account connected "
            "(a bot has no access to old messages). Log in at /userbot, or post the "
            "files here and I'll index them as they arrive.",
            chat_id=chat.get("id"), reply_to=msg.get("message_id"))
        return
    telegram.send_message("🔎 Scanning this group's history…",
                          chat_id=chat.get("id"), reply_to=msg.get("message_id"))
    res = await indexer.scan(row)
    if res.get("error"):
        telegram.send_message(f"Scan failed: {telegram.esc(res['error'])}",
                              chat_id=chat.get("id"), reply_to=msg.get("message_id"))
        return
    total = db.count_files(chat.get("id"))
    telegram.send_message(
        f"✅ Done — indexed <b>{total}</b> file(s) from this group "
        f"({res['added']} new). Ask me for one, e.g. <i>“Bonki, get the apple music apk”</i>.",
        chat_id=chat.get("id"), reply_to=msg.get("message_id"))


async def _do_find(chat, msg, user, query, hashtag, scan):
    chat_id = chat.get("id")
    if not query:
        telegram.send_message("What file are you after? e.g. <i>get the apple music apk</i>",
                              chat_id=chat_id, reply_to=msg.get("message_id"))
        return

    # auto-scan on demand when the index is empty (or the user asked to scan)
    if (scan or db.count_files(chat_id) == 0) and userbot.is_running():
        row = db.get_group_by_chat(chat_id) or {}
        if row:
            await indexer.scan(row)

    best, others = find_best(query, chat_id)
    if not best:
        if db.count_files(chat_id) == 0:
            telegram.send_message(
                "I haven't indexed any files from this group yet. Say "
                "<i>“Bonki, scan this group”</i> (with the user account connected) "
                "or just post the files here.",
                chat_id=chat_id, reply_to=msg.get("message_id"))
        else:
            telegram.send_message(
                f"Couldn't find anything matching “{telegram.esc(query)}”. "
                f"I have {db.count_files(chat_id)} file(s) indexed — try <i>list files</i>.",
                chat_id=chat_id, reply_to=msg.get("message_id"))
        return

    hashtag = normalize_hashtag(hashtag, query, best.get("name"))
    if str(db.get_settings().get("persist_tags") or "1") == "1" and hashtag:
        db.add_tag(best.get("chat_id"), best.get("message_id"), hashtag)

    how = await _reply_with_file(chat, msg, user, best, hashtag)
    db.log_activity("find", chat_id, f"{best.get('name')} ({how}) for {query}")
    db.flush(force=True)

    # a quiet note if the match was weak / there were close alternatives
    if others and len(others) > 1 and (others[0].get("name") != others[1].get("name")):
        names = ", ".join(telegram.esc(f["name"]) for f in others[1:4])
        telegram.send_message(f"<i>Other close matches: {names}</i>",
                              chat_id=chat_id, reply_to=msg.get("message_id"))


def _list_like(intent, chat, msg, query):
    chat_id = chat.get("id")
    files = db.list_files(chat_id=chat_id)
    if intent == "count":
        ext = _ext_filter(query, files)
        n = len([f for f in files if not ext or f.get("ext") in ext])
        label = f" matching {', '.join(sorted(ext))}" if ext else ""
        telegram.send_message(f"📊 I have <b>{n}</b> file(s){label} indexed from this group.",
                              chat_id=chat_id, reply_to=msg.get("message_id"))
        return
    if intent == "stats":
        return _stats(chat, msg, files)
    # list
    ext = _ext_filter(query, files)
    sel = [f for f in files if not ext or f.get("ext") in ext]
    sel.sort(key=lambda f: -(f.get("date") or 0))
    if not sel:
        telegram.send_message("Nothing indexed yet — say <i>“Bonki, scan this group”</i>.",
                              chat_id=chat_id, reply_to=msg.get("message_id"))
        return
    lines = [f"🗂 <b>{len(sel)}</b> file(s):"]
    for f in sel[:30]:
        tags = " ".join(f.get("tags") or [])
        lines.append(f"• {telegram.esc(f['name'])} — {human_size(f.get('size'))} {tags}".rstrip())
    if len(sel) > 30:
        lines.append(f"…and {len(sel) - 30} more.")
    telegram.send_message("\n".join(lines)[:telegram.MAX_MESSAGE],
                          chat_id=chat_id, reply_to=msg.get("message_id"))


def _ext_filter(query, files):
    q = {canon_ext(t) for t in tokens(query or "") if canon_ext(t) in _EXT_WORDS}
    return q


def _stats(chat, msg, files):
    chat_id = chat.get("id")
    if not files:
        telegram.send_message("Nothing indexed yet — say <i>“Bonki, scan this group”</i>.",
                              chat_id=chat_id, reply_to=msg.get("message_id"))
        return
    by_ext, by_sender = {}, {}
    total = 0
    for f in files:
        by_ext[f.get("ext") or "?"] = by_ext.get(f.get("ext") or "?", 0) + 1
        s = f.get("sender_name") or "unknown"
        by_sender[s] = by_sender.get(s, 0) + 1
        total += f.get("size") or 0
    top_ext = sorted(by_ext.items(), key=lambda x: -x[1])[:8]
    top_sender = sorted(by_sender.items(), key=lambda x: -x[1])[:5]
    lines = [f"📊 <b>Group file stats</b>",
             f"Total: <b>{len(files)}</b> file(s), {human_size(total)}",
             "",
             "By type: " + ", ".join(f"{telegram.esc(e)}: {n}" for e, n in top_ext),
             "Top sharers: " + ", ".join(f"{telegram.esc(s)}: {n}" for s, n in top_sender)]
    telegram.send_message("\n".join(lines), chat_id=chat_id, reply_to=msg.get("message_id"))


def _summarise(chat, msg):
    chat_id = chat.get("id")
    files = sorted(db.list_files(chat_id=chat_id), key=lambda f: -(f.get("date") or 0))[:40]
    if not files:
        telegram.send_message("Nothing to summarise yet — I haven't indexed any files here.",
                              chat_id=chat_id, reply_to=msg.get("message_id"))
        return
    listing = "\n".join(
        f"- {f['name']} ({f.get('ext')}, {human_size(f.get('size'))}) by "
        f"{f.get('sender_name') or 'unknown'}" for f in files)
    if llm.enabled():
        out = llm.complete(
            "Summarise what has been shared in this Telegram group, in 3-5 short "
            "bullets, grouped sensibly (apps, tools, etc.). Be concrete.\n\n"
            f"Recent files:\n{listing}", system=_SYSTEM, max_tokens=400)
        if out:
            telegram.send_message(f"🧠 <b>Recent activity</b>\n{telegram.esc(out)}",
                                  chat_id=chat_id, reply_to=msg.get("message_id"))
            return
    lines = ["🧠 <b>Recent files shared</b>"] + [f"• {telegram.esc(f['name'])}" for f in files[:15]]
    telegram.send_message("\n".join(lines), chat_id=chat_id, reply_to=msg.get("message_id"))


def _help(chat, msg):
    telegram.send_message(
        "👋 I'm <b>Bonki</b>. I index the files shared in this group and find them for you.\n\n"
        "Try:\n"
        "• <i>Bonki, scan this group and tag apple music apk</i>\n"
        "• <i>Bonki, get the latest whatsapp mod apk</i>\n"
        "• <i>Bonki, list apks</i> · <i>how many zips</i> · <i>group stats</i>\n"
        "• <i>Bonki, summarise what's been shared</i>\n\n"
        "When I find a file I re-post it, @mention you, and tag it with a hashtag.",
        chat_id=chat.get("id"), reply_to=msg.get("message_id"))
