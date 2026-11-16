"""One place that talks to the language model.

Providers (set LLM_PROVIDER in Streamlit "Secrets" or as an environment variable):
  ollama     - local Ollama (default; only works on your own computer)
  openai     - ANY OpenAI-compatible API: Groq, OpenRouter, Google Gemini (compat
               endpoint), OpenAI, Together ... (set LLM_BASE_URL + LLM_API_KEY)
  anthropic  - the Claude API (LLM_API_KEY)
"""

import base64
import json
import os
import re
import time

import requests

DEFAULTS = {
    "provider": "ollama",
    "base_url": "",
    "api_key": "",
    "model": "",
    "vision_model": "",
}
PROVIDER_DEFAULTS = {
    "ollama": {
        "base_url": "http://localhost:11434",
        "model": "llama3",
        "vision_model": "llava",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "",
        "vision_model": "",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "model": "claude-haiku-5-5",
        "vision_model": "",
    },
}
PROVIDER_ALIASES = {
    "groq": "openai",
    "openrouter": "openai",
    "gemini": "openai",
    "openai-compatible": "openai",
    "claude": "anthropic",
}
SECRET_KEYS = [
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_VISION_MODEL",
]


class LLMError(Exception):
    def __init__(self, message: str, looped: bool = False, kind: str = ""):
        super().__init__(message)
        self.looped = looped
        self.kind = kind   # "no_model" (404) or "no_vision" (model cannot read pictures)


GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


def load_config(secrets: dict | None = None) -> dict:
    secrets = secrets or {}

    def get(key: str) -> str:
        return str(secrets.get(key) or os.environ.get(key) or "").strip()

    provider = get("LLM_PROVIDER").lower() or "ollama"
    provider = PROVIDER_ALIASES.get(provider, provider)
    if provider not in PROVIDER_DEFAULTS:
        provider = "ollama"
    cfg = dict(DEFAULTS, provider=provider)
    d = PROVIDER_DEFAULTS[provider]
    cfg["base_url"] = (get("LLM_BASE_URL") or d["base_url"]).rstrip("/")
    cfg["api_key"] = get("LLM_API_KEY")
    cfg["model"] = get("LLM_MODEL") or d["model"]
    vision = get("LLM_VISION_MODEL") or d["vision_model"]
    if not vision and provider == "openai" and "groq.com" in cfg["base_url"]:
        vision = GROQ_VISION_MODEL   # Groq's text models (e.g. gpt-oss) cannot read pictures
    cfg["vision_model"] = vision or cfg["model"]
    return cfg


def describe_config(cfg: dict) -> str:
    names = {
        "ollama": "Local Ollama",
        "openai": "Hosted API (OpenAI-compatible)",
        "anthropic": "Claude API",
    }
    return f"{names[cfg['provider']]} · model: {cfg['model'] or 'not set'}"


def config_problem(cfg: dict) -> str | None:
    if cfg["provider"] != "ollama":
        if not cfg["api_key"]:
            return "No API key is set. Add LLM_API_KEY in the app's Secrets."
        if not cfg["model"]:
            return "No model is set. Add LLM_MODEL in the app's Secrets."
    return None


def _headers(cfg: dict) -> dict:
    if cfg["provider"] == "anthropic":
        return {
            "x-api-key": cfg["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    return {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }


def _post(cfg: dict, path: str, payload: dict, timeout: int) -> requests.Response:
    try:
        return requests.post(
            f"{cfg['base_url']}{path}",
            json=payload,
            headers=_headers(cfg),
            timeout=timeout,
        )
    except requests.exceptions.ConnectionError:
        if cfg["provider"] == "ollama":
            raise LLMError(
                f"Cannot connect to Ollama at {cfg['base_url']}. Make sure Ollama is running."
            )
        raise LLMError(
            f"Cannot reach the AI service at {cfg['base_url']}. Check LLM_BASE_URL."
        )
    except requests.exceptions.Timeout:
        raise LLMError("The AI model took too long to respond. Try a shorter question.")


_NON_CHAT = (
    "whisper",
    "tts",
    "guard",
    "embed",
    "orpheus",
    "playai",
    "moderation",
    "rerank",
)


def _not_found_message(cfg: dict) -> str:
    base = f"Model '{cfg['model']}' was not found. It may have been retired or renamed."
    if cfg["provider"] != "openai":
        return base + " Check LLM_MODEL in Secrets."
    try:
        r = requests.get(f"{cfg['base_url']}/models", headers=_headers(cfg), timeout=10)
        if r.status_code == 200:
            ids = sorted(
                m.get("id", "")
                for m in r.json().get("data", [])
                if m.get("id") and not any(w in m["id"].lower() for w in _NON_CHAT)
            )
            if ids:
                shown = ", ".join(ids[:15]) + (" ..." if len(ids) > 15 else "")
                return f"{base} Available models: {shown}."
        if r.status_code in (401, 403):
            return "The AI service rejected the API key. Check LLM_API_KEY in Secrets."
    except requests.exceptions.RequestException:
        pass
    return base + " Check LLM_MODEL and LLM_BASE_URL in Secrets."


def _raise_for_status(cfg: dict, resp: requests.Response) -> None:
    if resp.status_code == 200:
        return
    body = resp.text[:300]
    if resp.status_code == 400 and "must be a string" in body:
        hint = " See console.groq.com/docs/vision for the current vision models." if "groq.com" in cfg.get("base_url", "") else ""
        raise LLMError(
            f"The AI model '{cfg.get('model', '')}' can only read text, not pictures. "
            "Add LLM_VISION_MODEL in the app's Secrets (Manage app > Settings > Secrets) with a model that "
            "understands images." + hint,
            kind="no_vision",
        )
    if resp.status_code == 500 and "repeat" in body.lower():
        raise LLMError("The model got stuck repeating itself.", looped=True)
    if resp.status_code in (401, 403):
        raise LLMError(
            "The AI service rejected the API key. Check LLM_API_KEY in Secrets."
        )
    if resp.status_code == 404:
        raise LLMError(_not_found_message(cfg), kind="no_model")
    if resp.status_code == 429:
        raise LLMError(
            "The AI service rate limit was reached. Wait a minute and try again."
        )
    raise LLMError(f"AI service error {resp.status_code}: {body}")


def _openai_text(resp: requests.Response) -> str:
    try:
        choice = resp.json()["choices"][0]
        text = choice["message"].get("content") or ""
    except (KeyError, IndexError, ValueError, AttributeError):
        raise LLMError("The AI service returned an unexpected answer.")
    if not text.strip():
        if choice.get("finish_reason") == "length":
            raise LLMError(
                "The AI model ran out of space before finishing. Ask a shorter question."
            )
        raise LLMError("The AI model returned an empty answer.")
    return text


def _anthropic_text(resp: requests.Response) -> str:
    try:
        return "".join(
            b.get("text", "") for b in resp.json()["content"] if b.get("type") == "text"
        )
    except (KeyError, TypeError, ValueError):
        raise LLMError("The AI service returned an unexpected answer.")


def generate(
    cfg: dict,
    prompt: str,
    json_mode: bool = True,
    temperature: float = 0.2,
    repeat_penalty: float = 1.15,
    max_tokens: int = 1024,
    timeout: int = 600,
) -> str:
    problem = config_problem(cfg)
    if problem:
        raise LLMError(problem)
    provider = cfg["provider"]

    if provider == "ollama":
        payload = {
            "model": cfg["model"],
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "repeat_penalty": repeat_penalty,
                "num_ctx": 4096,
                "num_predict": max_tokens,
            },
        }
        if json_mode:
            payload["format"] = "json"
        resp = _post(cfg, "/api/generate", payload, timeout)
        _raise_for_status(cfg, resp)
        return resp.json().get("response", "")

    if provider == "openai":
        payload = {
            "model": cfg["model"],
            "temperature": temperature,
            "max_tokens": max(max_tokens, 3000),
            "messages": [{"role": "user", "content": prompt}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = _post(cfg, "/chat/completions", payload, timeout)
        if resp.status_code == 400 and json_mode and "response_format" in resp.text:
            payload.pop("response_format")
            resp = _post(cfg, "/chat/completions", payload, timeout)
        _raise_for_status(cfg, resp)
        return _openai_text(resp)

    payload = {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "temperature": min(temperature, 1.0),
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = _post(cfg, "/v1/messages", payload, timeout)
    _raise_for_status(cfg, resp)
    return _anthropic_text(resp)


def stream_generate(
    cfg: dict,
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 2048,
    timeout: int = 300,
):
    """Yield text chunks in real time from Ollama, OpenAI-compatible, or Anthropic APIs."""
    problem = config_problem(cfg)
    if problem:
        raise LLMError(problem)

    provider = cfg["provider"]

    if provider == "ollama":
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_ctx": 4096,
                "num_predict": max_tokens,
            },
        }
        with requests.post(
            f"{cfg['base_url']}/api/chat",
            json=payload,
            headers=_headers(cfg),
            stream=True,
            timeout=timeout,
        ) as resp:
            _raise_for_status(cfg, resp)
            for line in resp.iter_lines():
                if not line:
                    continue
                data = json.loads(line.decode("utf-8"))
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    yield chunk
                if data.get("done"):
                    break
        return

    if provider == "openai":
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max(max_tokens, 3000),
            "stream": True,
        }
        with requests.post(
            f"{cfg['base_url']}/chat/completions",
            json=payload,
            headers=_headers(cfg),
            stream=True,
            timeout=timeout,
        ) as resp:
            _raise_for_status(cfg, resp)
            for line in resp.iter_lines():
                if not line:
                    continue
                decoded = line.decode("utf-8")
                if not decoded.startswith("data: "):
                    continue
                raw = decoded[6:].strip()
                if raw == "[DONE]":
                    break
                try:
                    delta = (
                        json.loads(raw)["choices"][0]
                        .get("delta", {})
                        .get("content", "")
                    )
                    if delta:
                        yield delta
                except (KeyError, IndexError, json.JSONDecodeError):
                    continue
        return

    sys_msg = "\n".join(m["content"] for m in messages if m["role"] == "system")
    chat_msgs = [m for m in messages if m["role"] != "system"]
    payload = {
        "model": cfg["model"],
        "system": sys_msg,
        "messages": chat_msgs,
        "max_tokens": max_tokens,
        "temperature": min(temperature, 1.0),
        "stream": True,
    }
    with requests.post(
        f"{cfg['base_url']}/v1/messages",
        json=payload,
        headers=_headers(cfg),
        stream=True,
        timeout=timeout,
    ) as resp:
        _raise_for_status(cfg, resp)
        for line in resp.iter_lines():
            if not line:
                continue
            decoded = line.decode("utf-8")
            if not decoded.startswith("data: "):
                continue
            try:
                evt = json.loads(decoded[6:])
                if evt.get("type") == "content_block_delta":
                    text = evt.get("delta", {}).get("text", "")
                    if text:
                        yield text
            except json.JSONDecodeError:
                continue


def describe_image(
    cfg: dict, jpeg: bytes, prompt: str, model: str, timeout: int = 600
) -> str:
    b64 = base64.b64encode(jpeg).decode()
    provider = cfg["provider"]
    vcfg = dict(cfg, model=model)

    if provider == "ollama":
        payload = {
            "model": model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "options": {"temperature": 0.2, "repeat_penalty": 1.15, "num_predict": 400},
        }
        resp = _post(cfg, "/api/generate", payload, timeout)
        _raise_for_status(vcfg, resp)
        return resp.json().get("response", "")

    if provider == "openai":
        payload = {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 1500,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                    ],
                }
            ],
        }
        resp = _post(cfg, "/chat/completions", payload, timeout)
        _raise_for_status(vcfg, resp)
        return _openai_text(resp)

    payload = {
        "model": model,
        "max_tokens": 500,
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    resp = _post(cfg, "/v1/messages", payload, timeout)
    _raise_for_status(vcfg, resp)
    return _anthropic_text(resp)


def vision_ready(cfg: dict, model: str) -> tuple[bool, str]:
    problem = config_problem(cfg)
    if problem:
        return False, problem
    if not model:
        return False, "No vision model is set."
    if cfg["provider"] != "ollama":
        return True, ""
    try:
        r = requests.get(f"{cfg['base_url']}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]
    except requests.exceptions.RequestException:
        return False, f"Cannot connect to Ollama at {cfg['base_url']}."
    wanted = model.strip()
    ok = (
        wanted in models
        if ":" in wanted
        else any(m.split(":")[0] == wanted for m in models)
    )
    if not ok:
        return False, f"The vision model '{model}' is not installed."
    return True, ""


# ---------------------------------------------------------------- vision model discovery
# Providers retire/rename models often (e.g. Groq's Llama-4 Scout). Instead of trusting one hard-coded name,
# look at what the account really offers and pick a model that can read pictures.
_NOT_VISION = ("whisper", "tts", "guard", "embed", "orpheus", "allam", "gpt-oss", "safeguard", "moderation", "rerank", "compound", "transcribe")
_VISION_HINTS = [
    r"llama-4-(scout|maverick)",
    r"vision|\bvl\b|-vl-|llava|pixtral|multimodal|omni",
    r"qwen/qwen3\.\d+",       # Qwen 3.5+ are multimodal (plain qwen3-32b is text-only)
    r"gemma-?3",
    r"gpt-4o|gpt-4\.1|gpt-5|o4-mini",
]
_models_cache: dict = {}
_working_vision: dict = {}


def list_models(cfg: dict) -> list:
    """All model ids of an OpenAI-compatible provider (cached for 10 minutes; [] if unavailable)."""
    if cfg.get("provider") != "openai":
        return []
    key = cfg.get("base_url", "")
    hit = _models_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    ids: list = []
    try:
        r = requests.get(f"{cfg['base_url']}/models", headers=_headers(cfg), timeout=10)
        if r.status_code == 200:
            ids = [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
    except Exception:
        ids = []
    if ids:
        _models_cache[key] = (time.time(), ids)
    return ids


def vision_candidates(cfg: dict, exclude=()) -> list:
    ids = list_models(cfg)
    ranked: list = []
    for pat in _VISION_HINTS:
        for i in ids:
            low = i.lower()
            if re.search(pat, low) and not any(w in low for w in _NOT_VISION) and i not in ranked and i not in exclude:
                ranked.append(i)
    return ranked


def resolve_vision_model(cfg: dict, preferred: str) -> str:
    """The vision model to use now: one that worked before, else the preferred name if the account has it,
    else the best picture-capable model the account offers."""
    if cfg.get("provider") != "openai":
        return preferred
    key = cfg.get("base_url", "")
    if key in _working_vision:
        return _working_vision[key]
    ids = list_models(cfg)
    if not ids or preferred in ids:
        return preferred
    cands = vision_candidates(cfg)
    return cands[0] if cands else preferred


def remember_vision_model(cfg: dict, model: str) -> None:
    if cfg.get("provider") == "openai":
        _working_vision[cfg.get("base_url", "")] = model
