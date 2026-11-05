"""Low-memory mode: make the bot survive a 256 MB container.

The process was being OOM-killed every ~72 seconds on a 256 MB host, which looked
like "the bot restarts and re-posts everything". This module detects the limit and
switches off the expensive parts.

What is switched off in low-memory mode:
  * numpy-backed ML (intent net, categoriser, ranker, LSA vectors) - fuzzy search
    and TF-IDF still work, so searching is unaffected in practice
  * analytics snapshots, scheduled skills, learn-loop retraining, federation sync
  * concurrent uploads drop from 3 to 1
  * Pillow-based quote cards fall back to a text quote

Set LOW_MEMORY=1 to force it, or LOW_MEMORY=0 to disable detection.
"""
import logging
import os

log = logging.getLogger("lowmem")

_LIMIT_MB = None
_MODE = None


def limit_mb():
    """The container's memory limit in MB, or None when unlimited."""
    global _LIMIT_MB
    if _LIMIT_MB is not None:
        return _LIMIT_MB
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as f:
                raw = f.read().strip()
            if raw in ("max", "-1", ""):
                continue
            val = int(raw)
            if val > 0 and val < (1 << 40):        # ignore "unlimited" sentinels
                _LIMIT_MB = round(val / (1024 * 1024))
                return _LIMIT_MB
        except Exception:
            continue
    _LIMIT_MB = 0
    return _LIMIT_MB


def is_low():
    """True when we should run in low-memory mode."""
    global _MODE
    if _MODE is not None:
        return _MODE
    env = str(os.environ.get("LOW_MEMORY") or "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        _MODE = True
    elif env in ("0", "false", "no", "off"):
        _MODE = False
    else:
        lim = limit_mb()
        _MODE = bool(lim and lim < 400)
    if _MODE:
        log.warning("LOW-MEMORY MODE: container limit %s MB — ML layers, extra "
                    "loops and parallel uploads are off", limit_mb() or "?")
    return _MODE


def describe():
    return ("low-memory" if is_low() else "normal") + \
           (f" ({limit_mb()} MB limit)" if limit_mb() else "")
