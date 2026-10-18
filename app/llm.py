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


def provider():
    p = (_s("llm_provider", "LLM_PROVIDER", "off") or "off").strip().lower()
    return p if p in ("gemini", "openai") else "off"


def enabled():
    p = provider()
    if p == "gemini":
        return bool(_s("gemini_api_key", "GEMINI_API_KEY"))
    if p == "openai":
        return bool(_s("openai_api_key", "OPENAI_API_KEY"))
    return False


def describe():
    p = provider()
    if p == "gemini":
        return f"gemini ({_s('gemini_model', 'GEMINI_MODEL', 'gemini-2.0-flash')})"
    if p == "openai":
        return f"openai-compatible ({_s('openai_model', 'OPENAI_MODEL', 'gpt-4o-mini')})"
    return "off (keyword mode)"


def complete(prompt, system=None, temperature=0.2, max_tokens=900, json_mode=False):
    """Return the model's text, or None if the brain is off / unreachable."""
    p = provider()
    try:
        if p == "gemini":
            return _gemini(prompt, system, temperature, max_tokens, json_mode)
        if p == "openai":
            return _openai(prompt, system, temperature, max_tokens, json_mode)
    except Exception as e:
        log.warning("llm(%s) failed: %s", p, e)
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
def _gemini(prompt, system, temperature, max_tokens, json_mode):
    key = _s("gemini_api_key", "GEMINI_API_KEY")
    model = _s("gemini_model", "GEMINI_MODEL", "gemini-2.0-flash")
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    r = requests.post(f"{GEMINI_API}/{model}:generateContent",
                      params={"key": key}, json=body, timeout=45)
    r.raise_for_status()
    text = ""
    for cand in r.json().get("candidates", []):
        for part in (cand.get("content") or {}).get("parts", []):
            text += part.get("text") or ""
    return text.strip() or None


def _openai(prompt, system, temperature, max_tokens, json_mode):
    key = _s("openai_api_key", "OPENAI_API_KEY")
    base = _s("openai_base_url", "OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = _s("openai_model", "OPENAI_MODEL", "gpt-4o-mini")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(f"{base}/chat/completions", json=body, timeout=45,
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"})
    r.raise_for_status()
    data = r.json()
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip() or None
