"""The learning layer.

Bonki learns from real usage - no pretrained weights, no GPU, nothing invented.
Two things are actually learned, both from data that arrives in your group:

  * a **TF-IDF vector model** over the files really in the group (their names,
    captions and tags). It is rebuilt as files are indexed, so retrieval gets
    better as the corpus grows.
  * **association weights** from feedback - when a user confirms which file was
    right for a query (the inline buttons under a result), that query's words are
    linked to that file. Next time those words pull it up.

`rerank()` blends the base fuzzy score with both, so ranking improves with real
files and real corrections. Everything is inspectable and runs on-device.

This is classical retrieval ML (vector-space model + a learned re-ranker), not a
neural network - and it is honest about that.
"""
import logging
import math
import secrets
import time

from . import db

log = logging.getLogger("learn")

_cache = {}        # chat_id -> {"version", "idf", "vecs", "n"}
_assoc = {}        # chat_id -> {"version", "map"}
_pending = {}      # token -> in-flight search (for the feedback buttons)
_PENDING_MAX = 500


# ---------------------------------------------------------------- helpers ---
def _key(f):
    return f"{f.get('chat_id')}:{f.get('message_id')}"


def _tokens(s):
    from . import agent                      # lazy - avoids an import cycle
    return agent.tokens(s)


def _key_tokens(s):
    """Tokens that should drive learning - distinctive words only, never file
    types (so "apk" doesn't get associated with every apk the user confirms)."""
    from . import agent
    return [t for t in agent.tokens(s) if t not in agent._EXT_WORDS]


def _version(chat_id):
    return (len(db.list_files(chat_id=chat_id)), len(db.list_feedback(chat_id=chat_id)))


def invalidate(chat_id=None):
    if chat_id is None:
        _cache.clear()
        _assoc.clear()
    else:
        _cache.pop(chat_id, None)
        _assoc.pop(chat_id, None)


# ------------------------------------------------------------- TF-IDF model --
def _doc_tf(f):
    tf = {}
    for t in _tokens(f"{f.get('name', '')} {f.get('caption', '')} "
                     f"{' '.join(f.get('tags') or [])}"):
        tf[t] = tf.get(t, 0) + 1
    return tf


def _build(chat_id):
    files = db.list_files(chat_id=chat_id)
    n = len(files)
    df, docs = {}, {}
    for f in files:
        tf = _doc_tf(f)
        docs[_key(f)] = tf
        for t in tf:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
    vecs = {}
    for k, tf in docs.items():
        v = {t: (1.0 + math.log(c)) * idf.get(t, 1.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs[k] = {t: x / norm for t, x in v.items()}
    _cache[chat_id] = {"version": _version(chat_id), "idf": idf, "vecs": vecs, "n": n}
    log.info("learn: built TF-IDF for %s (%d files, %d terms)", chat_id, n, len(idf))
    return _cache[chat_id]


def _model(chat_id):
    m = _cache.get(chat_id)
    if m is None or m["version"] != _version(chat_id):
        m = _build(chat_id)
    return m


def tfidf_scores(query, files, chat_id):
    """Cosine similarity between the query and each file's TF-IDF vector."""
    m = _model(chat_id)
    if not m["n"]:
        return {}
    qt = _tokens(query)
    if not qt:
        return {}
    qtf = {}
    for t in qt:
        qtf[t] = qtf.get(t, 0) + 1
    qv = {t: (1.0 + math.log(c)) * m["idf"].get(t, 1.0) for t, c in qtf.items()}
    norm = math.sqrt(sum(x * x for x in qv.values())) or 1.0
    qv = {t: x / norm for t, x in qv.items()}
    out = {}
    for f in files:
        dv = m["vecs"].get(_key(f))
        if not dv:
            continue
        out[_key(f)] = max(0.0, min(1.0, sum(w * dv.get(t, 0.0) for t, w in qv.items())))
    return out


# -------------------------------------------------------- learned associations
def _associations(chat_id):
    """token -> {file_key: weight} learned from confirmed / rejected searches."""
    a = _assoc.get(chat_id)
    if a is not None and a["version"] == _version(chat_id):
        return a["map"]
    acc = {}
    for ex in db.list_feedback(chat_id=chat_id):
        key = f"{ex.get('chat_id')}:{ex.get('message_id')}"
        w = 1.0 if ex.get("positive") else -0.6
        for t in set(_key_tokens(ex.get("query") or "")):
            acc.setdefault(t, {})
            acc[t][key] = acc[t].get(key, 0.0) + w
    out = {}
    for t, d in acc.items():
        d = {k: v for k, v in d.items() if v > 0}
        mx = max(d.values()) if d else 0.0
        if mx > 0:
            out[t] = {k: v / mx for k, v in d.items()}
    _assoc[chat_id] = {"version": _version(chat_id), "map": out}
    return out


# ------------------------------------------------------------------ rerank ---
def rerank(base_ranked, files, query, chat_id):
    """Blend the base fuzzy score with TF-IDF similarity and learned associations.

    Crucially the candidate set is the *union* of what each signal likes - a
    file the fuzzy matcher never saw can still surface because it is strongly
    associated with the query words from real feedback.
    """
    base = {_key(f): s for s, f in base_ranked}
    tf = tfidf_scores(query, files, chat_id)
    assoc = _associations(chat_id)
    qt = _key_tokens(query)
    out = []
    for f in files:
        k = _key(f)
        b = base.get(k, 0.0)
        t = tf.get(k, 0.0)
        a = min(1.0, sum(assoc.get(x, {}).get(k, 0.0) for x in qt))
        if b <= 0 and a <= 0 and t < 0.35:
            continue                    # nothing likes this file
        out.append((b + 0.35 * t + 0.60 * a, f))
    out.sort(key=lambda x: (-x[0], -(x[1].get("date") or 0)))
    return out


# -------------------------------------------------------------- feedback -----
def record(chat_id, query, file_row, positive, source="button"):
    """Log a real outcome and let the model pick it up immediately."""
    if not file_row:
        return
    db.add_feedback(chat_id, query, file_row.get("name"), file_row.get("message_id"),
                    positive=positive, source=source)
    invalidate(chat_id)


def remember_search(chat_id, query, files, user, reply_to, hashtag):
    """Stash an in-flight search so the feedback buttons can refer to it."""
    token = secrets.token_urlsafe(6)[:8]
    _pending[token] = {"t": time.time(), "chat_id": chat_id, "query": query,
                       "files": [dict(f) for f in files], "user": user or {},
                       "reply_to": reply_to, "hashtag": hashtag}
    if len(_pending) > _PENDING_MAX:              # drop the oldest
        for k in sorted(_pending, key=lambda k: _pending[k]["t"])[:50]:
            _pending.pop(k, None)
    return token


def get_pending(token):
    return _pending.get(token)


def drop_pending(token):
    _pending.pop(token, None)


# ---------------------------------------------------------------- reporting --
def stats(chat_id=None):
    fb = db.list_feedback(chat_id=chat_id)
    pos = sum(1 for f in fb if f.get("positive"))
    files = db.list_files(chat_id=chat_id) if chat_id else db.list_files()
    vocab = len(_model(chat_id)["idf"]) if chat_id else 0
    assoc = _associations(chat_id) if chat_id else {}
    return {"examples": len(fb), "positive": pos, "negative": len(fb) - pos,
            "accuracy": (pos / len(fb)) if fb else None,
            "files": len(files), "vocab": vocab,
            "associations": sum(len(v) for v in assoc.values())}


def associations(chat_id):
    """Public view of the learned token -> file weights."""
    return _associations(chat_id)


def top_aliases(chat_id, n=15):
    """The strongest learned word -> file links, for the dashboard."""
    names = {_key(f): f.get("name") for f in db.list_files(chat_id=chat_id)}
    rows = []
    for token, d in _associations(chat_id).items():
        for key, w in d.items():
            rows.append((w, token, names.get(key, key)))
    rows.sort(reverse=True)
    return rows[:n]


def train(chat_id=None):
    """Rebuild the model from the real corpus + feedback. (Explicit 'train'.)"""
    invalidate(chat_id)
    if chat_id:
        _build(chat_id)
        _associations(chat_id)
    else:
        for g in db.list_groups():
            cid = g.get("chat_id")
            _build(cid)
            _associations(cid)
    return stats(chat_id)
