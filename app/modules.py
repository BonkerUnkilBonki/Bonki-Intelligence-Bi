"""Per-chat module switches - so Bonki doesn't duplicate what Rose already does.

Every automatic behaviour and command group belongs to a module. Turn a module off
in a chat and Bonki stops doing that thing there, while everything else keeps
working. That is how you avoid two bots banning the same person twice.

Configured from inside Telegram with /config — no dashboard needed.
"""
import logging

from . import db

log = logging.getLogger("modules")

# name -> (label, what it covers)
MODULES = {
    "moderation": ("Moderation", "ban · kick · mute · warn · purge · pin"),
    "welcome":    ("Welcome / goodbye", "greeting new members, farewells"),
    "captcha":    ("CAPTCHA", "mute new joiners until they verify"),
    "antiflood":  ("Antiflood", "mute fast repeat senders"),
    "blocklist":  ("Blocklist", "delete messages with blocked words"),
    "locks":      ("Locks", "delete locked message types"),
    "filters":    ("Filters", "automatic replies to trigger words"),
    "notes":      ("Notes / rules", "saved notes and /rules"),
    "search":     ("File search", "indexing, find, list, stats, ask"),
    "tag":        ("Tagging", "/tag, /tagall"),
    "fedbans":    ("Federation", "fed bans and cross-chat enforcement"),
    "ai":         ("Chat replies", "conversational answers when addressed"),
    "mirror":     ("Dump / mirror", "live forwarding between chats"),
    "skills":     ("Scheduled skills", "/skill tasks"),
}

DEFAULT = "1"          # modules start ON

# which module a command belongs to (anything unlisted is "core" = always allowed)
COMMAND_MODULE = {
    "ban": "moderation", "bonk": "moderation", "unban": "moderation",
    "kick": "moderation", "mute": "moderation", "unmute": "moderation",
    "warn": "moderation", "bonki": "moderation", "warns": "moderation",
    "resetwarns": "moderation", "del": "moderation", "purge": "moderation",
    "spurge": "moderation", "pin": "moderation", "unpin": "moderation",
    "report": "moderation", "welcome": "welcome", "setwelcome": "welcome",
    "goodbye": "welcome", "setgoodbye": "welcome",
    "captcha": "captcha", "setflood": "antiflood", "flood": "antiflood",
    "addblocklist": "blocklist", "rmblocklist": "blocklist",
    "blocklist": "blocklist", "blocklists": "blocklist",
    "lock": "locks", "unlock": "locks", "locks": "locks",
    "filter": "filters", "stop": "filters", "stopall": "filters",
    "filters": "filters",
    "save": "notes", "get": "notes", "clear": "notes", "notes": "notes",
    "setrules": "notes", "rules": "notes",
    "scan": "search", "find": "search", "list": "search", "stats": "search",
    "ask": "search", "catalog": "search", "channelstat": "search",
    "tag": "tag", "tagall": "tag", "utagall": "tag",
    "fban": "fedbans", "fbonk": "fedbans", "unfban": "fedbans",
    "fedbans": "fedbans", "joinfed": "fedbans", "leavefed": "fedbans",
    "fedsync": "fedbans", "fedbridge": "fedbans",
    "dump": "mirror", "skill": "skills", "skills": "skills",
}


def _conf(chat_id):
    return (db.get_chatconf(chat_id) or {}).get("modules") or {}


def enabled(chat_id, module):
    """Is this module on in this chat? Defaults to on."""
    if module not in MODULES:
        return True
    return str(_conf(chat_id).get(module, DEFAULT)) != "0"


def set_module(chat_id, module, on):
    conf = dict(_conf(chat_id))
    conf[module] = "1" if on else "0"
    db.set_chatconf(chat_id, {"modules": conf})


def toggle(chat_id, module):
    new = not enabled(chat_id, module)
    set_module(chat_id, module, new)
    return new


def command_allowed(chat_id, cmd):
    """Commands map to a module; unknown commands are always allowed."""
    mod = COMMAND_MODULE.get((cmd or "").lower())
    if not mod:
        return True
    return enabled(chat_id, mod)


def keyboard(chat_id):
    """Inline keyboard: one button per module, ✅ on / ⬜ off."""
    rows, row = [], []
    for name, (label, _desc) in MODULES.items():
        mark = "✅" if enabled(chat_id, name) else "⬜"
        row.append({"text": f"{mark} {label}", "callback_data": f"mod:{name}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "🔄 Turn all ON", "callback_data": "mod:__all_on"},
                 {"text": "⛔ Turn all OFF", "callback_data": "mod:__all_off"}])
    rows.append([{"text": "🩺 Check permissions", "callback_data": "mod:__perms"}])
    return {"inline_keyboard": rows}


def summary_text(chat_id):
    off = [MODULES[n][0] for n in MODULES if not enabled(chat_id, n)]
    lines = ["⚙️ <b>Bonki modules for this chat</b>", ""]
    if off:
        lines.append("Off here: " + ", ".join(off))
    else:
        lines.append("All modules are on.")
    lines.append("")
    lines.append("Tap a module to switch it off — useful when another bot (Rose) "
                 "already handles that job, so you don't get double bans or double "
                 "welcomes.")
    return "\n".join(lines)
