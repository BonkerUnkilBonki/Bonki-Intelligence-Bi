"""File category classifier - another small numpy network, trained locally.

Classifies a file name into a coarse category (app, game, mod, ebook, movie,
music, archive, document, image, other) so the bot can answer "list games" or
"how many ebooks" and group results sensibly.

Same honest scale as the intent net: a ~25k-parameter MLP trained from scratch on
a labelled set of example file names. It learns name patterns, not file contents -
it never opens anything you share.
"""
import hashlib
import logging
import math
import re

try:
    import numpy as np
except Exception:                                    # pragma: no cover
    np = None

log = logging.getLogger("categorize")

DIM = 512
HIDDEN = 40
SEED = 7

CATEGORIES = ["app", "game", "mod", "ebook", "movie", "music", "archive",
              "document", "image", "other"]

# Words that strongly indicate a category - used as training material, not rules.
EXAMPLES = {
    "app": ["whatsapp messenger.apk", "instagram.apk", "spotify premium.apk",
            "telegram x.apk", "netflix.apk", "chrome browser.apk", "paytm.apk",
            "gmail.apk", "youtube.apk", "facebook lite.apk", "truecaller.apk",
            "swiggy apk", "phonepe apk", "shareit apk"],
    "game": ["gta san andreas.apk", "pubg mobile.obb", "minecraft pe.apk",
             "clash of clans.apk", "subway surfers apk", "candy crush.apk",
             "free fire apk", "among us.apk", "asphalt 9 apk", "game data.obb",
             "call of duty mobile apk"],
    "mod": ["whatsapp mod apk", "instagram plus mod.apk", "spotify mod unlocked.apk",
            "youtube vanced mod.apk", "hacked version apk", "premium unlocked apk",
            "mod menu apk", "unlimited coins mod apk", "pro unlocked.apk",
            "adfree mod.apk"],
    "ebook": ["python crash course.pdf", "atomic habits.epub", "novel.epub",
              "book collection.pdf", "study material.pdf", "notes chapter.pdf",
              "programming book.pdf", "kindle ebook.epub"],
    "movie": ["movie 2024.mkv", "film hd.mp4", "web series s01e01.mkv",
              "documentary.mp4", "bollywood movie.mkv", "season 2 complete.mp4",
              "trailer.mp4"],
    "music": ["song.mp3", "album flac", "playlist.m4a", "lofi beats.mp3",
              "soundtrack ost.mp3", "remix.mp3", "podcast episode.mp3"],
    "archive": ["files.zip", "backup.rar", "pack.7z", "collection.zip",
                "assets bundle.rar", "photos zip", "mods pack.zip"],
    "document": ["invoice.pdf", "notes.docx", "resume.pdf", "report.xlsx",
                 "contract.pdf", "presentation.pptx", "data.csv", "letter.docx"],
    "image": ["wallpaper.jpg", "photo.png", "screenshot.webp", "logo.svg",
              "banner.jpeg", "sticker pack.png"],
    "other": ["unknown file", "random thing", "blob", "no extension",
              "misc item", "untitled"],
}

_EXT_HINT = {
    "apk": "app", "xapk": "app", "apks": "app", "ipa": "app",
    "obb": "game", "zip": "archive", "rar": "archive", "7z": "archive",
    "tar": "archive", "gz": "archive",
    "pdf": "document", "doc": "document", "docx": "document",
    "xls": "document", "xlsx": "document", "ppt": "document",
    "pptx": "document", "txt": "document", "csv": "document",
    "epub": "ebook", "mobi": "ebook", "azw3": "ebook",
    "mp4": "movie", "mkv": "movie", "avi": "movie", "mov": "movie", "webm": "movie",
    "mp3": "music", "flac": "music", "m4a": "music", "wav": "music", "ogg": "music",
    "jpg": "image", "jpeg": "image", "png": "image", "webp": "image",
    "gif": "image", "svg": "image",
}


def _features(text):
    out = []
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) < 2:
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


def _train(epochs=350, lr=0.35, batch=16):
    rng = np.random.default_rng(SEED)
    X, y = [], []
    for i, cat in enumerate(CATEGORIES):
        for name in EXAMPLES[cat]:
            X.append(_vector(name))
            y.append(i)
    X, y = np.array(X), np.array(y)
    n, d = X.shape
    W1 = rng.normal(0, math.sqrt(2.0 / d), (d, HIDDEN))
    b1 = np.zeros(HIDDEN)
    W2 = rng.normal(0, math.sqrt(2.0 / HIDDEN), (HIDDEN, len(CATEGORIES)))
    b2 = np.zeros(len(CATEGORIES))
    Y = np.zeros((n, len(CATEGORIES)))
    Y[np.arange(n), y] = 1.0

    for _ in range(epochs):
        idx = rng.permutation(n)
        for s in range(0, n, batch):
            sel = idx[s:s + batch]
            xb, yb = X[sel], Y[sel]
            h = np.maximum(0.0, xb @ W1 + b1)
            z = h @ W2 + b2
            z -= z.max(axis=1, keepdims=True)
            p = np.exp(z)
            p /= p.sum(axis=1, keepdims=True)
            dz = (p - yb) / len(sel)
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
    global _MODEL
    if _MODEL is None and np is not None:
        try:
            _MODEL = _train()
            log.info("category net trained: %d examples, acc %.2f",
                     _MODEL["n"], _MODEL["acc"])
        except Exception as e:
            log.warning("category net failed: %s", e)
            _MODEL = {"error": str(e)}
    return _MODEL


def available():
    m = model()
    return np is not None and m is not None and "W1" in m


def classify(name):
    """(category, confidence). Falls back to the extension hint when unsure."""
    ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    m = model()
    if not m or "W1" not in m:
        return _EXT_HINT.get(ext, "other"), 0.0
    v = _vector(name)
    h = np.maximum(0.0, v @ m["W1"] + m["b1"])
    z = h @ m["W2"] + m["b2"]
    z -= z.max()
    p = np.exp(z)
    p /= p.sum()
    i = int(p.argmax())
    conf = float(p[i])
    hint = _EXT_HINT.get(ext)
    # a confident extension is a strong prior - trust it over a weak net guess
    if hint and (conf < 0.55 or CATEGORIES[i] == "other"):
        return hint, max(conf, 0.6)
    if hint and hint != CATEGORIES[i] and conf < 0.75:
        return hint, 0.6
    return CATEGORIES[i], conf


def describe():
    m = model()
    if not m or "W1" not in m:
        return "off"
    return (f"numpy MLP {DIM}→{HIDDEN}→{len(CATEGORIES)} · {m['n']} file-name "
            f"examples · train acc {m['acc'] * 100:.0f}%")
