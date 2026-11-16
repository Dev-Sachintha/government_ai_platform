import streamlit as st
from ai_client import call_ai_chat, normalize_messages


def handle_scan_request(uploaded_file, catalogue_items: list[dict]):
    """Example Scan tab handler using Option A string normalization."""
    catalogue_summary = "\n".join(
        f"- {item.get('name', 'Item')}: {item.get('description', '')}"
        for item in catalogue_items
    )

    # Even if an upstream helper constructs a multimodal array payload,
    # `call_ai_chat` / `normalize_messages` converts `content` to a plain string.
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Identify the scanned object from file '{uploaded_file.name}'. "
                        f"Official catalogue:\n{catalogue_summary or 'Empty'}"
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,..."},
                },
            ],
        }
    ]

    return call_ai_chat(
        messages=messages,
        api_base_url=st.secrets.get("AI_BASE_URL", None),
        api_key=st.secrets.get("AI_API_KEY", None),
    )
