"""Short conversational replies, with no model behind them.

Bonki without an LLM key cannot generate language - that is the honest limit. But
"hello" deserves better than a wall of text about indexing, so this handles the
handful of everyday things people say in a group and answers briefly.

If an LLM is configured, agent.py prefers it; this is the offline floor.
"""
import re

_RULES = [
    (r"^(hi|hey|hello|yo|hola|namaste|salam|good morning|good evening|gm)\b",
     "Hey 👋 Ask me for a file any time — <i>Bonki find whatsapp apk</i>."),
    (r"\b(thanks|thank you|thx|ty|shukriya|dhanyavad)\b",
     "Any time 👍"),
    (r"^(bye|goodbye|gn|good night|see ya)\b",
     "See you 👋"),
    (r"\b(how are you|how's it going|whats up|what's up|sup)\b",
     "Running fine — indexing and ready. What do you need?"),
    (r"\b(who are you|what are you|your name|are you a bot|are you human)\b",
     "I'm Bonki — a bot that indexes the files shared in this group and finds "
     "them. Not a person, and I won't pretend to be one."),
    (r"\b(are you (dumb|stupid|useless|broken)|you suck|stupid bot)\b",
     "Fair enough — tell me what you actually wanted and I'll try again."),
    (r"\b(lol|lmao|haha|xd|😂)\b",
     "😄"),
    (r"\b(good|nice|great|awesome|cool|perfect)\b",
     "👍"),
]

# Directed abuse: acknowledge once, steer back to work, never mirror it.
_HOSTILE = re.compile(r"\b(f+u+c*k|f\*+k|bitch|bastard|idiot|stupid|shut up)\b", re.I)

_FALLBACKS = (
    "Not sure what you need from me there — I'm best at finding files.",
    "That one's past me. Ask for a file and I'll have a go.",
)


def reply_for(text):
    """A short reply for ordinary chat, or None if nothing fits."""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()

    if _HOSTILE.search(low):
        return ("I'll pass on that one. If you want something from the index, "
                "say <i>Bonki find &lt;name&gt;</i>.")

    for pattern, reply in _RULES:
        if re.search(pattern, low):
            return reply

    # a bare question mark with a few words - at least say something human
    if low.endswith("?") and len(low.split()) <= 6:
        return None
    return None
