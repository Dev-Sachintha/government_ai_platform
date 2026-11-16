import os
import requests


def normalize_messages(messages: list[dict]) -> list[dict]:
    """
    Ensures every message in `messages` has a plain string `content` field.
    Prevents 400 `invalid_request_error`: `messages[0].content must be a string`
    on text-only or strict OpenAI-compatible endpoints.
    """
    cleaned: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            text_parts = []
            has_image = False

            for part in content:
                if isinstance(part, str):
                    text_parts.append(part)
                elif isinstance(part, dict):
                    part_type = part.get("type", "")
                    if part_type == "text" and part.get("text"):
                        text_parts.append(str(part["text"]))
                    elif part_type == "image_url":
                        has_image = True

            combined = "\n".join(t for t in text_parts if t).strip()
            if has_image:
                combined = (
                    f"{combined}\n[Attached image provided by user for identification]"
                    if combined
                    else "[Attached image provided by user for identification]"
                )
            content = combined
        elif content is None:
            content = ""
        elif not isinstance(content, str):
            content = str(content)

        cleaned.append({"role": role, "content": content})

    return cleaned


def call_ai_chat(
    messages: list[dict],
    model: str | None = None,
    api_base_url: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 1024,
) -> str:
    """
    Sends a chat completion request with guaranteed string-only `content` fields.
    """
    base_url = (
        api_base_url
        or os.getenv("AI_BASE_URL")
        or "https://api.groq.com/openai/v1"
    ).rstrip("/")
    key = (
        api_key
        or os.getenv("AI_API_KEY")
        or os.getenv("GROQ_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )

    if not key:
        return "Configuration error: Missing API key."

    selected_model = model or os.getenv(
        "AI_TEXT_MODEL",
        "llama-3.3-70b-versatile" if "groq.com" in base_url else "gpt-4o-mini",
    )

    payload = {
        "model": selected_model,
        "messages": normalize_messages(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=45,
        )
    except requests.RequestException as exc:
        return f"AI connection error: {exc}"

    if not resp.ok:
        return f"AI service error {resp.status_code}: {resp.text}"

    data = resp.json()
    return data["choices"][0]["message"]["content"]
