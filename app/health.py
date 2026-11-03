"""Boot and liveness facts - so "it died" becomes a number, not a guess.

Ported from the BonkiFinds bot, which tracked how often the process restarted and
how much memory it used. On a free/256 MB host both matter, and after a server
renewal you want to see at a glance whether it actually came back up.
"""
import logging
import os
import time

from . import db

log = logging.getLogger("health")


def memory_mb():
    """Resident memory of this process in MB (Linux), or None."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        pass
    return None


def note_boot():
    """Count this start and work out how long we were down."""
    try:
        prev = int(db.get_settings().get("boot_count") or 0)
    except (TypeError, ValueError):
        prev = 0
    try:
        last = int(db.get_settings().get("last_boot_at") or 0)
    except (TypeError, ValueError):
        last = 0
    now = int(time.time())
    gap = (now - last) if last else 0
    db.set_settings({"boot_count": str(prev + 1),
                     "last_boot_at": str(now),
                     "last_downtime_sec": str(gap)})
    db.flush(force=True)
    return prev + 1, gap


def summary():
    s = db.get_settings()
    try:
        boots = int(s.get("boot_count") or 0)
    except (TypeError, ValueError):
        boots = 0
    started = s.get("last_boot_at") or ""
    try:
        started_str = (time.strftime("%d %b %Y %H:%M", time.localtime(int(started)))
                       if started else "unknown")
        up_min = int((time.time() - float(started)) / 60) if started else 0
    except (TypeError, ValueError):
        started_str, up_min = "unknown", 0
    try:
        gap = int(s.get("last_downtime_sec") or 0)
    except (TypeError, ValueError):
        gap = 0
    return {"boots": boots, "started": started_str, "uptime_min": up_min,
            "mem_mb": memory_mb(), "downtime_sec": gap}


def human_gap(sec):
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "unknown"
    if sec <= 0:
        return "first start"
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60}m"
    if sec < 172800:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"
