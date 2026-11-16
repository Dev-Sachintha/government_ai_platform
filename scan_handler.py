import base64
import io
import os
import requests
from PIL import Image


def _encode_image_base64(image_bytes: bytes, max_size: int = 1024) -> str:
    """Resizes image safely to avoid payload limits and returns a JPEG base64 string."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((max_size, max_size))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _extract_ocr_fallback(image_bytes: bytes) -> str:
    """Optional local OCR fallback if the configured API endpoint only accepts string content."""
    try:
        import pytesseract
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        return pytesseract.image_to_string(img).strip()
    except Exception:
        return ""


def scan_object_with_ai(
    image_bytes: bytes,
    filename: str = "scan.jpg",
    catalogue_items: list[dict] | None = None,
    api_base_url: str | None = None,
    api_key: str | None = None,
    vision_model: str | None = None,
) -> str:
    """
    Sends the scanned image to an OpenAI-compatible AI endpoint.
    Automatically retries with a plain string `messages[0].content` if the provider
    returns `messages[0].content must be a string`.
    """
    base_url = (api_base_url or os.getenv("AI_BASE_URL") or "https://api.groq.com/openai/v1").rstrip("/")
    key = api_key or os.getenv("AI_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")

    if not key:
        return "Configuration error: Missing API key (`AI_API_KEY` / `GROQ_API_KEY` / `OPENAI_API_KEY`)."

    # Pick an appropriate vision model based on provider URL if not explicitly set
    if not vision_model:
        if "groq.com" in base_url:
            vision_model = "meta-llama/llama-4-scout-17b-16e-instruct"
        elif "generativelanguage.googleapis.com" in base_url:
            vision_model = "gemini-2.5-flash"
        else:
            vision_model = "gpt-4o-mini"

    catalogue_text = ""
    if catalogue_items:
        formatted = "\n".join(
            f"- {item.get('name', 'Item')}: {item.get('description', '')}"
            for item in catalogue_items
        )
        catalogue_text = (
            f"\n\nOfficial Admin Scan Catalogue:\n{formatted}\n"
            "If the scanned object matches an item in the catalogue, prioritize returning its official information."
        )

    prompt_text = (
        "Identify the main physical object shown in this image. "
        "Provide its name, category, and a concise, factual explanation."
        f"{catalogue_text}"
    )

    b64_image = _encode_image_base64(image_bytes)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    # 1. Primary attempt: Multimodal message (image_url + text)
    # Kept in a single user message without a separate system message for Groq/OpenAI compatibility.
    multimodal_payload = {
        "model": vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                    },
                ],
            }
        ],
        "temperature": 0.3,
        "max_tokens": 512,
    }

    resp = requests.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json=multimodal_payload,
        timeout=45,
    )

    # 2. Automatic fallback: If endpoint enforces `messages[0].content must be a string`
    if resp.status_code == 400 and "must be a string" in resp.text:
        ocr_text = _extract_ocr_fallback(image_bytes)
        text_only_prompt = (
            f"A user uploaded an image named '{filename}' for object identification. "
            f"Detected text/labels in image: '{ocr_text or 'No text detected'}'. "
            f"{prompt_text}"
        )
        string_payload = {
            "model": os.getenv(
                "AI_TEXT_MODEL",
                "llama-3.3-70b-versatile" if "groq.com" in base_url else "gpt-4o-mini",
            ),
            "messages": [
                {
                    "role": "user",
                    "content": text_only_prompt,
                }
            ],
            "temperature": 0.3,
            "max_tokens": 512,
        }
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=string_payload,
            timeout=45,
        )

    if not resp.ok:
        return f"AI service error {resp.status_code}: {resp.text}"

    data = resp.json()
    return data["choices"][0]["message"]["content"]
