"""Self-hosted Sinhala TTS API powered by Piper's Dilu voice.

The model is downloaded from Hugging Face at startup and cached in the container.
Set SINHALA_TTS_API_KEY before starting the service.
"""
from __future__ import annotations

import hmac
import io
import logging
import os
import threading
import time
import wave
import resource
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

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sinhala_tts")


def _memory_rss_mb() -> float | None:
    """Return peak resident memory in MiB on Linux; never fail a request for diagnostics."""
    try:
        # Linux reports ru_maxrss in KiB.
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        return None


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
    logger.info("Loading Sinhala voice model repo=%s model_file=%s", MODEL_REPO, MODEL_FILE)
    _voice = PiperVoice.load(model_path, config_path=config_path)
    logger.info("Sinhala voice model loaded successfully rss_peak_mb=%s", _memory_rss_mb())
    yield
    _voice = None


app = FastAPI(
    title="Government AI Platform — Sinhala TTS",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_diagnostics(request, call_next):
    """Log request start/end with Cloudflare Ray ID to trace edge 502s."""
    ray_id = request.headers.get("cf-ray", "not-provided")
    started = time.monotonic()
    logger.info(
        "request_start method=%s path=%s cf_ray=%s",
        request.method, request.url.path, ray_id,
    )
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request_crashed method=%s path=%s cf_ray=%s elapsed_s=%.2f rss_peak_mb=%s",
            request.method, request.url.path, ray_id,
            time.monotonic() - started, _memory_rss_mb(),
        )
        raise

    logger.info(
        "request_complete method=%s path=%s status=%s cf_ray=%s elapsed_s=%.2f "
        "response_bytes=%s rss_peak_mb=%s",
        request.method, request.url.path, response.status_code, ray_id,
        time.monotonic() - started, response.headers.get("content-length", "streamed"),
        _memory_rss_mb(),
    )
    return response


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


@app.api_route("/", methods=["GET", "HEAD"])
def root():
    """Simple root response for platform and browser health probes."""
    return {"status": "ok", "service": "sinhala-tts"}


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
    synthesis_started = time.monotonic()
    logger.info(
        "synthesis_start chars=%s cf_ray=%s rss_peak_mb=%s",
        len(text), "see_request_log", _memory_rss_mb(),
    )
    try:
        # Piper's ONNX Runtime session is shared; serialize inference for safety on
        # small CPU instances and avoid concurrent model-session contention.
        with _voice_lock:
            with wave.open(output, "wb") as wav_file:
                _voice.synthesize_wav(text, wav_file)
    except Exception as exc:
        logger.exception(
            "synthesis_failed chars=%s elapsed_s=%.2f rss_peak_mb=%s",
            len(text), time.monotonic() - synthesis_started, _memory_rss_mb(),
        )
        raise HTTPException(status_code=500, detail="Speech synthesis failed.") from exc

    audio_bytes = output.getvalue()
    logger.info(
        "synthesis_complete chars=%s audio_bytes=%s elapsed_s=%.2f rss_peak_mb=%s",
        len(text), len(audio_bytes), time.monotonic() - synthesis_started, _memory_rss_mb(),
    )

    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={
            "Content-Disposition": 'inline; filename="sinhala-speech.wav"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
