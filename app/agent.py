"""The agent - Bonki's intelligence.

Given a plain-English instruction from a group ("Bonki, find apple music apk",
"scan this group and tag apple music apk", "find Antigravity-bypass.apk"), it
works out the intent, searches the group's file index with fuzzy matching, picks
the file that best fits, and replies the way the user asked for: re-posting the
file, @mentioning whoever asked, and adding a hashtag.

Two things matter here:

  * **Matching** separates *distinctive* words ("antigravity") from *type* words
    ("apk"). A type word alone never selects a file, so "apple music apk" won't
    fall back to some unrelated APK. Distinctive words match fuzzily, so typos
    and partial names still work.
  * **I/O is off the event loop.** Telegram, the LLM and the state store are all
    blocking; every call is offloaded to a thread so the bot never freezes and
    always answers.

The brain is optional. With no LLM key it still works, using deterministic
keyword matching for intent parsing and the same fuzzy file ranking.
"""
import asyncio
import difflib
import logging
import os
import re
import tempfile
import time

from . import approval, db, indexer, learn, llm, telegram, userbot

log = logging.getLogger("agent")

try:                                    # optional, much better fuzzy matching
    from rapidfuzz import fuzz as _rf
except Exception:                       # pragma: no cover - graceful fallback
    _rf = None

INTENTS = ("find", "list", "count", "stats", "summarise", "scan", "help", "unknown")

# words that carry no search value
_STOP = {
    "the", "a", "an", "please", "pls", "plz", "can", "you", "could", "would", "me",
    "my", "i", "want", "need", "get", "give", "send", "show", "find", "search",
    "for", "of", "to", "is", "it", "this", "that", "these", "those", "from", "in",
    "on", "at", "and", "or", "any", "there", "do", "have", "has", "with", "tag",
    "tagged", "scan", "group", "here", "file", "files", "latest", "new", "newest",
    "old", "oldest", "app", "apps", "kindly", "bro", "bhai", "yaar",
    "hey", "hi", "hello", "yo", "ok", "okay", "sup", "dude", "mate",
}
# file-type words: hints, never the deciding signal on their own. Broad on
# purpose - a query like "keybox.xml" should treat "xml" as a type, not a name.
_EXT_WORDS = {
    "apk", "apks", "xapk", "xapks", "apkm", "obb", "ipa", "aab",
    "zip", "zips", "7z", "rar", "tar", "gz", "tgz", "bz2", "xz",
    "iso", "img", "bin", "dat", "pak", "deb", "rpm", "exe", "msi", "dmg", "elf",
    "sh", "bat", "cmd", "ps1", "py", "js", "lua", "json", "xml", "yaml", "yml",
    "ini", "cfg", "conf", "log", "csv", "tsv", "txt", "md", "sqlite", "db",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "png", "jpg", "jpeg", "webp", "gif", "svg", "ico",
    "mp3", "wav", "flac", "m4a", "mp4", "mkv", "avi", "mov", "srt", "torrent",
}

_SYSTEM = (
    "You are Bonki, a Telegram group assistant that keeps an index of the files "
    "(APKs, zips, etc.) shared in a group and helps people find them. "
    "You turn a short instruction into JSON. Reply with ONLY JSON, no prose."
)

MIN_SCORE = 0.30          # below this, we say we found nothing


# ------------------------------------------------------------ small utils ---
def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9#. ]+", " ", (s or "").lower())).strip()


def tokens(s):
    return [t for t in norm(s).split() if t and t not in _STOP]


def canon_ext(word):
    w = (word or "").lower().lstrip(".")
    if w.endswith("s") and w[:-1] in _EXT_WORDS:
        return w[:-1]
    return w


def _ratio(a, b):
    if not a or not b:
        return 0.0
    if _rf:
        return _rf.ratio(a, b)
    return difflib.SequenceMatcher(None, a, b).ratio() * 100


def _partial(a, b):
    if not a or not b:
        return 0.0
    if _rf:
        return _rf.partial_ratio(a, b)
    return difflib.SequenceMatcher(None, a, b).ratio() * 100


def _camel(words):
    out = ""
    for w in words:
        w = re.sub(r"[^a-z0-9]", "", w.lower())
        if w:
            out += w[0].upper() + w[1:]
    return out


def make_hashtag(query, name=""):
    def clean(words):
        out = []
        for w in words:
            w = re.sub(r"\.[a-z0-9]{1,6}$", "", w)     # drop a trailing .ext
            if w and w not in _EXT_WORDS:
                out.append(w)
        return out
    words = clean(tokens(query))
    if not words and name:
        words = clean(tokens(_name_core(name)))
    return "#" + (_camel(words[:4]) or "File")


def normalize_hashtag(raw, query="", name=""):
    raw = (raw or "").strip()
    if raw:
        raw = raw.split()[0].lstrip("#")
        if re.fullmatch(r"[A-Za-z0-9]{1,40}", raw) and any(c.isupper() for c in raw):
            return "#" + raw
        camel = _camel(re.split(r"[\s_\-]+", raw))
        if camel:
            return "#" + camel
    return make_hashtag(query, name)


# ------------------------------------------------------------- file match ---
def _name_core(name):
    """Filename without its extension."""
    name = (name or "").strip()
    return re.sub(r"\.[A-Za-z0-9]{1,6}$", "", name) or name


def _hay(f):
    """The searchable text for one indexed file: full name + caption + tags."""
    return norm(f"{f.get('name', '')} {f.get('caption', '')} "
                f"{' '.join(f.get('tags') or [])}")


def _tok_hit(t, hay_toks, hay):
    """Does query token `t` appear in the file, allowing typos and prefixes?"""
    if t in hay:
        return True
    for ht in hay_toks:
        if len(t) >= 3 and (ht.startswith(t) or t.startswith(ht)):
            return True
        if len(t) >= 4 and _ratio(t, ht) >= 85:      # typo tolerance
            return True
    return False


def _score(q, f):
    """Relevance of one file to one normalised query string (0 = not a match).

    Type words ("apk", "xml") never decide a match on their own - only
    distinctive words do. A file with none of the distinctive words scores 0.
    A near-exact filename match gets a strong bonus.
    """
    ext_hint = None
    m = re.search(r"\.([a-z0-9]{1,6})$", q)
    if m and m.group(1) in _EXT_WORDS:
        ext_hint = m.group(1)
        q_core = re.sub(r"\.[a-z0-9]{1,6}$", "", q)
    else:
        q_core = q
    qtok = tokens(q_core)
    type_toks = {canon_ext(t) for t in qtok if canon_ext(t) in _EXT_WORDS}
    if ext_hint:
        type_toks.add(ext_hint)
    name_toks = [t for t in qtok if t not in _EXT_WORDS]

    hay = _hay(f)
    hay_toks = hay.split()
    ncore = norm(_name_core(f.get("name", "")))
    fr = max(_ratio(q_core, hay), _ratio(q_core, ncore)) / 100.0

    if name_toks:
        cov = sum(1 for t in name_toks if _tok_hit(t, hay_toks, hay)) / len(name_toks)
        if cov == 0:
            return 0.0
        score = 0.60 * cov + 0.30 * fr + 0.10 * (1.0 if cov >= 0.999 else 0.0)
    else:
        score = 0.60 * fr
    # a near-exact filename match should always win
    if len(q_core) >= 3 and (q_core in ncore or ncore in q_core):
        score += 0.25
    if type_toks:
        score += 0.08 if f.get("ext") in type_toks else -0.20
    return score


def rank(query, files, exts=None, variants=None):
    """Score index entries against a query. Returns [(score, file)] best first.

    `variants` are alternative query strings (e.g. LLM-suggested synonyms); each
    file takes its best score across the main query and the variants.
    """
    qs = [norm(query)] + [norm(v) for v in (variants or []) if v]
    scored = []
    for f in files:
        if exts and f.get("ext") not in exts:
            continue
        s = max((_score(q, f) for q in qs), default=0.0)
        if s > 0:
            scored.append((s, f))
    scored.sort(key=lambda x: (-x[0], -(x[1].get("date") or 0)))
    return scored


async def _llm_pick(query, candidates):
    if not llm.enabled() or len(candidates) < 2:
        return candidates[0][1] if candidates else None
    listing = "\n".join(
        f"{i}. {f['name']} [{f.get('ext')}] size={human_size(f.get('size'))} "
        f"tags={f.get('tags')} caption={(f.get('caption') or '')[:60]}"
        for i, (_, f) in enumerate(candidates[:12]))
    prompt = (
        f'The user wants: "{query}"\n\n'
        f"Candidate files (pick the single best match):\n{listing}\n\n"
        'Reply with ONLY JSON: {"index": <number of the best match, or -1 if none '
        'really fit>, "why": "<a few words>"}')
    data = await asyncio.to_thread(llm.complete_json, prompt, system=_SYSTEM)
    if isinstance(data, dict):
        try:
            i = int(data.get("index"))
        except (TypeError, ValueError):
            i = -1
        if 0 <= i < len(candidates):
            return candidates[i][1]
    return candidates[0][1]


async def _llm_expand(query):
    """Ask the brain for aliases/synonyms to widen a weak search."""
    if not llm.enabled():
        return []
    prompt = (
        f'A user is searching a Telegram group\'s file list for: "{query}".\n'
        "Give up to 5 alternative search terms or common aliases that might appear "
        'in a file name (e.g. "wa" -> "whatsapp", "yt" -> "youtube"). '
        'Reply with ONLY JSON: {"terms": ["...", "..."]}')
    data = await asyncio.to_thread(llm.complete_json, prompt, system=_SYSTEM)
    if isinstance(data, dict) and isinstance(data.get("terms"), list):
        return [str(x) for x in data["terms"] if str(x).strip()][:5]
    return []


async def find_best(query, chat_id):
    files = db.list_files(chat_id=chat_id)
    if not files:
        return None, []
    ranked = rank(query, files)
    # weak result? let the brain suggest synonyms and re-rank.
    if (not ranked or ranked[0][0] < 0.60) and llm.enabled():
        aliases = await _llm_expand(query)
        if aliases:
            ranked2 = rank(query, files, variants=aliases)
            if ranked2 and (not ranked or ranked2[0][0] > ranked[0][0] + 0.01):
                log.info("search widened via aliases %s", aliases)
                ranked = ranked2
    # blend in what the bot has learned: TF-IDF similarity + feedback associations
    ranked = learn.rerank(ranked, files, query, chat_id)
    if not ranked or ranked[0][0] < MIN_SCORE:
        return None, []
    best_score, best = ranked[0]
    best = await _llm_pick(query, ranked) or best
    others = [f for s, f in ranked[1:6] if s >= max(MIN_SCORE, best_score * 0.6)]
    return best, others


# ------------------------------------------------------------- intent parse -
async def parse(text):
    hint = await _llm_parse(text)
    return hint or _heuristic_parse(text)


async def _llm_parse(text):
    if not llm.enabled():
        return None
    files = db.list_files()
    sample = "\n".join(f"- {f['name']} [{f.get('ext')}] tags={f.get('tags')}"
                       for f in files[:25]) or "(index is empty)"
    prompt = (
        "Intents:\n"
        "- find: the user wants one specific file (get/send/show/give/find/tag a "
        "named file). The name may be given with or without a type, e.g. 'apple "
        "music apk' or just 'Antigravity-bypass.apk'.\n"
        "- list: show the files (optionally filtered, e.g. 'list apks')\n"
        "- count: how many files (optionally filtered)\n"
        "- stats: breakdown of the group's files\n"
        "- summarise: summarise what's been shared recently\n"
        "- scan: (re)scan the group's history\n"
        "- help: what can you do\n"
        "- unknown\n\n"
        f"Some files already indexed in this group:\n{sample}\n\n"
        f'User instruction: "{text}"\n\n'
        "Reply with ONLY this JSON:\n"
        '{"intent":"...","query":"<the file/thing they want, or the filter; keep '
        'only the distinctive words - drop the file type and words like \'in this '
        'group\'; empty if none>",'
        '"hashtag":"<#Tag the user asked for, else a short CamelCase tag for the '
        'file, else empty>",'
        '"scan":<true if they asked to scan/index the group>,'
        '"reply":"<one short friendly sentence to say while working, else empty>"}')
    data = await asyncio.to_thread(llm.complete_json, prompt, system=_SYSTEM)
    if not isinstance(data, dict):
        return None
    intent = str(data.get("intent") or "unknown").lower().strip()
    if intent not in INTENTS:
        intent = "unknown"
    scan = bool(data.get("scan")) or intent == "scan" or "scan" in norm(text)
    q = str(data.get("query") or "").strip()
    return {
        "intent": "scan" if intent == "scan" else intent,
        "query": q,
        "hashtag": normalize_hashtag(data.get("hashtag"), q),
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
            and not re.search(r"\b(get|send|give|find|tag|download)\b", low):
        intent = "list"
    elif re.search(r"\b(get|send|give|find|search|tag|upload|share|need|want|download|looking for)\b", low):
        intent = "find"
    elif scan:
        intent = "scan"
    else:
        intent = "find" if tokens(text) else "unknown"

    words = [w for w in re.split(r"\s+", (text or "").strip()) if w]
    qwords = [w for w in words if norm(w) not in _STOP and not w.startswith("#")
              and not w.startswith("@")]
    query = " ".join(qwords).strip()
    hashtag = normalize_hashtag(hashtag_m.group(0) if hashtag_m else "", query)
    return {"intent": intent, "query": query, "hashtag": hashtag, "scan": scan, "reply": ""}


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
        lines += ["", telegram.esc(cap[:200])]
    return _with_footer("\n".join(lines))


# ---- non-blocking I/O wrappers (Telegram + state store are blocking) ----
async def _send(text, chat_id, reply_to=None, reply_markup=None):
    return await asyncio.to_thread(telegram.send_message, text, chat_id=chat_id,
                                   reply_to=reply_to, reply_markup=reply_markup)


async def _send_doc(path, filename, caption, chat_id, reply_to=None):
    return await asyncio.to_thread(telegram.send_document, path, filename, caption,
                                   chat_id=chat_id, reply_to=reply_to)


async def _typing(chat_id):
    return await asyncio.to_thread(telegram.send_chat_action, chat_id)


async def _flush():
    try:
        await asyncio.to_thread(db.flush, True)
    except Exception:
        pass


def handle_of(chat):
    return (db.get_group_by_chat(chat.get("id"), chat.get("username")) or {}).get("handle") \
        or chat.get("id")


async def _send_link(chat, msg, user, file_row, hashtag):
    """Last-resort reply: the file's details and a link to the original post."""
    link = _link(chat, file_row.get("message_id"))
    body = [f"📦 <b>{telegram.esc(file_row.get('name'))}</b>"]
    if hashtag:
        body.append(telegram.esc(hashtag))
    if user:
        body.append(f"👤 {telegram.mention_html(user)}")
    if file_row.get("size"):
        body.append(f"📁 {human_size(file_row['size'])}")
    if link:
        body.append(f'🔗 <a href="{link}">open the original message</a>')
    await _send(_with_footer("\n".join(body), telegram.MAX_MESSAGE), chat.get("id"),
                reply_to=msg.get("message_id"))


async def _reply_with_file(chat, msg, user, file_row, hashtag):
    """Share the actual file - not a link. Tries, in order:

      1. server-side copy by the bot  - any size, no re-upload, appears as Bonki
      2. upload by the bot            - files up to the configured size limit
      3. forward via the user account - any size, server-side (the account shares)
      4. a link                       - only if every route above fails
    """
    s = db.get_settings()
    chat_id = chat.get("id")
    reply_to = msg.get("message_id")
    mid = file_row.get("message_id")
    size = file_row.get("size") or 0
    limit_bytes = float(s.get("max_upload_mb") or 50) * 1024 * 1024
    caption = _caption(file_row, user, hashtag)
    name = file_row.get("name") or "file"

    if str(s.get("reply_with_file") or "1") != "1":
        return await _send_link(chat, msg, user, file_row, hashtag)

    # 1) server-side copy by the bot - full size, no re-upload, from Bonki
    try:
        await asyncio.to_thread(telegram.copy_message, from_chat_id=chat_id,
                                message_id=mid, chat_id=chat_id, caption=caption,
                                reply_to=reply_to)
        return "copied"
    except Exception as e:
        log.info("bot copyMessage failed (%s); trying upload/forward", e)

    if userbot.is_running():
        # 2) small enough to upload as the bot (so it still comes from Bonki)
        if not size or size <= limit_bytes:
            tmp = os.path.join(tempfile.gettempdir(), f"bonki_{mid}_{int(time.time())}")
            try:
                path = await userbot.download(handle_of(chat), mid, tmp)
                if path:
                    await _send_doc(path, name, caption, chat_id, reply_to=reply_to)
                    _cleanup(path)
                    return "file"
            except Exception as e:
                log.warning("upload route failed: %s", e)
                _cleanup(tmp)
        # 3) too big to upload: the account shares it server-side (up to 2 GB)
        try:
            await userbot.forward(handle_of(chat), mid, chat_id, reply_to=reply_to,
                                  caption=caption)
            return "forwarded"
        except Exception as e:
            log.warning("forward route failed: %s", e)

    await _send_link(chat, msg, user, file_row, hashtag)
    return "link"


def _cleanup(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ------------------------------------------------------------ dispatchers ---
async def handle(chat, msg, user, text):
    """Handle one addressed group instruction end-to-end."""
    chat_id = chat.get("id")
    if db.is_paused() or not _group_allowed(chat_id):
        return
    if approval.gate(chat_id, chat.get("username")):
        return                      # not verified by the owner yet - do nothing
    text = (text or "").strip()
    if text.startswith("/"):                     # explicit /commands
        parts = text.split(None, 1)
        cmd = parts[0].lstrip("/").split("@")[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("start", "help"):
            return await _help(chat, msg)
        if cmd == "scan":
            return await _do_scan(chat, msg)
        if cmd in ("list", "files"):
            return await _list_like("list", chat, msg, rest)
        if cmd == "stats":
            return await _list_like("stats", chat, msg, rest)
        text = (rest or cmd) if cmd == "find" else f"{cmd} {rest}".strip()
    parsed = await parse(text)
    intent, query, hashtag, scan = (parsed["intent"], parsed["query"],
                                    parsed["hashtag"], parsed["scan"])
    log.info("agent: intent=%s query=%r scan=%s chat=%s", intent, query, scan, chat_id)

    await _typing(chat_id)

    if intent == "help":
        return await _help(chat, msg)
    if intent == "scan":
        return await _do_scan(chat, msg)
    if intent == "summarise":
        return await _summarise(chat, msg)
    if intent in ("list", "count", "stats"):
        return await _list_like(intent, chat, msg, query)
    if intent == "find" or (intent == "unknown" and query):
        return await _do_find(chat, msg, user, query, hashtag, scan)
    return await _help(chat, msg)


def _group_allowed(chat_id):
    raw = (db.get_settings().get("allowed_groups") or "").strip()
    if not raw:
        return True
    return str(chat_id) in {x.strip() for x in raw.split(",") if x.strip()}


async def _do_scan(chat, msg):
    row = db.get_group_by_chat(chat.get("id"), chat.get("username"))
    if not row:
        row = db.get_group(db.add_group(chat.get("id"), chat.get("title") or "",
                                        chat.get("username") or ""))
    if not userbot.is_running():
        await _send("I can't read the group's history without the user account "
                    "connected (a bot has no access to old messages). Log in at "
                    "/userbot, or post the files here and I'll index them as they arrive.",
                    chat.get("id"), reply_to=msg.get("message_id"))
        return
    await _send("🔎 Scanning this group's history…", chat.get("id"),
                reply_to=msg.get("message_id"))
    res = await indexer.scan(row)
    if res.get("error"):
        await _send(f"Scan failed: {telegram.esc(res['error'])}", chat.get("id"),
                    reply_to=msg.get("message_id"))
        return
    total = db.count_files(chat.get("id"))
    await _send(f"✅ Done — indexed <b>{total}</b> file(s) from this group "
                f"({res['added']} new). Ask me for one, e.g. "
                f"<i>“Bonki, find apple music apk”</i>.", chat.get("id"),
                reply_to=msg.get("message_id"))


async def _do_find(chat, msg, user, query, hashtag, scan):
    chat_id = chat.get("id")
    if not query:
        await _send("What file are you after? e.g. <i>Bonki, find apple music apk</i>",
                    chat_id, reply_to=msg.get("message_id"))
        return

    if (scan or db.count_files(chat_id) == 0) and userbot.is_running():
        row = db.get_group_by_chat(chat_id, chat.get("username"))
        if row:
            await indexer.scan(row)

    best, others = await find_best(query, chat_id)
    if not best:
        if db.count_files(chat_id) == 0:
            await _send("I haven't indexed any files from this group yet. Say "
                        "<i>“Bonki, scan this group”</i> (with the user account "
                        "connected) or just post the files here.", chat_id,
                        reply_to=msg.get("message_id"))
        else:
            await _send(f"Couldn't find anything matching “{telegram.esc(query)}”. "
                        f"I have {db.count_files(chat_id)} file(s) indexed — try "
                        f"<i>list files</i>.", chat_id, reply_to=msg.get("message_id"))
        return

    hashtag = normalize_hashtag(hashtag, query, best.get("name"))
    if str(db.get_settings().get("persist_tags") or "1") == "1" and hashtag:
        db.add_tag(best.get("chat_id"), best.get("message_id"), hashtag)

    how = await _reply_with_file(chat, msg, user, best, hashtag)
    db.log_activity("find", chat_id, f"{best.get('name')} ({how}) for {query}")
    await _flush()

    # feedback buttons: the candidates the user can confirm - this is the
    # signal the bot learns from (which file a real query actually wanted).
    cands = [best] + [f for f in others if _fkey(f) != _fkey(best)][:3]
    token = learn.remember_search(chat_id, query, cands, user,
                                  msg.get("message_id"), hashtag)
    rows = [[{"text": f"{i + 1}. {f.get('name')}"[:60],
              "callback_data": f"fb|p|{token}|{i}"}] for i, f in enumerate(cands)]
    rows.append([{"text": "❌ None of these", "callback_data": f"fb|n|{token}"}])
    head = ""
    if others:
        parts = []
        for f in others[:4]:
            nm = telegram.esc(f["name"])
            link = _link(chat, f.get("message_id"))
            parts.append(f'<a href="{link}">{nm}</a>' if link else nm)   # link in the name
        head = "🔎 <b>Other close matches:</b> " + ", ".join(parts) + "\n\n"
    resp = await _send(head + "<i>Tap the right file so I learn — or “None”.</i>",
                       chat_id, reply_to=msg.get("message_id"),
                       reply_markup={"inline_keyboard": rows})
    sent_id = ((resp or {}).get("result") or {}).get("message_id")
    if sent_id:
        asyncio.create_task(_autodelete_close_matches(chat_id, sent_id, token))


async def _autodelete_close_matches(chat_id, message_id, token):
    """Delete the 'other close matches' message if the user never taps a choice.

    A tap removes the pending search (learn.drop_pending), so a still-pending
    token at the deadline means nothing was selected - then the message goes.
    """
    try:
        minutes = float(db.get_settings().get("feedback_delete_minutes") or 10)
    except (TypeError, ValueError):
        minutes = 10.0
    if minutes <= 0:
        return
    await asyncio.sleep(minutes * 60)
    if learn.get_pending(token) is None:
        return                      # a choice was made - keep the message
    try:
        await asyncio.to_thread(telegram.delete_message, chat_id, message_id)
        log.info("auto-deleted close-matches message in %s", chat_id)
    except Exception as e:
        log.warning("could not auto-delete close-matches message: %s", e)
    finally:
        learn.drop_pending(token)


async def handle_feedback(token, idx, positive, chat):
    """A user confirmed a file (or rejected them all) - learn from it."""
    ctx = learn.get_pending(token)
    if not ctx:
        return "expired"
    files = ctx.get("files") or []
    if positive and 0 <= idx < len(files):
        chosen = files[idx]
        learn.record(ctx["chat_id"], ctx["query"], chosen, True, source="button")
        for i, f in enumerate(files):                 # the rest were not it
            if i != idx:
                learn.record(ctx["chat_id"], ctx["query"], f, False, source="button")
        await _reply_with_file(chat, {"message_id": ctx.get("reply_to")},
                               ctx.get("user"), chosen, ctx.get("hashtag"))
        learn.drop_pending(token)
        await _flush()
        return "shared"
    for f in files:                                   # "None" - none of them
        learn.record(ctx["chat_id"], ctx["query"], f, False, source="button")
    learn.drop_pending(token)
    await _flush()
    return "noted"


def _fkey(f):
    return f"{f.get('chat_id')}:{f.get('message_id')}"


async def _list_like(intent, chat, msg, query):
    chat_id = chat.get("id")
    files = db.list_files(chat_id=chat_id)
    ext = _ext_filter(query)
    if intent == "count":
        n = len([f for f in files if not ext or f.get("ext") in ext])
        label = f" matching {', '.join(sorted(ext))}" if ext else ""
        await _send(f"📊 I have <b>{n}</b> file(s){label} indexed from this group.",
                    chat_id, reply_to=msg.get("message_id"))
        return
    if intent == "stats":
        return await _stats(chat, msg, files)
    sel = [f for f in files if not ext or f.get("ext") in ext]
    sel.sort(key=lambda f: -(f.get("date") or 0))
    if not sel:
        await _send("Nothing indexed yet — say <i>“Bonki, scan this group”</i>.",
                    chat_id, reply_to=msg.get("message_id"))
        return
    lines = [f"🗂 <b>{len(sel)}</b> file(s):"]
    for f in sel[:30]:
        tags = " ".join(f.get("tags") or [])
        lines.append(f"• {telegram.esc(f['name'])} — {human_size(f.get('size'))} {tags}".rstrip())
    if len(sel) > 30:
        lines.append(f"…and {len(sel) - 30} more.")
    await _send("\n".join(lines)[:telegram.MAX_MESSAGE], chat_id,
                reply_to=msg.get("message_id"))


def _ext_filter(query):
    return {canon_ext(t) for t in tokens(query or "") if canon_ext(t) in _EXT_WORDS}


async def _stats(chat, msg, files):
    chat_id = chat.get("id")
    if not files:
        await _send("Nothing indexed yet — say <i>“Bonki, scan this group”</i>.",
                    chat_id, reply_to=msg.get("message_id"))
        return
    by_ext, by_sender, total = {}, {}, 0
    for f in files:
        by_ext[f.get("ext") or "?"] = by_ext.get(f.get("ext") or "?", 0) + 1
        s = f.get("sender_name") or "unknown"
        by_sender[s] = by_sender.get(s, 0) + 1
        total += f.get("size") or 0
    top_ext = sorted(by_ext.items(), key=lambda x: -x[1])[:8]
    top_sender = sorted(by_sender.items(), key=lambda x: -x[1])[:5]
    lines = ["📊 <b>Group file stats</b>",
             f"Total: <b>{len(files)}</b> file(s), {human_size(total)}", "",
             "By type: " + ", ".join(f"{telegram.esc(e)}: {n}" for e, n in top_ext),
             "Top sharers: " + ", ".join(f"{telegram.esc(s)}: {n}" for s, n in top_sender)]
    await _send("\n".join(lines), chat_id, reply_to=msg.get("message_id"))


async def _summarise(chat, msg):
    chat_id = chat.get("id")
    files = sorted(db.list_files(chat_id=chat_id), key=lambda f: -(f.get("date") or 0))[:40]
    if not files:
        await _send("Nothing to summarise yet — I haven't indexed any files here.",
                    chat_id, reply_to=msg.get("message_id"))
        return
    listing = "\n".join(
        f"- {f['name']} ({f.get('ext')}, {human_size(f.get('size'))}) by "
        f"{f.get('sender_name') or 'unknown'}" for f in files)
    if llm.enabled():
        out = await asyncio.to_thread(
            llm.complete,
            "Summarise what has been shared in this Telegram group, in 3-5 short "
            "bullets, grouped sensibly (apps, tools, etc.). Be concrete.\n\n"
            f"Recent files:\n{listing}", system=_SYSTEM, max_tokens=400)
        if out:
            await _send(f"🧠 <b>Recent activity</b>\n{telegram.esc(out)}", chat_id,
                        reply_to=msg.get("message_id"))
            return
    lines = ["🧠 <b>Recent files shared</b>"] + [f"• {telegram.esc(f['name'])}"
                                                for f in files[:15]]
    await _send("\n".join(lines), chat_id, reply_to=msg.get("message_id"))


async def _help(chat, msg):
    await _send(
        "👋 I'm <b>Bonki</b>. I index the files shared in this group and find them.\n\n"
        "Try:\n"
        "• <i>Bonki, find apple music apk</i>\n"
        "• <i>Bonki, find Antigravity-bypass.apk</i> — just the name works, no file "
        "type needed\n"
        "• <i>Bonki, scan this group</i>\n"
        "• <i>Bonki, list apks</i> · <i>how many zips</i> · <i>group stats</i>\n"
        "• <i>Bonki, summarise what's been shared</i>\n\n"
        "When I find a file I re-post it, @mention you, and tag it with a hashtag. "
        "Spelling doesn't have to be exact.",
        chat.get("id"), reply_to=msg.get("message_id"))
