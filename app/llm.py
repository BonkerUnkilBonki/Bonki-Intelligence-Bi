"""The switchable brain.

One small interface - `complete()` and `complete_json()` - with two backends the
dashboard can flip between:

  * "gemini"  - Google Gemini (generativelanguage API)
  * "openai"  - any OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq, Ollama,
                vLLM, LM Studio, ...) via base_url + key + model
  * "off"     - no LLM; the agent falls back to deterministic keyword matching

Every caller treats a None return as "the brain is unavailable" and degrades
gracefully, so the bot keeps working with no API key at all.
"""
import json
import logging
import os
import re
import time

import requests

from . import db

log = logging.getLogger("llm")

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models"


def _s(key, env=None, default=""):
    """A setting from the dashboard, falling back to an env var."""
    v = db.get_settings().get(key)
    if v not in (None, ""):
        return str(v)
    if env and os.environ.get(env):
        return os.environ[env]
    return default


# --------------------------------------------------------- model registry ----
# Each entry is Gemini's native API or an OpenAI-compatible endpoint.
#   auto-hook   : any provider whose key is present is used with no config
#   auto-switch : if the active model errors (quota, outage, bad key) the next
#                 available one is used and remembered, so the bot keeps working
SPECS = [
    {"name": "gemini", "kind": "gemini", "key": "gemini_api_key",
     "env": "GEMINI_API_KEY",
     "model": ("gemini_model", "GEMINI_MODEL", "gemini-2.0-flash")},
    {"name": "openai", "kind": "openai", "key": "openai_api_key",
     "env": "OPENAI_API_KEY",
     "base": ("openai_base_url", "OPENAI_BASE_URL", "https://api.openai.com/v1"),
     "model": ("openai_model", "OPENAI_MODEL", "gpt-4o-mini")},
    {"name": "openrouter", "kind": "openai", "key": "openrouter_api_key",
     "env": "OPENROUTER_API_KEY",
     "base": ("openrouter_base_url", "OPENROUTER_BASE_URL",
              "https://openrouter.ai/api/v1"),
     "model": ("openrouter_model", "OPENROUTER_MODEL",
               "meta-llama/llama-3.3-70b-instruct:free")},
    {"name": "groq", "kind": "openai", "key": "groq_api_key",
     "env": "GROQ_API_KEY",
     "base": ("groq_base_url", "GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
     "model": ("groq_model", "GROQ_MODEL", "llama-3.1-8b-instant")},
    {"name": "together", "kind": "openai", "key": "together_api_key",
     "env": "TOGETHER_API_KEY",
     "base": ("together_base_url", "TOGETHER_BASE_URL", "https://api.together.xyz/v1"),
     "model": ("together_model", "TOGETHER_MODEL",
               "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free")},
    {"name": "cerebras", "kind": "openai", "key": "cerebras_api_key",
     "env": "CEREBRAS_API_KEY",
     "base": ("cerebras_base_url", "CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1"),
     "model": ("cerebras_model", "CEREBRAS_MODEL", "llama3.1-8b")},
    {"name": "deepinfra", "kind": "openai", "key": "deepinfra_api_key",
     "env": "DEEPINFRA_API_KEY",
     "base": ("deepinfra_base_url", "DEEPINFRA_BASE_URL",
              "https://api.deepinfra.com/v1/openai"),
     "model": ("deepinfra_model", "DEEPINFRA_MODEL",
               "meta-llama/Meta-Llama-3.1-8B-Instruct")},
    {"name": "huggingface", "kind": "openai", "key": "hf_api_key",
     "env": "HF_API_KEY",
     "base": ("hf_base_url", "HF_BASE_URL", "https://api-inference.huggingface.co/v1"),
     "model": ("hf_model", "HF_MODEL", "meta-llama/Llama-3.1-8B-Instruct")},
    {"name": "ollama", "kind": "openai", "key": "ollama_api_key",
     "env": "OLLAMA_API_KEY",
     "base": ("ollama_base_url", "OLLAMA_BASE_URL", "http://localhost:11434/v1"),
     "model": ("ollama_model", "OLLAMA_MODEL", "llama3.2"), "local": True},

    # ---- other local open-source servers (all OpenAI-compatible, no key) ----
    {"name": "lmstudio", "kind": "openai", "key": "lmstudio_api_key",
     "env": "LMSTUDIO_API_KEY",
     "base": ("lmstudio_base_url", "LMSTUDIO_BASE_URL", "http://localhost:1234/v1"),
     "model": ("lmstudio_model", "LMSTUDIO_MODEL", "local-model"), "local": True},
    {"name": "vllm", "kind": "openai", "key": "vllm_api_key",
     "env": "VLLM_API_KEY",
     "base": ("vllm_base_url", "VLLM_BASE_URL", "http://localhost:8000/v1"),
     "model": ("vllm_model", "VLLM_MODEL", "meta-llama/Llama-3.1-8B-Instruct"),
     "local": True},
    {"name": "llamacpp", "kind": "openai", "key": "llamacpp_api_key",
     "env": "LLAMACPP_API_KEY",
     "base": ("llamacpp_base_url", "LLAMACPP_BASE_URL", "http://localhost:8080/v1"),
     "model": ("llamacpp_model", "LLAMACPP_MODEL", "local-model"), "local": True},
    {"name": "localai", "kind": "openai", "key": "localai_api_key",
     "env": "LOCALAI_API_KEY",
     "base": ("localai_base_url", "LOCALAI_BASE_URL", "http://localhost:8080/v1"),
     "model": ("localai_model", "LOCALAI_MODEL", "llama-3.2-3b-instruct"),
     "local": True},

    # ---- free/cheap hosts of open-weight models ----
    {"name": "mistral", "kind": "openai", "key": "mistral_api_key",
     "env": "MISTRAL_API_KEY",
     "base": ("mistral_base_url", "MISTRAL_BASE_URL", "https://api.mistral.ai/v1"),
     "model": ("mistral_model", "MISTRAL_MODEL", "open-mistral-nemo")},
    {"name": "github", "kind": "openai", "key": "github_models_key",
     "env": "GITHUB_MODELS_TOKEN",
     "base": ("github_models_base_url", "GITHUB_MODELS_BASE_URL",
              "https://models.inference.ai.azure.com"),
     "model": ("github_models_model", "GITHUB_MODELS_MODEL", "gpt-4o-mini")},
    {"name": "sambanova", "kind": "openai", "key": "sambanova_api_key",
     "env": "SAMBANOVA_API_KEY",
     "base": ("sambanova_base_url", "SAMBANOVA_BASE_URL",
              "https://api.sambanova.ai/v1"),
     "model": ("sambanova_model", "SAMBANOVA_MODEL",
               "Meta-Llama-3.1-8B-Instruct")},
    {"name": "hyperbolic", "kind": "openai", "key": "hyperbolic_api_key",
     "env": "HYPERBOLIC_API_KEY",
     "base": ("hyperbolic_base_url", "HYPERBOLIC_BASE_URL",
              "https://api.hyperbolic.xyz/v1"),
     "model": ("hyperbolic_model", "HYPERBOLIC_MODEL",
               "meta-llama/Llama-3.3-70B-Instruct")},
    {"name": "fireworks", "kind": "openai", "key": "fireworks_api_key",
     "env": "FIREWORKS_API_KEY",
     "base": ("fireworks_base_url", "FIREWORKS_BASE_URL",
              "https://api.fireworks.ai/inference/v1"),
     "model": ("fireworks_model", "FIREWORKS_MODEL",
               "accounts/fireworks/models/llama-v3p1-8b-instruct")},
]


def _spec(name):
    for sp in SPECS:
        if sp["name"] == name:
            return sp
    return None


def _keys_for(sp):
    """All keys for a provider: settings (comma/newline separated) then env.

    Supporting several keys per provider is what makes "auto" useful - when one
    hits its free quota, the next is used instead of the bot going silent.
    """
    if not sp:
        return []
    raw = _s(sp["key"], sp["env"])
    out, seen = [], set()
    for part in re.split(r"[,\n;]+", raw or ""):
        k = part.strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    if not out and sp.get("local"):
        return ["local"]                    # local server needs no real key
    return out


_NOTIFY_COOLDOWN = 600          # don't repeat the same notice within 10 minutes


def _notify_owner(text, key=""):
    """DM the owner when an AI model starts or the bot switches between them.

    Throttled per provider so a flapping endpoint cannot spam the DM.
    """
    try:
        from . import approval, telegram
        oid = approval.owner_id()
        if not oid:
            return
        now = time.time()
        try:
            last = float(db.get_settings().get("llm_notify_last") or 0)
            last_key = db.get_settings().get("llm_notify_key") or ""
        except (TypeError, ValueError):
            last, last_key = 0, ""
        if key and key == last_key and (now - last) < _NOTIFY_COOLDOWN:
            return
        telegram.send_message(text, chat_id=oid)
        db.set_settings({"llm_notify_last": str(int(now)), "llm_notify_key": key})
    except Exception as e:
        log.info("owner notify failed: %s", e)


def describe_model():
    """'provider / model' for the active brain, for messages and the dashboard."""
    name = provider()
    if name == "off":
        return "built-in ML (no AI model)"
    sp = _spec(name) or {}
    model = _s(*sp["model"]) if sp.get("model") else ""
    return f"{name} / {model}" if model else name


def _health():
    try:
        return json.loads(db.get_settings().get("key_health") or "{}")
    except Exception:
        return {}


def _save_health(h):
    try:
        db.set_settings({"key_health": json.dumps(h)[:8000]})
    except Exception:
        pass


def _kid(name, key):
    """Stable id for a key without storing it in the health map."""
    k = str(key or "")
    return f"{name}:{k[-6:] if len(k) > 6 else k}"


def key_ready(name, key, health=None):
    """False while a key is cooling down after a quota/auth failure."""
    h = health if health is not None else _health()
    e = h.get(_kid(name, key)) or {}
    return float(e.get("until") or 0) <= time.time()


def mark_fail(name, key, err):
    """A 429 cools for 15 min; an auth failure cools for 6 hours."""
    h = _health()
    text = str(err).lower()
    mins = 15
    if "401" in text or "invalid" in text or "unauthor" in text or "403" in text:
        mins = 360
    h[_kid(name, key)] = {"until": time.time() + mins * 60,
                          "err": str(err)[:120], "t": time.time()}
    _save_health(h)
    log.info("key %s cooling for %dm", _kid(name, key), mins)


def mark_ok(name, key):
    h = _health()
    if h.pop(_kid(name, key), None) is not None:
        _save_health(h)


def _key_for(sp):
    ks = _keys_for(sp)
    return ks[0] if ks else ""


def _explicit(sp):
    """Was this provider configured on purpose (a key, or a base URL for local)?"""
    if _keys_for(sp):
        # a local spec reports a placeholder key, so check the base separately
        if not sp.get("local"):
            return True
    if sp.get("local"):
        base_setting, base_env = sp["base"][0], sp["base"][1]
        return bool((db.get_settings().get(base_setting) or "").strip()
                    or (os.environ.get(base_env) or "").strip())
    return False


def available():
    """Auto-hook: providers that have credentials right now, in priority order.

    A local server counts ONLY if it was explicitly configured. Otherwise every
    message would try to reach a dead localhost:1234 before answering, which made
    the bot look dead instead of using its built-in ML.
    """
    out = []
    for sp in SPECS:
        if _explicit(sp):
            out.append(sp["name"])
    return out


def provider():
    want = (_s("llm_provider", "LLM_PROVIDER", "") or "").strip().lower()
    av = available()
    if want in av:
        return want
    return av[0] if av else "off"


def enabled():
    return provider() != "off"


def describe():
    p = provider()
    if p == "gemini":
        return f"gemini ({_s('gemini_model', 'GEMINI_MODEL', 'gemini-2.0-flash')})"
    if p == "openai":
        return f"openai-compatible ({_s('openai_model', 'OPENAI_MODEL', 'gpt-4o-mini')})"
    return "off (keyword mode)"


# ------------------------------------------------------------- embeddings ---
def embed(texts):
    """Embed texts with a pretrained model. Returns list[list[float]] or None."""
    if not texts:
        return []
    p = provider()
    try:
        if p == "gemini":
            return _gemini_embed(texts)
        if p == "openai":
            return _openai_embed(texts)
    except Exception as e:
        log.info("embedding request failed: %s", e)
    return None


def _gemini_embed(texts):
    key = _s("gemini_api_key", "GEMINI_API_KEY")
    model = _s("gemini_embed_model", "GEMINI_EMBED_MODEL", "text-embedding-004")
    body = {"requests": [{"model": f"models/{model}",
                          "content": {"parts": [{"text": (t or "")[:2000]}]}}
                         for t in texts]}
    r = requests.post(f"{GEMINI_API}/{model}:batchEmbedContents",
                      params={"key": key}, json=body, timeout=20)
    r.raise_for_status()
    return [e.get("values") for e in r.json().get("embeddings", [])]


def _openai_embed(texts):
    key = _s("openai_api_key", "OPENAI_API_KEY")
    base = _s("openai_base_url", "OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = _s("openai_embed_model", "OPENAI_EMBED_MODEL", "text-embedding-3-small")
    r = requests.post(f"{base}/embeddings", timeout=20,
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "input": [(t or "")[:2000] for t in texts]})
    r.raise_for_status()
    return [d.get("embedding") for d in r.json().get("data", [])]


def complete(prompt, system=None, temperature=0.2, max_tokens=900, json_mode=False):
    """Return the model's text, or None if every model is unreachable.

    Tries the active model first, then the other available ones. A success on a
    fallback becomes the new active model, so a dead key or an exhausted free
    quota degrades instead of taking the bot down.
    """
    active = provider()
    order = [active] if active != "off" else []
    for name in available():
        if name not in order:
            order.append(name)
    for name in order:
        sp = _spec(name)
        if not sp:
            continue
        all_keys = _keys_for(sp) or [None]
        health = _health()
        ready = [k for k in all_keys if k is None or key_ready(name, k, health)]
        # if every key is cooling, try them anyway rather than giving up
        keys = ready or all_keys
        for idx, key in enumerate(keys):
            try:
                if sp["kind"] == "gemini":
                    out = _gemini(prompt, system, temperature, max_tokens,
                                  json_mode, sp, key)
                else:
                    out = _openai(prompt, system, temperature, max_tokens,
                                  json_mode, sp, key)
                if out:
                    if key:
                        mark_ok(name, key)
                    # only announce a "start" when this IS the active model -
                    # otherwise the switch notice below says it all
                    if name == active and not db.get_settings().get("llm_started_notified"):
                        db.set_settings({"llm_started_notified": "1"})
                        _notify_owner(
                            f"🟢 <b>AI model started</b>\n"
                            f"• {describe_model()}\n"
                            f"• key pool: {len(all_keys)} key(s)"
                            + ("\n• local model — free, nothing leaves your machine"
                               if sp.get("local") else ""),
                            key=f"start:{name}")
                    if name != active:
                        db.set_settings({"llm_provider": name,
                                         "llm_switched_to": name,
                                         "llm_switched_at": str(int(time.time()))})
                        log.warning("llm auto-switched to %s", name)
                        _notify_owner(
                            f"🔀 <b>Switched AI model</b>\n"
                            f"• now: <b>{name}</b>"
                            + (f" / <code>{_s(*sp['model'])}</code>"
                               if sp.get("model") else "")
                            + f"\n• was: {active or 'nothing'}"
                            + ("\n• reason: the previous model failed or hit its quota"
                               if active and active != "off" else ""),
                            key=f"switch:{name}")
                    if idx:
                        log.info("llm(%s) answered on key #%d", name, idx + 1)
                    return out
            except Exception as e:
                log.warning("llm(%s) key #%d failed: %s", name, idx + 1, e)
                if key:
                    mark_fail(name, key, e)
    return None


def complete_json(prompt, system=None, temperature=0.0, max_tokens=700):
    """Ask for JSON and parse it leniently (handles ```json fences)."""
    text = complete(prompt, system=system, temperature=temperature,
                    max_tokens=max_tokens, json_mode=True)
    if not text:
        return None
    return _loads(text)


def _loads(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


# --------------------------------------------------------------- backends ---
def _gemini(prompt, system, temperature, max_tokens, json_mode, spec=None,
            key=None):
    sp = spec or _spec("gemini")
    key = key or _key_for(sp)
    model = _s(*sp["model"])
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    r = requests.post(f"{GEMINI_API}/{model}:generateContent",
                      params={"key": key}, json=body, timeout=10)
    r.raise_for_status()
    text = ""
    for cand in r.json().get("candidates", []):
        for part in (cand.get("content") or {}).get("parts", []):
            text += part.get("text") or ""
    return text.strip() or None


def _openai(prompt, system, temperature, max_tokens, json_mode, spec=None,
            key=None):
    sp = spec or _spec("openai")
    key = key or _key_for(sp)
    base = _s(*sp["base"]).rstrip("/")
    model = _s(*sp["model"])
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(f"{base}/chat/completions", json=body, timeout=10,
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"})
    r.raise_for_status()
    data = r.json()
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip() or None
