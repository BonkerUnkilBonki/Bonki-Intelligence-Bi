"""Local vector embeddings - no API, no key, no model download.

This builds vectors from *your own files* at runtime:

  1. each text becomes a hashed bag of **word tokens and character 3-grams**
     (character n-grams are what let it match "whatsap" to "whatsapp" and cope
     with typos and glued words like "whatsappmod")
  2. IDF weights are learned from the corpus, so common words count for less
  3. when the corpus is big enough, **truncated SVD (LSA)** projects the sparse
     vectors down to dense ones, which is what captures co-occurrence meaning
     ("messenger" ends up near "whatsapp" because they appear together)

Be clear about what this is and is not. It is genuine unsupervised machine
learning, trained on your data, running locally in numpy. It is **not** a
transformer and not a neural network: there are no pretrained weights here,
because none can be shipped without a download or an API. It gives real semantic
similarity (typos, word order, related terms) at zero cost and zero dependency.
"""
import hashlib
import logging
import math
import re

try:
    import numpy as np
except Exception:                                    # pragma: no cover
    np = None

log = logging.getLogger("localvec")

HASH_DIM = 512          # hashed feature space
SVD_DIM = 64            # dense size after LSA
MIN_DOCS_FOR_SVD = 25   # below this, LSA is unstable - stay sparse

_STOP = set("""a an the and or of for to in on at by with from is are was were be been
this that these those it its as if then than so such not no yes you your we our they
their he she his her them us me my i can could should would will shall may might do
does did done have has had get got make made new file files apk app version latest
download link group chat telegram""".split())

_cache = {"key": None, "model": None}


def _words(text):
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w]


def _features(text):
    """Word tokens + character 3-grams (typo tolerant)."""
    out = []
    for w in _words(text):
        if w in _STOP or len(w) < 2:
            continue
        out.append("w:" + w)
        if len(w) >= 4:
            padded = f"_{w}_"
            for i in range(len(padded) - 2):
                out.append("c:" + padded[i:i + 3])
    return out


def _hash_index(feat):
    h = hashlib.blake2b(feat.encode("utf-8", "ignore"), digest_size=8).digest()
    return int.from_bytes(h, "big") % HASH_DIM


def _sparse(text, idf=None):
    vec = [0.0] * HASH_DIM
    for feat in _features(text):
        vec[_hash_index(feat)] += 1.0
    if idf is not None:
        for i in range(HASH_DIM):
            if vec[i]:
                vec[i] *= idf[i]
    return vec


def _corpus_key(texts):
    h = hashlib.sha1()
    for t in texts:
        h.update((t or "").encode("utf-8", "ignore"))
    return h.hexdigest()


def fit(texts):
    """Learn IDF (and LSA when the corpus allows) from these texts."""
    if np is None:
        return None
    texts = [t for t in texts if t]
    if not texts:
        return None

    counts = np.zeros(HASH_DIM)
    for t in texts:
        for feat in _features(t):
            counts[_hash_index(feat)] += 1.0
    n = len(texts)
    idf = np.log((1 + n) / (1 + counts)) + 1.0          # smoothed IDF

    model = {"idf": idf, "n": n, "svd": None, "mean": None}
    if n >= MIN_DOCS_FOR_SVD:
        try:
            X = np.array([_sparse(t, idf) for t in texts])
            mean = X.mean(axis=0)
            Xc = X - mean
            # economy SVD, keep SVD_DIM components
            U, S, Vt = np.linalg.svd(Xc, full_matrices=False)
            k = min(SVD_DIM, Vt.shape[0])
            model["svd"] = Vt[:k]
            model["mean"] = mean
        except Exception as e:
            log.info("LSA skipped: %s", e)
    return model


def _model_for(texts):
    key = _corpus_key(texts)
    if _cache["key"] == key and _cache["model"] is not None:
        return _cache["model"]
    model = fit(texts)
    _cache["key"], _cache["model"] = key, model
    return model


def _vec(model, text):
    if model is None or np is None:
        return None
    v = np.array(_sparse(text, model["idf"]))
    if model.get("svd") is not None:
        v = (v - model["mean"]) @ model["svd"].T
    return v


def embed_many(texts, corpus=None):
    """Return {text: vector}. `corpus` is what IDF/LSA is fitted on."""
    if np is None or not texts:
        return {}
    corpus = corpus or texts
    model = _model_for(corpus)
    if model is None:
        return {}
    out = {}
    for t in texts:
        v = _vec(model, t)
        if v is not None:
            out[t] = [float(x) for x in v]
    return out


def available():
    return np is not None


def describe():
    return ("local hashed n-gram vectors + IDF"
            + (" + LSA" if _cache.get("model") and _cache["model"].get("svd") is not None
               else " (LSA once the corpus passes %d files)" % MIN_DOCS_FOR_SVD))
