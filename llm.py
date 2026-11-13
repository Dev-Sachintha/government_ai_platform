import json


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

    # Anthropic SSE streaming
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
