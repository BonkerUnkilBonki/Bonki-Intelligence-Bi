"""Semantic search with pretrained embeddings - the "deep learning" part.

This uses a **pretrained neural embedding model** (a transformer) through the
provider you already configured (Gemini or any OpenAI-compatible endpoint) and
compares meaning, not just words. It is genuine deep-learning *inference*.

What it is NOT: we do not **train** a model. There is no GPU here, no labelled
corpus, and training on a handful of file names would produce something worse
than the text matching we already have. So we use someone else's trained model
and never claim otherwise.

Design: two-stage retrieval, which keeps it fast and cheap.
  1. the existing fuzzy + TF-IDF ranking picks a shortlist
  2. only those ~30 candidates are embedded, plus the query, and re-scored by
     cosine similarity
Vectors are cached in memory by text hash, so repeated searches cost nothing.
"""
import hashlib
import logging
import math

from . import db, llm

log = logging.getLogger("embed")

_MAX_CANDIDATES = 30
_cache = {}                       # text-hash -> vector
_MAX_CACHE = 4000


def enabled():
    return llm.enabled()


def _key(text):
    return hashlib.sha1((text or "").encode("utf-8", "ignore")).hexdigest()


def _remember(k, vec):
    if len(_cache) > _MAX_CACHE:
        _cache.clear()
    _cache[k] = vec


def embed_many(texts):
    """Embed several texts in one API round-trip. Returns {text: vector}."""
    todo, out = [], {}
    for t in texts:
        k = _key(t)
        if k in _cache:
            out[t] = _cache[k]
        elif t not in todo:
            todo.append(t)
    if not todo:
        return out
    vecs = llm.embed(todo)
    if not vecs:
        return out
    for text, vec in zip(todo, vecs):
        if vec:
            _remember(_key(text), vec)
            out[text] = vec
    return out


def _cosine(a, b):
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(x * x for x in a[:n]))
    nb = math.sqrt(sum(x * x for x in b[:n]))
    return dot / (na * nb) if na and nb else 0.0


def _doc_text(f):
    return f"{f.get('name', '')} {f.get('caption', '')} {' '.join(f.get('tags') or [])}".strip()


def rerank(query, ranked, limit=_MAX_CANDIDATES):
    """Blend semantic similarity into an already-ranked list. Returns the list."""
    if not enabled() or not ranked:
        return ranked
    cands = ranked[:limit]
    docs = [_doc_text(f)[:600] for _, f in cands]
    try:
        vecs = embed_many([query] + docs)
    except Exception as e:
        log.info("embedding failed, keeping text ranking: %s", e)
        return ranked
    qv = vecs.get(query)
    if not qv:
        return ranked

    out = []
    for (score, f), doc in zip(cands, docs):
        sim = _cosine(qv, vecs.get(doc) or [])
        out.append((score + 0.45 * max(0.0, sim), f))
    out.sort(key=lambda x: (-x[0], -(x[1].get("date") or 0)))
    # keep anything beyond the shortlist untouched, after the re-ranked part
    return out + ranked[limit:]
