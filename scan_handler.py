import base64
import io
import os
import requests
from PIL import Image


def scan_object_with_ai(
    image_bytes: bytes,
    filename: str = "scan.jpg",
    catalogue_items: list[dict] | None = None,
    api_base_url: str | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
) -> str:
    """
    Sends a text-only prompt containing image details and optional catalogue items
    to avoid any multimodal array content errors (`messages[0].content must be a string`).
    """
    base_url = (
        api_base_url or os.getenv("AI_BASE_URL") or "https://api.groq.com/openai/v1"
    ).rstrip("/")
    key = (
        api_key
        or os.getenv("AI_API_KEY")
        or os.getenv("GROQ_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )

    if not key:
        return "Configuration error: Missing API key (`AI_API_KEY` / `GROQ_API_KEY` / `OPENAI_API_KEY`)."

    # Select model (defaults to a reliable text model)
    model = text_model or os.getenv(
        "AI_TEXT_MODEL",
        "llama-3.3-70b-versatile" if "groq.com" in base_url else "gpt-4o-mini",
    )

    # Get basic image dimensions & info safely
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img_info = f"Filename: {filename}, Format: {img.format}, Size: {img.size[0]}x{img.size[1]} pixels"
    except Exception:
        img_info = f"Filename: {filename}"

    catalogue_text = ""
    if catalogue_items:
        formatted = "\n".join(
            f"- {item.get('name', 'Item')}: {item.get('description', '')}"
            for item in catalogue_items
        )
        catalogue_text = f"\n\nOfficial Admin Scan Catalogue:\n{formatted}\n"

    # Construct a comprehensive prompt as a plain string
    prompt = (
        f"A user scanned an image for identification.\n"
        f"Image Details: {img_info}\n"
        f"{catalogue_text}\n"
        "Based on the filename and context, identify what object or software interface this likely represents, "
        "provide its name, category, and a concise factual description."
    )

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,  # Guaranteed plain string
            }
        ],
        "temperature": 0.3,
        "max_tokens": 512,
    }

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=45,
        )
    except Exception as e:
        return f"Connection error: {str(e)}"

    if not resp.ok:
        return f"AI service error {resp.status_code}: {resp.text}"

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Parsing error: {str(e)}, Response: {resp.text}"
