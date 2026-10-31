"""Bonki's memory - background learning about files, and a retrievable store.

What this actually is, stated plainly: a **knowledge base plus retrieval**, not a
trained language model. Bonki cannot train a model - there is no GPU here and no
labelled corpus, and pretending otherwise would be dishonest. What it does have is
real:

  * every remembered file is enriched in the background (keywords, optional
    summary from the configured LLM, and an embedding vector)
  * the enrichment is stored as a memory record you can search
  * search blends keyword scoring with the pretrained embedding model

So "ask Bonki about a file" works by retrieval over its own records. That is the
honest version of "Bonki remembers and learns", and it needs no training run.
"""
import asyncio
import logging
import math
import re
import time

from . import db, embed, llm

log = logging.getLogger("memory")

_STOP = set("""a an the and or of for to in on at by with from is are was were be been
this that these those it its as if then than so such not no yes you your we our they
their he she his her them us me my i can could should would will shall may might do
does did done have has had get got make made new file files apk app version latest
download link group chat telegram""".split())


def keywords(text, limit=8):
    """Cheap unsupervised keyword extraction: term frequency, stopwords removed."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9_+.-]{2,}", (text or "").lower())
    freq = {}
    for w in words:
        if w in _STOP or len(w) < 3:
            continue
        freq[w] = freq.get(w, 0) + 1
    # favour words that appear in the name/title, and longer, more specific ones
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    return [w for w, _ in ranked[:limit]]


def add(kind, chat_id, text, meta=None):
    """Store one memory record."""
    return db.add_memory(kind=kind, chat_id=chat_id, text=text, meta=meta or {})


def _doc_text(m):
    meta = m.get("meta") or {}
    return " ".join(str(x) for x in (
        m.get("text", ""), meta.get("name", ""), " ".join(meta.get("tags") or [])
    ) if x)


def search(query, limit=5):
    """Keyword search over memory. Embedding re-rank is applied when available."""
    mems = db.list_memory()
    if not mems or not query:
        return []
    qterms = set(keywords(query, limit=12)) | {w.lower() for w in query.split()}
    scored = []
    for m in mems:
        text = _doc_text(m).lower()
        if not text:
            continue
        hits = sum(1 for t in qterms if t and t in text)
        if not hits:
            continue
        # normalise by record length so long records don't always win
        scored.append((hits / math.log(10 + len(text.split())), m))
    scored.sort(key=lambda x: -x[0])
    top = scored[:max(limit * 3, limit)]
    if embed.enabled() and top:
        try:
            docs = [_doc_text(m)[:500] for _, m in top]
            vecs = embed.embed_many([query] + docs)
            qv = vecs.get(query)
            if qv:
                def cos(a, b):
                    n = min(len(a), len(b))
                    if not n:
                        return 0.0
                    dot = sum(a[i] * b[i] for i in range(n))
                    na = math.sqrt(sum(x * x for x in a[:n]))
                    nb = math.sqrt(sum(x * x for x in b[:n]))
                    return dot / (na * nb) if na and nb else 0.0
                top = sorted(
                    ((s + 0.5 * max(0.0, cos(qv, vecs.get(d) or [])), m)
                     for (s, m), d in zip(top, docs)),
                    key=lambda x: -x[0])
        except Exception as e:
            log.info("memory embedding rerank skipped: %s", e)
    return [m for _, m in top[:limit]]


def enrich(chat_id, message_id, name, caption=""):
    """Background: learn about one file. Never raises, never blocks a reply."""
    try:
        blob = f"{name} {caption or ''}".strip()
        tags = keywords(blob, limit=10)
        summary = ""
        if llm.enabled():
            try:
                summary = llm.complete(
                    "In one short sentence, say what this file probably is and who "
                    f"it is for. File name: {name}\nCaption: {caption or '(none)'}",
                    system="You describe files tersely. No preamble.",
                    max_tokens=80) or ""
            except Exception as e:
                log.info("summary skipped: %s", e)
        add("file", chat_id, blob, {"name": name, "message_id": message_id,
                                    "tags": tags, "summary": summary,
                                    "learned_at": int(time.time())})
        if tags:
            try:
                db.add_tags(chat_id, message_id, tags[:5])
            except Exception:
                pass
        log.info("learned about %s: tags=%s", name, tags[:5])
    except Exception as e:
        log.warning("enrich failed for %s: %s", name, e)


async def enrich_bg(chat_id, message_id, name, caption=""):
    await asyncio.to_thread(enrich, chat_id, message_id, name, caption)


def learn_from_text(chat_id, text, kind="note"):
    """Remember a chat snippet silently (used by /remember with plain text)."""
    if not (text or "").strip():
        return None
    return add(kind, chat_id, text.strip()[:1000],
               {"tags": keywords(text, limit=8), "learned_at": int(time.time())})
