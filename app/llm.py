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
    {"name": "ollama", "kind": "openai", "key": "ollama_api_key",
     "env": "OLLAMA_API_KEY",
     "base": ("ollama_base_url", "OLLAMA_BASE_URL", "http://localhost:11434/v1"),
     "model": ("ollama_model", "OLLAMA_MODEL", "llama3.2")},
]


def _spec(name):
    for sp in SPECS:
        if sp["name"] == name:
            return sp
    return None


def _key_for(sp):
    if not sp:
        return ""
    k = _s(sp["key"], sp["env"])
    if not k and sp["name"] == "ollama":
        return "ollama"                     # local server needs no real key
    return k


def available():
    """Auto-hook: providers that have credentials right now, in priority order."""
    out = []
    for sp in SPECS:
        if sp["name"] == "ollama" and not _s(sp["key"], sp["env"]):
            continue                        # only if explicitly configured
        if _key_for(sp):
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
        try:
            if sp["kind"] == "gemini":
                out = _gemini(prompt, system, temperature, max_tokens, json_mode, sp)
            else:
                out = _openai(prompt, system, temperature, max_tokens, json_mode, sp)
            if out:
                if name != active:
                    db.set_settings({"llm_provider": name,
                                     "llm_switched_to": name,
                                     "llm_switched_at": str(int(time.time()))})
                    log.warning("llm auto-switched to %s", name)
                return out
        except Exception as e:
            log.warning("llm(%s) failed: %s", name, e)
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
def _gemini(prompt, system, temperature, max_tokens, json_mode, spec=None):
    sp = spec or _spec("gemini")
    key = _key_for(sp)
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


def _openai(prompt, system, temperature, max_tokens, json_mode, spec=None):
    sp = spec or _spec("openai")
    key = _key_for(sp)
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
