"""Self-hosted Sinhala TTS API powered by Piper's Dilu voice.

The model is downloaded from Hugging Face at startup and cached in the container.
Set SINHALA_TTS_API_KEY before starting the service.
"""
from __future__ import annotations

import hmac
import io
import os
import threading
import wave
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import Response
from huggingface_hub import hf_hub_download
from pydantic import BaseModel, Field
from piper import PiperVoice

MODEL_REPO = os.getenv("SINHALA_TTS_MODEL_REPO", "chaturadilan/dilu-tts")
MODEL_FILE = "onnx/si_LK-dilu-medium.onnx"
CONFIG_FILE = "onnx/si_LK-dilu-medium.onnx.json"
MAX_TEXT_LENGTH = int(os.getenv("SINHALA_TTS_MAX_CHARS", "2500"))

_voice: PiperVoice | None = None
_voice_lock = threading.Lock()


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _voice
    api_key = os.getenv("SINHALA_TTS_API_KEY", "").strip()
    if len(api_key) < 32:
        raise RuntimeError("SINHALA_TTS_API_KEY must be set to a secret of at least 32 characters.")
    cache_dir = Path(os.getenv("HF_HOME", "/tmp/huggingface"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    model_path = hf_hub_download(
        repo_id=MODEL_REPO,
        filename=MODEL_FILE,
        cache_dir=str(cache_dir),
    )
    config_path = hf_hub_download(
        repo_id=MODEL_REPO,
        filename=CONFIG_FILE,
        cache_dir=str(cache_dir),
    )
    _voice = PiperVoice.load(model_path, config_path=config_path)
    yield
    _voice = None


app = FastAPI(
    title="Government AI Platform — Sinhala TTS",
    version="1.0.0",
    lifespan=lifespan,
)


def _authorize(authorization: str | None, x_api_key: str | None) -> None:
    expected = os.getenv("SINHALA_TTS_API_KEY", "").strip()
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    elif x_api_key:
        supplied = x_api_key.strip()
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )


@app.get("/health")
def health():
    return {
        "status": "ok" if _voice is not None else "loading",
        "model": MODEL_REPO,
        "voice": "si_LK-dilu-medium",
        "loaded": _voice is not None,
    }


@app.post("/v1/tts")
def synthesize(
    payload: TTSRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _authorize(authorization, x_api_key)
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Text cannot be empty.")
    if not any("\u0D80" <= char <= "\u0DFF" for char in text):
        raise HTTPException(status_code=422, detail="Input must contain Sinhala characters.")
    if _voice is None:
        raise HTTPException(status_code=503, detail="Sinhala voice model is not loaded.")

    output = io.BytesIO()
    try:
        # Piper's ONNX Runtime session is shared; serialize inference for safety on
        # small CPU instances and avoid concurrent model-session contention.
        with _voice_lock:
            with wave.open(output, "wb") as wav_file:
                _voice.synthesize_wav(text, wav_file)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Speech synthesis failed.") from exc

    return Response(
        content=output.getvalue(),
        media_type="audio/wav",
        headers={
            "Content-Disposition": 'inline; filename="sinhala-speech.wav"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
