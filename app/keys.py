"""Adding and checking AI keys from inside Telegram.

What this does: you paste a key once with /setkey, Bonki **tests it**, stores it,
and from then on uses it automatically — including rotating to a second key when
the first hits its free quota.

What it deliberately does NOT do: find or generate keys for you. A key is issued by
a provider to an account holder; no bot can create one, and scraping leaked keys
off the internet would be using someone else's credentials. /freeai lists where the
genuinely free ones come from.
"""
import asyncio
import logging
import re

from . import db, llm

log = logging.getLogger("keys")

# Free tiers worth knowing. Signup only - a key is never shared or scraped.
FREE_TIERS = [
    # --- local, open-source, no key at all ---
    ("Ollama", "ollama", "https://ollama.com/download",
     "runs Llama/Qwen/Gemma locally — no key, no cost"),
    ("LM Studio", "lmstudio", "https://lmstudio.ai/",
     "desktop app, OpenAI-compatible on :1234"),
    ("vLLM", "vllm", "https://docs.vllm.ai/",
     "self-host open models at speed"),
    ("llama.cpp server", "llamacpp", "https://github.com/ggerganov/llama.cpp",
     "runs GGUF models on modest hardware"),
    ("LocalAI", "localai", "https://localai.io/",
     "drop-in OpenAI replacement, local"),
    # --- free tiers hosting open-weight models ---
    ("Google Gemini", "gemini", "https://aistudio.google.com/app/apikey",
     "generous free tier, no card"),
    ("Groq", "groq", "https://console.groq.com/keys",
     "very fast, free tier, no card"),
    ("OpenRouter", "openrouter", "https://openrouter.ai/keys",
     "free ':free' open models"),
    ("Cerebras", "cerebras", "https://cloud.cerebras.ai/",
     "free tier, fast inference"),
    ("Mistral", "mistral", "https://console.mistral.ai/api-keys/",
     "free tier, open models"),
    ("GitHub Models", "github", "https://github.com/settings/tokens",
     "free with a GitHub token"),
    ("Together", "together", "https://api.together.ai/settings/api-keys",
     "free/trial open models"),
    ("SambaNova", "sambanova", "https://cloud.sambanova.ai/",
     "free tier, Llama models"),
    ("Hyperbolic", "hyperbolic", "https://app.hyperbolic.xyz/settings",
     "cheap open-model hosting"),
    ("Fireworks", "fireworks", "https://fireworks.ai/api-keys",
     "fast open-model inference"),
    ("DeepInfra", "deepinfra", "https://deepinfra.com/dash/api_keys",
     "cheap, open models"),
    ("Hugging Face", "huggingface", "https://huggingface.co/settings/tokens",
     "free inference credits"),
]


def mask(key):
    k = str(key or "")
    if len(k) <= 8:
        return "…"
    return k[:4] + "…" + k[-4:]


def _settings_key(name):
    return f"{name}_api_key"


def add_key(name, key):
    """Append a key to that provider's pool. Returns (added, total)."""
    sp = llm._spec(name)
    if not sp:
        raise RuntimeError(f"unknown provider '{name}'")
    key = (key or "").strip()
    if not key:
        raise RuntimeError("empty key")
    cur = str(db.get_settings().get(_settings_key(name)) or "")
    existing = [k.strip() for k in re.split(r"[,\n;]+", cur) if k.strip()]
    if key in existing:
        return False, len(existing)
    existing.append(key)
    db.set_settings({_settings_key(name): ",".join(existing)})
    db.flush(force=True)
    return True, len(existing)


def remove_keys(name):
    db.set_settings({_settings_key(name): ""})
    db.flush(force=True)


async def test_key(name, key):
    """Make one tiny call so a bad key is caught now, not mid-conversation."""
    sp = llm._spec(name)
    if not sp:
        raise RuntimeError(f"unknown provider '{name}'")
    try:
        if sp["kind"] == "gemini":
            out = await asyncio.to_thread(llm._gemini, "ping", None, 0.0, 8, False,
                                          sp, key)
        else:
            out = await asyncio.to_thread(llm._openai, "ping", None, 0.0, 8, False,
                                          sp, key)
    except Exception as e:
        raise RuntimeError(f"{type(e).__name__}: {e}") from e
    if out is None:
        raise RuntimeError("provider returned nothing")
    return True


def status():
    """Per-provider: how many keys, masked, and which one is active."""
    active = llm.provider()
    rows = []
    for sp in llm.SPECS:
        keys = llm._keys_for(sp)
        # ollama always "has a key" (its local default) - don't list it as configured
        if sp["name"] == "ollama" and not (db.get_settings().get("ollama_api_key")
                                           or db.get_settings().get("ollama_base_url")):
            keys = []
        rows.append({
            "name": sp["name"],
            "count": len(keys),
            "masked": mask(keys[0]) if keys else "",
            "active": sp["name"] == active,
        })
    return rows

# Prefixes providers use. Detection means the owner can just paste a key in DM
# instead of remembering a command - it is tested and saved automatically.
_PATTERNS = [
    ("openrouter", re.compile(r"^sk-or-v1-[A-Za-z0-9]{20,}$")),
    ("openai",     re.compile(r"^sk-(proj-)?[A-Za-z0-9_\-]{20,}$")),
    ("groq",       re.compile(r"^gsk_[A-Za-z0-9]{20,}$")),
    ("cerebras",   re.compile(r"^csk-[A-Za-z0-9]{20,}$")),
    ("huggingface", re.compile(r"^hf_[A-Za-z0-9]{20,}$")),
    ("gemini",     re.compile(r"^AIza[0-9A-Za-z_\-]{30,}$")),
    ("together",   re.compile(r"^[0-9a-f]{64}$")),
]


def detect(text):
    """(provider, key) if the text is a bare API key, else (None, None)."""
    t = (text or "").strip().strip('"\'')
    if not t or " " in t or len(t) < 20:
        return None, None
    for name, rx in _PATTERNS:
        if rx.match(t):
            return name, t
    return None, None


async def auto_add(text):
    """Detect + test + save + persist a pasted key. Returns a message or None."""
    from . import envfile
    name, key = detect(text)
    if not name:
        return None
    try:
        await test_key(name, key)
    except Exception as e:
        return (f"🔑 That looks like a <b>{name}</b> key, but it was rejected: "
                f"{str(e)[:120]}")
    is_new, total = add_key(name, key)
    if not is_new:
        return f"🔑 That <b>{name}</b> key is already saved."
    # write it to .env too, so a redeploy does not lose it
    saved_disk = envfile.set_env(f"{name.upper()}_API_KEY", key)
    return (f"✅ Saved a <b>{name}</b> key (<code>{mask(key)}</code>). "
            f"Pool now holds {total}.\n"
            + ("Also written to .env, so it survives a restart."
               if saved_disk else
               "<i>Could not write .env on this host — it is in the state store "
               "only.</i>"))
