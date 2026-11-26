"""Self-hosted Sinhala TTS API powered by Piper's Dilu voice.

The model is downloaded from Hugging Face at startup and cached in the container.
Set SINHALA_TTS_API_KEY before starting the service.
"""
from __future__ import annotations

import hmac
import io
import json
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
import onnxruntime as ort
from piper import PiperVoice
from piper.config import PiperConfig, SynthesisConfig

MODEL_REPO = os.getenv("SINHALA_TTS_MODEL_REPO", "chaturadilan/dilu-tts")
MODEL_FILE = "onnx/si_LK-dilu-medium.onnx"
CONFIG_FILE = "onnx/si_LK-dilu-medium.onnx.json"
MAX_TEXT_LENGTH = int(os.getenv("SINHALA_TTS_MAX_CHARS", "2500"))
# Keep ONNX Runtime thread pools small on CPU-limited Render instances.
ORT_THREADS = max(1, min(2, int(os.getenv("PIPER_ORT_THREADS", "1"))))
ORT_INTER_OP_THREADS = max(1, min(1, int(os.getenv("PIPER_ORT_INTER_OP_THREADS", "1"))))
# The default CPU arena and memory-pattern planner can retain sizeable
# allocations after inference. Disable them by default to reduce peak/retained
# memory on small containers. Set either variable to "false" to re-enable.
ORT_DISABLE_CPU_MEM_ARENA = os.getenv("PIPER_ORT_DISABLE_CPU_MEM_ARENA", "true").lower() in {
    "1", "true", "yes", "on"
}
ORT_DISABLE_MEM_PATTERN = os.getenv("PIPER_ORT_DISABLE_MEM_PATTERN", "true").lower() in {
    "1", "true", "yes", "on"
}
# BASIC uses less optimization workspace than ALL; it may trade some speed
# for a smaller memory footprint. Override with "all" on larger instances.
ORT_GRAPH_OPT_LEVEL = os.getenv("PIPER_ORT_GRAPH_OPT_LEVEL", "basic").lower()
_GRAPH_OPT_LEVELS = {
    "disable": ort.GraphOptimizationLevel.ORT_DISABLE_ALL,
    "basic": ort.GraphOptimizationLevel.ORT_ENABLE_BASIC,
    "extended": ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED,
    "all": ort.GraphOptimizationLevel.ORT_ENABLE_ALL,
}
if ORT_GRAPH_OPT_LEVEL not in _GRAPH_OPT_LEVELS:
    raise RuntimeError("PIPER_ORT_GRAPH_OPT_LEVEL must be disable, basic, extended, or all.")
LENGTH_SCALE = float(os.getenv("SINHALA_TTS_LENGTH_SCALE", "1.0"))
if not 0.5 <= LENGTH_SCALE <= 2.0:
    raise RuntimeError("SINHALA_TTS_LENGTH_SCALE must be between 0.5 and 2.0.")
SYNTHESIS_CONFIG = SynthesisConfig(length_scale=LENGTH_SCALE)

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
    with open(config_path, "r", encoding="utf-8") as config_file:
        voice_config = PiperConfig.from_dict(json.load(config_file))

    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = ORT_THREADS
    session_options.inter_op_num_threads = ORT_INTER_OP_THREADS
    session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session_options.graph_optimization_level = _GRAPH_OPT_LEVELS[ORT_GRAPH_OPT_LEVEL]
    session_options.enable_cpu_mem_arena = not ORT_DISABLE_CPU_MEM_ARENA
    session_options.enable_mem_pattern = not ORT_DISABLE_MEM_PATTERN

    _voice = PiperVoice(
        config=voice_config,
        session=ort.InferenceSession(
            model_path,
            sess_options=session_options,
            providers=["CPUExecutionProvider"],
        ),
    )
    logger.info(
        "Sinhala voice model loaded rss_peak_mb=%s intra_threads=%s inter_threads=%s "
        "graph_optimization=%s cpu_mem_arena=%s mem_pattern=%s length_scale=%s",
        _memory_rss_mb(),
        ORT_THREADS,
        ORT_INTER_OP_THREADS,
        ORT_GRAPH_OPT_LEVEL,
        not ORT_DISABLE_CPU_MEM_ARENA,
        not ORT_DISABLE_MEM_PATTERN,
        LENGTH_SCALE,
    )
    yield
    _voice = None


app = FastAPI(
    title="Government AI Platform — Sinhala TTS",
    version="1.1.0",
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
        "synthesis_start chars=%s rss_peak_mb=%s",
        len(text), _memory_rss_mb(),
    )
    try:
        # A single shared Piper/ONNX session must never run two inferences at once.
        with _voice_lock:
            with wave.open(output, "wb") as wav_file:
                _voice.synthesize_wav(text, wav_file, syn_config=SYNTHESIS_CONFIG)
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
