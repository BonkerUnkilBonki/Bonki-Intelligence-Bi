"""Free and open-source AI, auto-connected - Ollama.

Ollama runs **open-weight models** (Llama, Qwen, Gemma, Mistral, Phi, DeepSeek…)
on a machine you control. No account, no key, no cost, no data leaving your box.
That is what "free and open source AI" actually means in practice.

Bonki auto-detects an Ollama server, picks a sensible model, and switches the brain
to it. If none is reachable it explains how to start one instead of failing quietly.
"""
import json
import logging
import os

import requests

from . import db

log = logging.getLogger("ollama")

# Where an Ollama server might be. Order matters: local first, then the usual
# docker-host alias, then anything explicitly configured.
CANDIDATES = (
    "http://localhost:11434",
    "http://127.0.0.1:11434",
    "http://host.docker.internal:11434",
    "http://ollama:11434",
)

# prefer small-but-capable open models that run on modest hardware
PREFERRED = ("llama3.2", "llama3.1", "qwen2.5", "gemma2", "mistral", "phi3",
             "deepseek-r1", "tinyllama")


def _bases():
    out = []
    for raw in (db.get_settings().get("ollama_base_url") or "",
                os.environ.get("OLLAMA_BASE_URL") or "",
                os.environ.get("OLLAMA_HOST") or ""):
        u = (raw or "").strip().rstrip("/")
        if not u:
            continue
        if u.endswith("/v1"):
            u = u[:-3]
        if not u.startswith("http"):
            u = "http://" + u
        if u not in out:
            out.append(u)
    for u in CANDIDATES:
        if u not in out:
            out.append(u)
    return out


def probe(base, timeout=3):
    """[(name, size_mb)] on a reachable Ollama server, else None."""
    try:
        r = requests.get(f"{base.rstrip('/')}/api/tags", timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json() or {}
        out = []
        for m in data.get("models") or []:
            name = m.get("name") or m.get("model") or ""
            if not name:
                continue
            size = round((m.get("size") or 0) / (1024 * 1024))
            out.append((name, size))
        return out
    except Exception:
        return None


def pick(models):
    """Choose a model from what is installed, preferring a good general one."""
    names = [n for n, _ in models]
    for want in PREFERRED:
        for n in names:
            if n.split(":")[0] == want or n.startswith(want):
                return n
    return names[0] if names else ""


def autodetect(apply=True):
    """Find a working Ollama, choose a model, and switch the brain to it.

    Returns {"ok", "base", "model", "models", "reason"}.
    """
    for base in _bases():
        models = probe(base)
        if not models:
            continue
        model = pick(models)
        if apply:
            db.set_settings({
                "llm_provider": "ollama",
                "ollama_base_url": f"{base.rstrip('/')}/v1",
                "ollama_model": model,
                "ollama_detected_at": str(int(__import__("time").time())),
            })
            try:
                db.flush(force=True)
            except Exception:
                pass
        return {"ok": True, "base": base, "model": model, "models": models}
    return {"ok": False, "reason": "no Ollama server found",
            "tried": _bases()}


SETUP = (
    "<b>Run open-source AI yourself (free, no key)</b>\n\n"
    "<b>1. Install Ollama</b> — <a href=\"https://ollama.com/download\">"
    "ollama.com/download</a> (Windows, macOS, Linux)\n\n"
    "<b>2. Pull a model</b> (pick by your RAM):\n"
    "• <code>ollama pull llama3.2</code> — 2 GB, good all-rounder\n"
    "• <code>ollama pull qwen2.5</code> — 4.7 GB, strong at multilingual\n"
    "• <code>ollama pull gemma2</code> — 5 GB, fast\n"
    "• <code>ollama pull tinyllama</code> — 640 MB, for weak machines\n\n"
    "<b>3. Let Bonki reach it</b>\n"
    "• Same machine as the bot: nothing to do — I detect "
    "<code>localhost:11434</code> automatically.\n"
    "• Bot on a server, Ollama on your PC: start Ollama with "
    "<code>OLLAMA_HOST=0.0.0.0</code>, then give me the address:\n"
    "  <code>/ollama url http://YOUR-IP:11434</code>\n"
    "  (use a tunnel like Tailscale or Cloudflare Tunnel rather than opening "
    "the port to the internet)\n\n"
    "<b>4. Check</b> — <code>/ollama</code> shows the server, the model, and what "
    "else is installed."
)


async def cmd_ollama(chat, msg, user, args):
    """Status, auto-detect, or point Bonki at an Ollama server."""
    import asyncio

    from . import llm, rose
    parts = (args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("setup", "help", "how"):
        return await rose._reply(chat, msg, SETUP)

    if sub == "url":
        if not rest:
            return await rose._reply(chat, msg,
                                     "Usage: <code>/ollama url http://192.168.1.5:11434</code>")
        base = rest.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        models = await asyncio.to_thread(probe, base, 6)
        if not models:
            return await rose._reply(chat, msg,
                                     f"⚠️ Nothing answered at <code>{telegram_esc(base)}</code>. "
                                     f"Is Ollama running with OLLAMA_HOST=0.0.0.0, and is "
                                     f"the port reachable from here?")
        db.set_settings({"ollama_base_url": f"{base}/v1", "ollama_model": pick(models),
                         "llm_provider": "ollama"})
        await asyncio.to_thread(db.flush, True)
        return await rose._reply(chat, msg,
                                 f"✅ Connected to Ollama at <code>{telegram_esc(base)}</code> "
                                 f"using <b>{pick(models)}</b>.")

    if sub in ("detect", "auto", "connect"):
        await rose._reply(chat, msg, "🔍 Looking for an Ollama server…")
        res = await asyncio.to_thread(autodetect, True)
        if not res.get("ok"):
            return await rose._reply(
                chat, msg,
                "❌ No Ollama server found. Tried: " +
                ", ".join(f"<code>{telegram_esc(b)}</code>" for b in res.get("tried", [])[:4]) +
                "\n\nRun <code>/ollama setup</code> for the 3-step install.")
        return await rose._reply(
            chat, msg,
            f"✅ <b>Open-source AI connected</b>\n"
            f"• server: <code>{telegram_esc(res['base'])}</code>\n"
            f"• model: <b>{telegram_esc(res['model'])}</b>\n"
            f"• installed: {len(res.get('models') or [])} model(s)\n\n"
            f"The brain is now local — no key, no cost, nothing leaves your machine.")

    # status
    base = (db.get_settings().get("ollama_base_url") or "").replace("/v1", "")
    models = await asyncio.to_thread(probe, base, 4) if base else None
    provider = llm.provider()
    lines = ["🦙 <b>Open-source AI (Ollama)</b>", ""]
    if models:
        lines.append(f"• server: <code>{telegram_esc(base)}</code> ✅")
        lines.append(f"• using: <b>{telegram_esc(db.get_settings().get('ollama_model') or pick(models))}</b>")
        lines.append("• installed: " + ", ".join(f"<code>{telegram_esc(n)}</code>"
                                                 for n, _ in models[:8]))
        lines.append(f"• brain: {'✅ ollama' if provider == 'ollama' else provider}")
    else:
        lines.append("• no Ollama server reachable")
        lines.append("• <code>/ollama setup</code> — install it in 3 steps")
        lines.append("• <code>/ollama detect</code> — search again")
        lines.append("• <code>/ollama url http://IP:11434</code> — point at a remote one")
    await rose._reply(chat, msg, "\n".join(lines))


def telegram_esc(s):
    from . import telegram
    return telegram.esc(s)
