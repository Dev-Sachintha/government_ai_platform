"""Voice controls rendered as small iframes (streamlit components.html).

Why iframes: their <script> always runs and the buttons exist in the iframe's own
document, so clicks work on every Streamlit version/host. The scripts reach the main page
through window.parent (same origin) for the speech engine, the avatar pop-up and the chat box.
"""
import base64
import html as _html
import io
import json
import os

import streamlit as st
import streamlit.components.v1 as components

from avatar_js import STT_JS, TOOLS_JS

_BASE_CSS = """
html, body { margin:0; padding:0; background:transparent; font-family: Inter, system-ui, -apple-system, 'Segoe UI', sans-serif; color:#444; }
button { font-family: inherit; }
"""

_PILL = ("background:#fff; border:1px solid rgba(128,128,128,0.45); border-radius:999px; "
         "padding:5px 13px; cursor:pointer; font-size:12px; color:#333;")


def render_message_tools(text: str, msg_index: int):
    """📋 Copy / 🔊 Listen / ⏹️ Stop under an AI answer (Listen opens the talking avatar)."""
    attr = _html.escape(text or "", quote=True)
    page = (
        "<!doctype html><html><head><meta charset='utf-8'><style>" + _BASE_CSS + "</style></head><body>"
        f"<div data-gov-tools='1' data-index='{int(msg_index)}' data-text=\"{attr}\" "
        "style='display:flex;align-items:center;flex-wrap:wrap;gap:6px;padding:4px 2px;'>"
        f"<button type='button' data-action='copy' style=\"{_PILL}\">📋 Copy</button>"
        f"<button type='button' data-action='listen' style=\"{_PILL}\">🔊 Listen</button>"
        f"<button type='button' data-action='stop' style=\"{_PILL}\">⏹️ Stop</button>"
        "<span class='gov-status' role='status' aria-live='polite' style='font-size:11px;opacity:.8;'></span>"
        "</div><script>" + TOOLS_JS + "</script></body></html>"
    )
    components.html(page, height=46)


_LANGS = [
    ("en-US", "English (US)"), ("si-LK", "Sinhala (සිංහල)"), ("ta-IN", "Tamil (தமிழ்)"),
    ("hi-IN", "Hindi (हिन्दी)"), ("ar-SA", "Arabic (العربية)"), ("zh-CN", "Chinese (中文)"),
    ("ja-JP", "Japanese (日本語)"), ("ko-KR", "Korean (한국어)"), ("fr-FR", "French"),
    ("de-DE", "German"), ("es-ES", "Spanish"), ("ru-RU", "Russian"),
]


def render_stt_bar(disabled: bool = False):
    """🎤 Speak dictation bar: speech -> text in the chat box -> sent automatically."""
    dis = "disabled" if disabled else ""
    options = "".join(f"<option value='{v}'>{_html.escape(n)}</option>" for v, n in _LANGS)
    page = (
        "<!doctype html><html><head><meta charset='utf-8'><style>" + _BASE_CSS + "</style></head><body>"
        "<div style='display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:6px 12px;"
        "background:#f1f2f9;border:1px solid rgba(128,128,128,0.25);border-radius:999px;'>"
        "<span style='font-size:12px;font-weight:600;'>🎙️ Dictate:</span>"
        f"<select id='sttLang' {dis} style='padding:4px 6px;border-radius:8px;border:1px solid #999;font-size:12px;'>{options}</select>"
        f"<button id='sttBtn' type='button' {dis} style='background:linear-gradient(120deg,#6366f1,#8b5cf6);color:#fff;"
        "border:none;padding:6px 14px;border-radius:999px;cursor:pointer;font-size:12px;font-weight:600;'>🎤 Speak</button>"
        f"<button id='sttStopBtn' type='button' {dis} style=\"{_PILL}\">⏹ Stop</button>"
        "<span id='sttStatus' role='status' aria-live='polite' style='font-size:12px;opacity:.8;'>Click Speak to dictate</span>"
        "</div><script>" + STT_JS + "</script></body></html>"
    )
    components.html(page, height=54)


_HERE = os.path.dirname(os.path.abspath(__file__))


@st.cache_resource(show_spinner=False)
def _asset_data_uris():
    """Pictures as data: URIs, so nothing depends on Streamlit's static-file serving."""
    from PIL import Image

    def uri(name, width, quality):
        path = os.path.join(_HERE, "static", name)
        if not os.path.exists(path):
            return ""
        im = Image.open(path).convert("RGB")
        if im.width > width:
            im = im.resize((width, int(im.height * width / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    return {"bg": uri("background.jpg", 1280, 72), "avatar": uri("avatar.jpg", 720, 72)}


def render_assets():
    """Hidden component: hands the pictures to the main page (talking avatar + banner background)."""
    uris = _asset_data_uris()
    script = (
        "(function(){var P;try{P=(window.parent&&window.parent.document)?window.parent:window;}catch(e){P=window;}"
        "var A=" + json.dumps(uris["avatar"]) + ",B=" + json.dumps(uris["bg"]) + ";"
        "if(A)P.__govAvatarSrc=A;"
        "if(B){var d=P.document,s=d.getElementById('gov-assets-style');"
        "if(!s){s=d.createElement('style');s.id='gov-assets-style';d.head.appendChild(s);}"
        "s.textContent=':root{--gov-bg:url(\\''+B+'\\')}';}})();"
    )
    components.html("<!doctype html><html><body><script>" + script + "</script></body></html>", height=0)
