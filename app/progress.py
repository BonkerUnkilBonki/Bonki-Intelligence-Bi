"""Live progress for leech downloads and uploads, reported in the owner's DM.

One message per file, edited in place as it moves: download percentage, then upload
percentage, then done or failed. Edits are throttled so Telegram is not hammered
(a big file would otherwise mean hundreds of edits).
"""
import logging
import time

from . import db, telegram

log = logging.getLogger("progress")

EDIT_EVERY = 3.0          # seconds between edits


def _owner():
    try:
        from . import approval
        return approval.owner_id()
    except Exception:
        return None


def enabled():
    """On unless switched off with /leech progress off."""
    return str(db.get_settings().get("leech_progress") or "1") == "1"


def where():
    """dm (default) | channel | both | off."""
    val = str(db.get_settings().get("leech_progress_where") or "dm").lower()
    if not enabled():
        return "off"
    return val if val in ("dm", "channel", "both", "off") else "dm"


def targets(route=None):
    """Where a progress message should go for this file."""
    mode = where()
    out = []
    if mode in ("dm", "both"):
        o = _owner()
        if o:
            out.append({"chat_id": o, "thread_id": None, "kind": "dm"})
    if mode in ("channel", "both") and route:
        out.append({"chat_id": route.get("chat_id"),
                    "thread_id": route.get("thread_id") or None,
                    "kind": "channel"})
    return out


def human(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def bar(frac, width=12):
    frac = max(0.0, min(1.0, frac or 0.0))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


class Tracker:
    """One file's journey: download -> upload -> done."""

    def __init__(self, name, size=0, chat_title="", route=None):
        self.name = name
        self.size = int(size or 0)
        self.chat_title = chat_title
        self.dm = _owner()
        self.t0 = time.time()
        self._last_edit = 0.0
        self._last_pct = -1
        self._phase = "start"
        # one message per destination (DM and/or the channel topic)
        self.slots = []
        for t in targets(route):
            slot = dict(t)
            slot["msg_id"] = None
            slot["last_edit"] = 0.0
            self.slots.append(slot)
        for slot in self.slots:
            self._post(slot, self._render(0, "downloading"))

    # ---------------------------------------------------------------- text --
    def _render(self, done, phase):
        pct = (done / self.size) if self.size else 0
        head = {"downloading": "⬇️ <b>Downloading</b>",
                "uploading": "⬆️ <b>Uploading</b>",
                "done": "✅ <b>Sent</b>",
                "failed": "⚠️ <b>Failed</b>"}.get(phase, "⏳")
        line = f"{head} <code>{telegram.esc(self.name)}</code>"
        if self.size:
            line += (f"\n<code>{bar(pct)}</code> {int(pct * 100)}%  "
                     f"({human(done)} / {human(self.size)})")
        elif done:
            line += f"\n{human(done)}"
        el = int(time.time() - self.t0)
        if el >= 2:
            line += f"\n<i>{el}s elapsed</i>"
        return line

    def _post(self, slot, text):
        try:
            r = telegram.send_message(text, chat_id=slot["chat_id"],
                                      thread_id=slot.get("thread_id"))
            slot["msg_id"] = ((r or {}).get("result") or {}).get("message_id")
        except Exception as e:
            log.info("progress post failed (%s): %s", slot.get("kind"), e)
            slot["msg_id"] = None

    def _edit(self, text, force=False):
        now = time.time()
        for slot in self.slots:
            if not slot.get("msg_id"):
                continue
            if not force and (now - slot["last_edit"]) < EDIT_EVERY:
                continue
            slot["last_edit"] = now
            try:
                telegram.edit_message_text(slot["chat_id"], slot["msg_id"], text)
            except Exception as e:
                log.info("progress edit failed (%s): %s", slot.get("kind"), e)

    # -------------------------------------------------------------- events --
    def downloading(self, done, total=None):
        if total and not self.size:
            self.size = int(total)
        pct = int((done / self.size) * 100) if self.size else -1
        if pct == self._last_pct:
            return
        self._last_pct = pct
        self._edit(self._render(done, "downloading"))

    def stalled(self, done, attempt=1, err=""):
        txt = self._render(done, "downloading")
        txt += (f"\n<i>⚠️ connection stalled — resuming from "
                f"{human(done)} (attempt {attempt})</i>")
        self._edit(txt, force=True)

    def uploading(self, done=0):
        self._phase = "uploading"
        self._edit(self._render(done, "uploading"), force=True)

    def uploaded(self, done=None):
        self._edit(self._render(done or self.size, "uploading"))

    def done(self):
        self._edit(self._render(self.size, "done"), force=True)

    def failed(self, err=""):
        txt = self._render(0, "failed")
        if err:
            txt += f"\n<i>{telegram.esc(str(err)[:160])}</i>"
        self._edit(txt, force=True)


def callback_for(tracker):
    """A (done, total) callback suitable for streaming downloads."""
    if not tracker:
        return None
    return lambda done, total=None: tracker.downloading(done, total)


class ProgressFile:
    """Wraps a file for requests so upload progress can be measured."""

    def __init__(self, path, tracker=None):
        self._fh = open(path, "rb")
        self._tracker = tracker
        try:
            import os
            self._total = os.path.getsize(path)
        except Exception:
            self._total = 0
        self._done = 0

    def read(self, size=-1):
        chunk = self._fh.read(size)
        self._done += len(chunk or b"")
        if self._tracker:
            self._tracker.uploaded(self._done)
        return chunk

    def __len__(self):
        return self._total

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass

    def __getattr__(self, item):
        return getattr(self._fh, item)
