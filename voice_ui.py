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
import llm

from avatar_js import STT_JS, TOOLS_JS

_BASE_CSS = """
html, body { margin:0; padding:0; background:transparent; font-family: Inter, system-ui, -apple-system, 'Segoe UI', sans-serif; color:#444; }
button { font-family: inherit; }
"""

_PILL = ("background:#fff; border:1px solid rgba(128,128,128,0.45); border-radius:999px; "
         "padding:5px 13px; cursor:pointer; font-size:12px; color:#333;")


_TTS_LANGUAGES = {
    "Original answer (auto)": "auto",
    "English": "en-US", "Sinhala (සිංහල)": "si-LK", "Tamil (தமிழ்)": "ta-LK",
    "Hindi (हिन्दी)": "hi-IN", "Arabic (العربية)": "ar-SA", "Chinese (中文)": "zh-CN",
    "Japanese (日本語)": "ja-JP", "Korean (한국어)": "ko-KR", "French": "fr-FR",
    "German": "de-DE", "Spanish": "es-ES", "Russian": "ru-RU", "Bengali": "bn-BD",
    "Urdu": "ur-PK", "Malay": "ms-MY", "Indonesian": "id-ID", "Thai": "th-TH",
    "Italian": "it-IT", "Portuguese": "pt-PT",
}


def _speech_frame(text: str, language: str, msg_index: int):
    """Speech starts only after the user directly presses Listen inside the iframe."""
    attr = _html.escape(text or "", quote=True)
    page = (
        "<!doctype html><html><head><meta charset='utf-8'><style>" + _BASE_CSS +
        "button{background:#fff;border:1px solid rgba(128,128,128,.45);border-radius:999px;"
        "padding:5px 13px;cursor:pointer;font-size:12px;color:#333}</style></head><body>"
        f"<div id='speech-{int(msg_index)}' data-text='{attr}' data-lang='{_html.escape(language)}' "
        "style='display:flex;align-items:center;gap:7px;padding:4px 2px;'>"
        "<button id='copy'>📋 Copy</button><button id='speak'>🔊 Listen</button><button id='stop'>⏹ Stop</button>"
        "<span id='status' role='status' aria-live='polite' style='font-size:11px;opacity:.8'></span>"
        "</div><script>"
        "(function(){const root=document.querySelector('[id^=speech-]');"
        "const host=(function(){try{return window.parent&&window.parent.speechSynthesis?window.parent:window;}catch(e){return window;}})();"
        "const text=root.dataset.text||'';"
        "function detect(t){if(/[\\u0D80-\\u0DFF]/.test(t))return 'si-LK';if(/[\\u0B80-\\u0BFF]/.test(t))return 'ta-LK';"
        "if(/[\\u0600-\\u06FF]/.test(t))return 'ar-SA';if(/[\\u0900-\\u097F]/.test(t))return 'hi-IN';return 'en-US';}"
        "const lang=root.dataset.lang==='auto'?detect(text):root.dataset.lang;"
        "function status(s){root.querySelector('#status').textContent=s;}"
        "function stop(){try{host.speechSynthesis.cancel();}catch(e){}status('Stopped.');}"
        "root.querySelector('#stop').addEventListener('click',stop);"
        "root.querySelector('#copy').addEventListener('click',function(){"
        "const clip=(host.navigator&&host.navigator.clipboard)?host.navigator.clipboard: navigator.clipboard;"
        "if(clip&&clip.writeText){clip.writeText(text).then(()=>status('Copied.'),()=>status('Copy blocked by browser permissions.'));}"
        "else{status('Clipboard is not supported in this browser.');}});"
        "root.querySelector('#speak').addEventListener('click',function(){"
        "if(!host.speechSynthesis||!host.SpeechSynthesisUtterance){status('Speech is not supported. Use Chrome or Edge.');return;}"
        "stop();const clean=text.replace(/https?:\\/\\/\\S+/g,' link ').replace(/[\\#*\\x60>|~]/g,' ').trim();"
        "if(!clean){status('Nothing to read.');return;}const chunks=clean.match(/.{1,170}(?:\\s|$)/g)||[clean];"
        "let i=0;const voices=host.speechSynthesis.getVoices();const base=lang.split('-')[0].toLowerCase();"
        "const voice=voices.find(v=>v.lang.toLowerCase()===lang.toLowerCase())||voices.find(v=>v.lang.toLowerCase().startsWith(base));"
        "function next(){if(i>=chunks.length){status('Finished.');return;}const u=new host.SpeechSynthesisUtterance(chunks[i++]);"
        "u.lang=lang;u.rate=1;if(voice)u.voice=voice;u.onstart=()=>status('Speaking '+lang+'…');u.onend=next;"
        "u.onerror=e=>status('Speech error: '+e.error);host.speechSynthesis.speak(u);}next();"
        "});})();</script></body></html>"
    )
    components.html(page, height=42)


def render_message_tools(text: str, msg_index: int, llm_cfg: dict | None = None):
    """Translate an answer with the configured AI provider, then read it aloud."""
    st.caption("Answer audio")
    cols = st.columns([2.4, 1.2])
    with cols[0]:
        language_name = st.selectbox(
            "Listening language", list(_TTS_LANGUAGES.keys()), index=0,
            key=f"voice_language_{msg_index}", label_visibility="collapsed",
        )
    lang_code = _TTS_LANGUAGES[language_name]
    original = text or ""
    speech_text = original
    if lang_code != "auto":
        import hashlib
        cache_key = "voice_translation_" + hashlib.sha256(
            (lang_code + "\\0" + original).encode("utf-8")
        ).hexdigest()[:24]
        with cols[1]:
            translate_clicked = st.button("🌐 Translate", key=f"voice_translate_{msg_index}")
        if translate_clicked:
            if not llm_cfg:
                st.error("Translation needs the configured AI provider. Open the main chat to use this feature.")
            else:
                try:
                    prompt = (
                        "Translate the following assistant answer into " + language_name + " (language code " + lang_code + "). "
                        "Preserve the complete meaning, headings, lists, tables, numbers, URLs, names and formatting. "
                        "Do not add explanations. Keep source-code blocks unchanged.\n\n" + original
                    )
                    with st.spinner("Translating answer…"):
                        translated = llm.generate(
                            llm_cfg, prompt, json_mode=False, temperature=0.1,
                            max_tokens=3000, timeout=120,
                        ).strip()
                    if not translated:
                        raise ValueError("The translation service returned an empty result.")
                    st.session_state[cache_key] = translated
                except Exception as exc:
                    st.error(f"Translation failed: {exc}")
        speech_text = st.session_state.get(cache_key, "")
        if not speech_text:
            st.caption("Select a language and press Translate before listening.")
    else:
        with cols[1]:
            st.write("")
    if speech_text:
        _speech_frame(speech_text, lang_code, msg_index)


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

    return {"bg": uri("background.jpg", 1280, 72), "avatar": uri("assistant_profile.jpg", 720, 72) or uri("avatar.jpg", 720, 72)}


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
