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


# ---------------------------------------------------------- disk safety ------
def work_dir():
    """Where to put large temp downloads.

    /tmp is a tmpfs (RAM) on many containers. Downloading a 100 MB asset there
    would eat the RAM we are trying to protect, so prefer a real disk directory
    under the app's data folder when one is writable.
    """
    import os
    import shutil
    for cand in (os.environ.get("BONKI_TMP"),
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "tmp"),
                 "/var/tmp"):
        if not cand:
            continue
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".w")
            with open(probe, "wb") as f:
                f.write(b"x")
            os.unlink(probe)
            if not _is_ram_fs(cand):
                return cand
        except Exception:
            continue
    return None                      # caller falls back to tempfile


def _is_ram_fs(path):
    """True when the filesystem holding `path` is memory-backed (tmpfs)."""
    try:
        import subprocess
        out = subprocess.run(["df", "-T", path], capture_output=True, text=True,
                             timeout=5).stdout
        return "tmpfs" in out.split("\n")[-1].lower() if out else False
    except Exception:
        return False


def free_mb(path="/"):
    try:
        import shutil
        return round(shutil.disk_usage(path).free / (1024 * 1024))
    except Exception:
        return 0


def can_download(size_bytes, path=None):
    """Is there room for this file (plus a little slack)?"""
    need = int(size_bytes or 0) / (1024 * 1024) * 1.15
    where = path or work_dir() or "/tmp"
    have = free_mb(where)
    return (have == 0 or have > need), have, round(need)
