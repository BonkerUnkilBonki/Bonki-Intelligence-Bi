"""A small neural network, written from scratch in numpy - no API, no download.

It exists to replace the one job that used to need a language model: working out
**what the user is asking for**. A tiny MLP (hashed n-gram input -> hidden ReLU ->
softmax) is trained at startup on a built-in set of labelled example phrasings,
then classifies incoming messages into intents.

Honest about the scale: this is a ~40k-parameter network trained on a few hundred
templates. That is enough to beat keyword matching on phrasing ("can you pull the
spotify one" -> find), and it is emphatically **not** a language model. It cannot
write prose, summarise, or answer questions. Anything generative still uses the
configured LLM, or the built-in templates.

Everything here is numpy: forward pass, backprop, SGD, cross-entropy. No
PyTorch, no TensorFlow, no model files, no key.
"""
import hashlib
import logging
import math
import re

try:
    import numpy as np
except Exception:                                    # pragma: no cover
    np = None

log = logging.getLogger("nn")

DIM = 512            # hashed n-gram features
HIDDEN = 48
SEED = 1234

INTENTS = ["find", "scan", "list", "count", "stats", "summarise", "help", "tag",
           "unknown"]

TEMPLATES = {
    "find": [
        "find whatsapp apk", "get the spotify app", "send me instagram", "give me that zip",
        "can you pull the spotify one", "i need whatsapp messenger", "looking for netflix apk",
        "where is the telegram file", "share the youtube apk", "download chrome apk for me",
        "anyone have the notes file", "please send latest whatsapp mod", "i want that movie",
        "find apple music", "get me the pdf", "do you have spotify premium",
        "send the apk please", "can i get instagram", "post the netflix file", "need the zip file",
    ],
    "scan": [
        "scan this group", "rescan the group", "index the chat", "scan history",
        "please scan this group now", "index all files here", "can you scan the group",
        "rescan everything", "scan the chat again", "index this group",
    ],
    "list": [
        "list files", "show all apks", "what files are here", "list apks",
        "show me everything", "all files please", "which files do you have",
        "list all zips", "show the file list", "what apks are there",
    ],
    "count": [
        "how many files", "count the apks", "how many zips", "number of files",
        "total files here", "how many apks do you have", "how many files are indexed",
    ],
    "stats": [
        "group stats", "show statistics", "stats please", "breakdown of files",
        "top senders", "who shares the most", "file stats", "give me the stats",
    ],
    "summarise": [
        "summarise this group", "recap what was shared", "tl dr of the chat",
        "what has been shared recently", "summarize the group", "give me a recap",
        "what did i miss", "summary of files",
    ],
    "help": [
        "help", "what can you do", "how do you work", "commands", "show commands",
        "what are your features", "how do i use you", "what can i ask you",
    ],
    "tag": [
        "tag him 5 times", "tag the user 10 times", "ping this guy", "tag everyone",
        "tag them 3 times", "mention him twice",
    ],
    "unknown": [
        "hello", "hi there", "lol", "nice", "ok", "thanks", "who are you",
        "good morning", "what is the weather", "tell me a joke", "hmm",
    ],
}

_STOP = set("""a an the and or of for to in on at by with from is are was were be been
this that these those it its as if then than so such not no yes you your we our they
their he she his her them us me my i can could should would will shall may might do
does did done have has had get got make made new file files apk app version latest
download link group chat telegram""".split())


def _features(text):
    out = []
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in _STOP or len(w) < 2:
            continue
        out.append("w:" + w)
        if len(w) >= 4:
            p = f"_{w}_"
            for i in range(len(p) - 2):
                out.append("c:" + p[i:i + 3])
    return out


def _index(feat):
    return int.from_bytes(hashlib.blake2b(feat.encode("utf-8", "ignore"),
                                          digest_size=8).digest(), "big") % DIM


def _vector(text):
    v = np.zeros(DIM)
    for f in _features(text):
        v[_index(f)] += 1.0
    n = np.linalg.norm(v)
    return v / n if n else v


def _dataset():
    X, y = [], []
    for i, intent in enumerate(INTENTS):
        for phrase in TEMPLATES[intent]:
            X.append(_vector(phrase))
            y.append(i)
    return np.array(X), np.array(y)


def _train(epochs=400, lr=0.35, batch=16):
    rng = np.random.default_rng(SEED)
    X, y = _dataset()
    n, d = X.shape
    W1 = rng.normal(0, math.sqrt(2.0 / d), (d, HIDDEN))
    b1 = np.zeros(HIDDEN)
    W2 = rng.normal(0, math.sqrt(2.0 / HIDDEN), (HIDDEN, len(INTENTS)))
    b2 = np.zeros(len(INTENTS))
    Y = np.zeros((n, len(INTENTS)))
    Y[np.arange(n), y] = 1.0

    for _ in range(epochs):
        idx = rng.permutation(n)
        for start in range(0, n, batch):
            sel = idx[start:start + batch]
            xb, yb = X[sel], Y[sel]
            h = np.maximum(0.0, xb @ W1 + b1)                 # forward
            z = h @ W2 + b2
            z -= z.max(axis=1, keepdims=True)
            p = np.exp(z)
            p /= p.sum(axis=1, keepdims=True)
            dz = (p - yb) / len(sel)                          # backprop
            dW2 = h.T @ dz
            db2 = dz.sum(axis=0)
            dh = (dz @ W2.T) * (h > 0)
            dW1 = xb.T @ dh
            db1 = dh.sum(axis=0)
            W1 -= lr * dW1
            b1 -= lr * db1
            W2 -= lr * dW2
            b2 -= lr * db2

    h = np.maximum(0.0, X @ W1 + b1)
    z = h @ W2 + b2
    z -= z.max(axis=1, keepdims=True)
    p = np.exp(z)
    p /= p.sum(axis=1, keepdims=True)
    acc = float((p.argmax(axis=1) == y).mean())
    return {"W1": W1, "b1": b1, "W2": W2, "b2": b2, "acc": acc, "n": n}


_MODEL = None


def model():
    """Train once, keep it in memory (about 40k params, <1s)."""
    global _MODEL
    if _MODEL is None and np is not None:
        try:
            _MODEL = _train()
            log.info("intent net trained: %d examples, train accuracy %.2f",
                     _MODEL["n"], _MODEL["acc"])
        except Exception as e:
            log.warning("intent net training failed: %s", e)
            _MODEL = {"error": str(e)}
    return _MODEL


def available():
    return np is not None and model() is not None and "W1" in model()


def classify(text):
    """(intent, confidence, {intent: prob}) or (None, 0.0, {})."""
    m = model()
    if not m or "W1" not in m:
        return None, 0.0, {}
    v = _vector(text)
    h = np.maximum(0.0, v @ m["W1"] + m["b1"])
    z = h @ m["W2"] + m["b2"]
    z -= z.max()
    p = np.exp(z)
    p /= p.sum()
    i = int(p.argmax())
    return INTENTS[i], float(p[i]), {INTENTS[j]: round(float(p[j]), 3)
                                     for j in range(len(INTENTS))}


def describe():
    m = model()
    if not m or "W1" not in m:
        return "off"
    return (f"numpy MLP {DIM}→{HIDDEN}→{len(INTENTS)} · "
            f"{m['n']} examples · train acc {m['acc'] * 100:.0f}%")
