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
import time
import wave

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
    """Render Listen controls wired to the animated talking avatar in the parent page."""
    attr = _html.escape(text or "", quote=True)
    lang = _html.escape(language or "auto", quote=True)
    page = (
        "<!doctype html><html><head><meta charset='utf-8'><style>" + _BASE_CSS +
        "button{background:#fff;border:1px solid rgba(128,128,128,.45);border-radius:999px;"
        "padding:5px 13px;cursor:pointer;font-size:12px;color:#333}"
        ".gov-status{font-size:11px;opacity:.8}</style></head><body>"
        f"<div id='speech-{int(msg_index)}' data-gov-tools data-text='{attr}' data-lang='{lang}' "
        "style='display:flex;align-items:center;gap:7px;padding:4px 2px;'>"
        "<button type='button' data-action='copy'>📋 Copy</button>"
        "<button type='button' data-action='listen'>🔊 Listen</button>"
        "<button type='button' data-action='stop'>⏹ Stop</button>"
        "<span class='gov-status' role='status' aria-live='polite'></span>"
        "</div><script>" + TOOLS_JS + "</script></body></html>"
    )
    components.html(page, height=46)


def _split_sinhala_tts_text(text: str, max_chars: int = 120) -> list[str]:
    """Split long text at paragraph/sentence/word boundaries under the API limit."""
    remaining = (text or "").strip()
    chunks = []
    while len(remaining) > max_chars:
        window = remaining[: max_chars + 1]
        # Prefer paragraph or sentence boundaries, then fall back to a word boundary.
        candidates = [
            window.rfind("\n"),
            window.rfind(". "),
            window.rfind("! "),
            window.rfind("? "),
            window.rfind("।"),
        ]
        cut = max(candidates)
        if cut >= max_chars // 2:
            if remaining[cut] in ".!?।":
                cut += 1
        else:
            cut = window.rfind(" ")
            if cut < max_chars // 2:
                cut = max_chars
        part = remaining[:cut].strip()
        if not part:
            cut = max_chars
            part = remaining[:cut].strip()
        chunks.append(part)
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _combine_sinhala_wav_parts(audio_parts: list[bytes]) -> bytes:
    """Join compatible PCM WAV responses into one playable WAV file."""
    if not audio_parts:
        raise ValueError("The Sinhala TTS server returned no audio.")
    if len(audio_parts) == 1:
        return audio_parts[0]

    output = io.BytesIO()
    reference = None
    audio_frames = []
    for index, audio_bytes in enumerate(audio_parts, start=1):
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
                params = wav_file.getparams()
                if reference is None:
                    reference = params
                elif (
                    params.nchannels != reference.nchannels
                    or params.sampwidth != reference.sampwidth
                    or params.framerate != reference.framerate
                    or params.comptype != reference.comptype
                ):
                    raise ValueError("Sinhala audio parts use incompatible WAV formats.")
                audio_frames.append(wav_file.readframes(wav_file.getnframes()))
        except (wave.Error, EOFError) as exc:
            raise ValueError(f"Audio part {index} is not a valid WAV file: {exc}") from exc

    with wave.open(output, "wb") as combined:
        combined.setparams(reference)
        silence_frames = max(1, int(reference.framerate * 0.12))
        silence = b"\x00" * silence_frames * reference.nchannels * reference.sampwidth
        for index, frames in enumerate(audio_frames):
            if index:
                combined.writeframesraw(silence)
            combined.writeframesraw(frames)
    return output.getvalue()


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
                    # Optional independent provider for translation only.
                    fallback_secrets = {
                        "LLM_PROVIDER": st.secrets.get("TRANSLATION_LLM_PROVIDER", os.getenv("TRANSLATION_LLM_PROVIDER", "")),
                        "LLM_BASE_URL": st.secrets.get("TRANSLATION_LLM_BASE_URL", os.getenv("TRANSLATION_LLM_BASE_URL", "")),
                        "LLM_API_KEY": st.secrets.get("TRANSLATION_LLM_API_KEY", os.getenv("TRANSLATION_LLM_API_KEY", "")),
                        "LLM_MODEL": st.secrets.get("TRANSLATION_LLM_MODEL", os.getenv("TRANSLATION_LLM_MODEL", "")),
                    }
                    fallback_cfg = None
                    if all(str(v or "").strip() for v in fallback_secrets.values()):
                        fallback_cfg = llm.load_config(fallback_secrets)
                    with st.spinner("Translating answer (automatic splitting and retry enabled)…"):
                        translated = llm.generate_translation(
                            llm_cfg, original, language_name, fallback_cfg=fallback_cfg
                        )
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
        # Sinhala is synthesized on the server; no browser voice or third-party
        # browser TTS endpoint is required. The API key remains on the Streamlit server.
        is_sinhala = lang_code == "si-LK" or (
            lang_code == "auto" and any("\u0D80" <= ch <= "\u0DFF" for ch in speech_text)
        )
        if is_sinhala:
            import hashlib
            import requests

            audio_key = "sinhala_tts_audio_" + hashlib.sha256(
                speech_text.encode("utf-8")
            ).hexdigest()[:24]
            service_url = str(
                st.secrets.get("SINHALA_TTS_URL", os.getenv("SINHALA_TTS_URL", ""))
            ).strip().rstrip("/")
            api_key = str(
                st.secrets.get("SINHALA_TTS_API_KEY", os.getenv("SINHALA_TTS_API_KEY", ""))
            ).strip()

            tts_chunks = _split_sinhala_tts_text(speech_text)
            if len(tts_chunks) > 1:
                st.caption(
                    f"Long Sinhala text will be split automatically into {len(tts_chunks)} audio parts."
                )
            if st.button(
                "🔊 Listen (Sinhala)",
                key=f"sinhala_listen_{msg_index}",
                disabled=not tts_chunks,
            ):
                if not service_url or not api_key:
                    st.error(
                        "Sinhala TTS is not configured. Set SINHALA_TTS_URL and "
                        "SINHALA_TTS_API_KEY in Streamlit Secrets."
                    )
                else:
                    try:
                        audio_parts = []
                        progress = st.progress(
                            0,
                            text=f"Connecting to Sinhala voice service (0/{len(tts_chunks)})…",
                        )
                        for part_number, chunk in enumerate(tts_chunks, start=1):
                            progress.progress(
                                (part_number - 1) / len(tts_chunks),
                                text=(
                                    f"Synthesizing Sinhala audio part "
                                    f"{part_number}/{len(tts_chunks)} — please wait…"
                                ),
                            )
                            # Render free instances can be temporarily unavailable while
                            # waking from idle or being replaced. Retry only transient
                            # gateway/connection failures; do not repeat normal 4xx errors.
                            response = None
                            for attempt in range(4):
                                try:
                                    candidate = requests.post(
                                        service_url + "/v1/tts",
                                        json={"text": chunk},
                                        headers={"Authorization": f"Bearer {api_key}"},
                                        # Model inference is CPU-bound and can take several minutes.
                                        timeout=(30, 600),
                                    )
                                except requests.ConnectionError:
                                    if attempt == 3:
                                        raise
                                    wait_seconds = 5 * (attempt + 1)
                                    st.warning(
                                        f"Sinhala TTS service is reconnecting. "
                                        f"Retrying in {wait_seconds} seconds "
                                        f"({attempt + 1}/3)…"
                                    )
                                    time.sleep(wait_seconds)
                                    continue

                                if candidate.status_code in (502, 503, 504) and attempt < 3:
                                    wait_seconds = 5 * (attempt + 1)
                                    candidate.close()
                                    st.warning(
                                        f"Sinhala TTS service is temporarily unavailable "
                                        f"(HTTP {candidate.status_code}). Retrying in "
                                        f"{wait_seconds} seconds ({attempt + 1}/3)…"
                                    )
                                    time.sleep(wait_seconds)
                                    continue

                                response = candidate
                                break

                            if response is None:
                                raise requests.ConnectionError(
                                    "Sinhala TTS service could not be reached after retries."
                                )
                            response.raise_for_status()
                            audio_bytes = response.content
                            if not audio_bytes.startswith(b"RIFF") or b"WAVE" not in audio_bytes[:16]:
                                raise ValueError(
                                    f"Audio part {part_number} is not a valid WAV file."
                                )
                            # Reject empty/corrupt WAV payloads before combining them.
                            try:
                                with wave.open(io.BytesIO(audio_bytes), "rb") as wav_check:
                                    if wav_check.getnframes() <= 0 or wav_check.getframerate() <= 0:
                                        raise ValueError("The WAV file contains no playable audio frames.")
                            except (wave.Error, EOFError) as exc:
                                raise ValueError(
                                    f"Audio part {part_number} contains an unreadable WAV file: {exc}"
                                ) from exc
                            audio_parts.append(audio_bytes)
                            progress.progress(
                                part_number / len(tts_chunks),
                                text=(
                                    f"Generated Sinhala audio part "
                                    f"{part_number}/{len(tts_chunks)} successfully."
                                ),
                            )
                        progress.progress(1.0, text="Combining Sinhala audio…")
                        combined_audio = _combine_sinhala_wav_parts(audio_parts)
                        if not combined_audio:
                            raise ValueError("The Sinhala TTS service returned an empty audio file.")
                        st.session_state[audio_key] = combined_audio
                        progress.empty()
                        st.success(f"Sinhala audio is ready ({len(tts_chunks)} part(s)). Press Play below.")
                    except requests.Timeout:
                        st.error(
                            "Sinhala TTS is taking longer than 10 minutes for one audio part. "
                            "The request timed out; please try again or check the Render TTS service logs."
                        )
                    except requests.HTTPError as exc:
                        status_code = getattr(getattr(exc, "response", None), "status_code", "unknown")
                        detail = ""
                        try:
                            detail = exc.response.text[:500]
                        except Exception:
                            pass
                        st.error(
                            f"Sinhala TTS service returned HTTP {status_code}. "
                            f"{detail or str(exc)}"
                        )
                    except requests.RequestException as exc:
                        st.error(
                            "Could not connect to the Sinhala TTS service. "
                            f"Check that the Render service is running and SINHALA_TTS_URL is correct. Details: {exc}"
                        )
                    except Exception as exc:
                        st.error(f"Sinhala audio generation failed: {exc}")

            audio_data = st.session_state.get(audio_key)
            if audio_data:
                st.audio(audio_data, format="audio/wav")
        else:
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

    return {"bg": uri("background.jpg", 1280, 72), "avatar": uri("avatar.jpg", 720, 72) or uri("assistant_profile.jpg", 720, 72)}


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
    components.html("<!doctype html><html><body><script>" + script + "</script></body></html>", height=1)
