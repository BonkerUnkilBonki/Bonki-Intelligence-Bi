"""Learned re-ranker - a logistic regression trained on real feedback.

This is genuine machine learning with a real objective, not a hand-tuned blend:
every time a user taps the file they actually wanted, we record a feature vector
with label 1 (and label 0 for the ones they rejected). After enough examples we
fit a logistic regression by gradient descent (numpy) and use its probability as
an extra ranking signal.

Why logistic regression and not a neural net: the honest reason is sample size.
With a few dozen to a few thousand feedback rows and 6 features, a small linear
model generalises and is interpretable; a neural net would memorise the rows and
look worse. The *semantic* deep-learning layer lives in `embed.py`, which uses a
pretrained transformer for meaning.

Until the model has enough rows, ranking falls back to the hand-tuned blend, so
nothing regresses on a fresh install.
"""
import json
import logging
import math
import time

from . import db

log = logging.getLogger("ranker")

try:
    import numpy as np
except Exception:                                    # pragma: no cover
    np = None

FEATURES = ("base", "recency", "name_len", "caption_hit", "query_len", "shared_before")
MIN_ROWS = 12
_EXT_WEIGHT = 0.45


def features(query, f, base_score=0.0, share_count=0):
    """Feature vector for one candidate. Keep this stable - it is the model input."""
    age_days = 999.0
    if f.get("date"):
        age_days = max(0.0, (time.time() - float(f["date"])) / 86400.0)
    recency = 1.0 / (1.0 + age_days / 30.0)
    name = (f.get("name") or "").lower()
    cap = (f.get("caption") or "").lower()
    q = (query or "").lower().strip()
    caption_hit = 1.0 if q and q in cap else 0.0
    return [
        float(base_score),
        recency,
        min(1.0, len(name) / 60.0),
        caption_hit,
        min(1.0, len(q.split()) / 5.0),
        min(1.0, float(share_count) / 5.0),
    ]


# A sensible starting model so the ranker is ON from the first search. These are
# hand-set priors, not learned - real training replaces them after MIN_ROWS taps.
PRIOR = {"w": [2.2, 0.25, 0.10, 1.10, 0.05, 0.10], "b": 0.0,
         "mu": [0.55, 0.50, 0.45, 0.30, 0.40, 0.10],
         "sd": [0.30, 0.28, 0.22, 0.45, 0.20, 0.18],
         "n": 0, "prior": True}


def _model():
    raw = db.get_settings().get("ranker_model") or ""
    if not raw:
        return dict(PRIOR)
    try:
        m = json.loads(raw)
        m.setdefault("prior", False)
        return m
    except Exception:
        return dict(PRIOR)


def is_prior():
    return bool((_model() or {}).get("prior"))


def trained():
    """True once the model can score - the prior counts, so this is on by default."""
    return np is not None and _model() is not None


def score(feat):
    """Probability that this candidate is the right file, or None if untrained."""
    m = _model()
    if np is None or not m:
        return None
    try:
        x = np.asarray(feat, dtype=float)
        mu = np.asarray(m["mu"], dtype=float)
        sd = np.asarray(m["sd"], dtype=float).copy()
        w = np.asarray(m["w"], dtype=float)
        sd[sd == 0] = 1.0            # a constant feature must never divide by 0
        z = float(((x - mu) / sd) @ w + m["b"])
        if not math.isfinite(z):
            return None
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
    except Exception:
        return None


def train(min_rows=MIN_ROWS):
    """Fit the model from recorded feedback. Returns a summary or None."""
    rows = db.list_ranker_rows()
    if np is None or len(rows) < min_rows:
        return None
    # need both classes, otherwise the fit is meaningless
    ys = [r["y"] for r in rows]
    if len(set(ys)) < 2:
        return None
    X = np.asarray([r["f"] for r in rows], dtype=float)
    y = np.asarray(ys, dtype=float)
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd == 0] = 1.0
    Xs = (X - mu) / sd
    w = np.zeros(Xs.shape[1])
    b = 0.0
    lr = 0.2
    for _ in range(500):                     # batch gradient descent
        p = 1.0 / (1.0 + np.exp(-(Xs @ w + b)))
        w -= lr * (Xs.T @ (p - y)) / len(y)
        b -= lr * float((p - y).mean())
    model = {"w": [round(float(v), 6) for v in w], "b": round(float(b), 6),
             "mu": [round(float(v), 6) for v in mu],
             "sd": [round(float(v), 6) for v in sd],
             "n": len(rows), "t": int(time.time())}
    db.set_settings({"ranker_model": json.dumps(model)})
    log.info("ranker trained on %d rows: w=%s b=%.3f", len(rows), model["w"], model["b"])
    return model


def rerank(ranked):
    """Blend the learned probability into an already-ranked candidate list."""
    if not trained() or not ranked:
        return ranked
    # the hand-set prior nudges gently; a trained model is trusted more
    weight = 0.20 if is_prior() else _EXT_WEIGHT
    out = []
    for base, f in ranked:
        p = score(f.get("_feat") or features("", f, base))
        if p is None:
            out.append((base, f))
        else:
            out.append((base * (1 - weight) + p * weight, f))
    out.sort(key=lambda x: (-x[0], -(x[1].get("date") or 0)))
    return out


def observe(f, label, trained_now=True):
    """Record one feedback example and refit when there is enough data."""
    feat = f.get("_feat")
    if not feat:
        return
    db.add_ranker_row(feat, label)
    if trained_now:
        rows = len(db.list_ranker_rows())
        if rows >= MIN_ROWS and rows % 10 == 0:      # refit every 10 new rows
            train()
