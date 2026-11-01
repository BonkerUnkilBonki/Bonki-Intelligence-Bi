"""Answering questions from the group's own memory - retrieval, then generation.

This is the honest version of "smart like Mira". Mira's edge inside a group is not
its database of other people's chats (that is proprietary and private); it is that
it remembers *this* group and can answer from it. Bonki already stores files,
decisions, learned memory and analytics - this module retrieves from all of them
and answers.

Two modes:
  * **with an LLM key** - retrieval-augmented generation: the retrieved facts are
    put in the prompt and the model writes the answer. This is the good path.
  * **without a key** - pattern-matched answers straight from the stored data, so
    the common questions still work offline and nothing is invented.

Every answer carries its source, so you can tell a real fact from a fallback.
"""
import logging
import re
import time

from . import analytics, db, llm, memory

log = logging.getLogger("ask")

_SYSTEM = ("You answer questions about a Telegram group using ONLY the facts "
           "provided. If the facts do not contain the answer, say you don't know "
           "rather than guessing. Be brief and concrete.")


def _fmt_size(b):
    try:
        b = float(b or 0)
    except (TypeError, ValueError):
        b = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.1f} {unit}".replace(".0 ", " ")
        b /= 1024.0
    return f"{b:.1f} TB"


def _when(ts):
    try:
        return time.strftime("%d %b %Y", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "unknown"


def gather(chat_id, question):
    """Pull the facts a question could be answered from."""
    files = db.list_files(chat_id=chat_id)
    q = (question or "").lower()
    words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 2]

    matched = [f for f in files
               if any(w in (f.get("name") or "").lower() for w in words)][:12]
    decisions = db.list_decisions(chat_id, 8)
    mems = memory.search(question, 5) if question else []
    st = analytics.compute(chat_id)

    return {"files": files, "matched": matched, "decisions": decisions,
            "memory": mems, "stats": st}


def _facts_block(data):
    st = data["stats"]
    lines = [f"Group: {st['files']} files indexed, {_fmt_size(st['size'])} total."]
    if st.get("members"):
        lines.append(f"Members: {st['members']}.")
    if st.get("types"):
        lines.append("Top types: " + ", ".join(f"{e} {n}" for e, n in st["types"][:4]))
    if st.get("senders"):
        lines.append("Top sharers: " + ", ".join(f"{s} {n}" for s, n in st["senders"][:3]))
    if data["matched"]:
        lines.append("Matching files:")
        for f in data["matched"][:8]:
            lines.append(f"- {f.get('name')} ({_fmt_size(f.get('size'))}, "
                         f"shared {_when(f.get('date'))})")
    if data["decisions"]:
        lines.append("Recorded decisions:")
        for d in data["decisions"][:5]:
            lines.append(f"- {d.get('text')} ({_when(d.get('t'))})")
    if data["memory"]:
        lines.append("Learned notes:")
        for m in data["memory"][:4]:
            meta = m.get("meta") or {}
            lines.append(f"- {meta.get('name') or (m.get('text') or '')[:60]}"
                         f" {meta.get('summary') or ''}".strip())
    return "\n".join(lines)


def _offline(question, data):
    """Answer without a model, from the stored facts only."""
    q = (question or "").lower()
    st = data["stats"]
    files = data["files"]

    if not files:
        return ("Nothing indexed in this chat yet, so I have no facts to answer "
                "from. Post a file and I'll start remembering.")

    if re.search(r"\b(how many|count|number of)\b", q):
        return (f"{st['files']} file(s) indexed, {_fmt_size(st['size'])} in total.")

    if re.search(r"\b(who|which (user|person))\b.*\b(most|top|shares?)\b", q) and st.get("senders"):
        top = ", ".join(f"{s} ({n})" for s, n in st["senders"][:3])
        return f"Most active sharers: {top}."

    if re.search(r"\b(latest|newest|recent)\b", q):
        newest = max(files, key=lambda f: f.get("date") or 0)
        return (f"The newest file is <b>{newest.get('name')}</b>, shared "
                f"{_when(newest.get('date'))}.")

    if re.search(r"\b(biggest|largest|heaviest)\b", q):
        big = max(files, key=lambda f: f.get("size") or 0)
        return f"The biggest file is <b>{big.get('name')}</b> ({_fmt_size(big.get('size'))})."

    if re.search(r"\b(decide|decided|decision|agreed)\b", q):
        if data["decisions"]:
            out = ["<b>Recorded decisions</b>"]
            for d in data["decisions"][:5]:
                out.append(f"• {d.get('text')} — {_when(d.get('t'))}")
            return "\n".join(out)
        return "No decisions have been recorded here. Someone can save one with <code>/decide …</code>."

    if data["matched"]:
        out = ["<b>Matching files</b>"]
        for f in data["matched"][:6]:
            out.append(f"• {f.get('name')} — {_fmt_size(f.get('size'))}, "
                       f"{_when(f.get('date'))}")
        return "\n".join(out)

    if st.get("types"):
        return ("I can answer questions about this group's files, decisions and "
                "activity. From what I have: "
                + ", ".join(f"{e} {n}" for e, n in st["types"][:4]) + ".")
    return "I don't have an answer for that from what's stored here."


async def answer(question, chat_id):
    """(text, source) - source is 'llm', 'local' or 'empty'."""
    data = gather(chat_id, question)

    if llm.enabled() and data["files"]:
        facts = _facts_block(data)
        try:
            import asyncio
            out = await asyncio.to_thread(
                llm.complete,
                f"Facts about this Telegram group:\n{facts}\n\nQuestion: {question}",
                system=_SYSTEM, max_tokens=350)
            if out:
                return out.strip(), "llm"
        except Exception as e:
            log.info("llm answer failed, falling back: %s", e)

    text = _offline(question, data)
    return text, ("empty" if not data["files"] else "local")
