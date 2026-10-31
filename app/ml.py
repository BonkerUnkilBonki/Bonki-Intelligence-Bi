"""Transparency for the ML layers - so you can actually SEE them working.

Nothing here changes ranking. It exists because "trust me, there's machine
learning" is not evidence: this prints the real component scores for a query and
the real state of each layer.
"""
import logging
import math

from . import db, embed, learn, llm, ranker

log = logging.getLogger("ml")


def _cos(a, b):
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(x * x for x in a[:n]))
    nb = math.sqrt(sum(x * x for x in b[:n]))
    return dot / (na * nb) if na and nb else 0.0


def _key(f):
    return f"{f.get('chat_id')}:{f.get('message_id')}"


def status(chat_id=None):
    """State of every layer, in plain numbers."""
    st = learn.stats(chat_id) if chat_id else learn.stats()
    rows = db.list_ranker_rows()
    model = ranker._model()
    return {
        "embed_on": embed.enabled(),
        "embed_model": (db.get_settings().get("gemini_embed_model")
                        or db.get_settings().get("openai_embed_model")
                        or "text-embedding-004"),
        "embed_cached": len(getattr(embed, "_cache", {}) or {}),
        "ranker_trained": bool(model),
        "ranker_rows": len(rows),
        "ranker_needs": ranker.MIN_ROWS,
        "ranker_weights": (model or {}).get("w"),
        "learn": st if isinstance(st, dict) else {},
        "memory": db.count_memory(),
        "brain": llm.describe(),
    }


def explain(query, chat_id, limit=6):
    """Per-candidate component scores for one query. This is the ML, visible."""
    from . import agent

    files = db.list_files(chat_id=chat_id)
    if not files:
        return {"files": 0, "rows": [], "embed_on": embed.enabled()}

    base_ranked = agent.rank(query, files)
    base = {_key(f): s for s, f in base_ranked}
    tf = learn.tfidf_scores(query, files, chat_id)
    assoc_map = learn._associations(chat_id)
    qt = learn._key_tokens(query)

    # candidates: anything the fuzzy matcher liked, or that has TF-IDF/assoc signal
    cands = []
    for f in files:
        k = _key(f)
        b, t = base.get(k, 0.0), tf.get(k, 0.0)
        a = min(1.0, sum(assoc_map.get(x, {}).get(k, 0.0) for x in qt))
        if b > 0 or t > 0 or a > 0:
            cands.append((b + 0.35 * t + 0.60 * a, f, b, t, a))
    cands.sort(key=lambda x: -x[0])
    cands = cands[:limit]

    # embedding similarity for the shortlist (one batched call)
    emb = {}
    if embed.enabled() and cands:
        try:
            docs = [f"{f.get('name','')} {f.get('caption','')}".strip() for _, f, *_ in cands]
            vecs = embed.embed_many([query] + docs)
            qv = vecs.get(query)
            if qv:
                for (_, f, *_), d in zip(cands, docs):
                    emb[_key(f)] = _cos(qv, vecs.get(d) or [])
        except Exception as e:
            log.info("embed explain skipped: %s", e)

    rows = []
    for final, f, b, t, a in cands:
        feat = ranker.features(query, f, final)
        p = ranker.score(feat) if ranker.trained() else None
        rows.append({
            "name": f.get("name") or "?",
            "base": round(b, 3),
            "tfidf": round(t, 3),
            "learned": round(a, 3),
            "embed": round(emb.get(_key(f), 0.0), 3),
            "ranker": (round(p, 3) if p is not None else None),
            "final": round(final, 3),
        })
    return {"files": len(files), "rows": rows, "embed_on": embed.enabled()}
