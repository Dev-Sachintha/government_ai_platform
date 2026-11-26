# Self-hosted Sinhala TTS API

FastAPI + Piper server using the MIT-licensed `chaturadilan/dilu-tts` Sinhala voice.

## Endpoints

- `GET /health` — readiness information (no key required).
- `POST /v1/tts` — accepts `{"text":"ආයුබෝවන්"}`, requires
  `Authorization: Bearer <SINHALA_TTS_API_KEY>`, returns a WAV audio response.

The API key must be at least 32 characters. Never put it in source control or browser
JavaScript. The Streamlit app calls this endpoint from Python.

## Deploy on Render

- Root directory: `sinhala_tts`
- Runtime: Docker
- Health check path: `/health`
- Environment variable: `SINHALA_TTS_API_KEY` (a random secret, 32+ characters)

The first deployment downloads the ONNX voice model from Hugging Face. The service is
configured to use one Uvicorn worker and serial inference so the model is not loaded
multiple times or used concurrently on a small instance.

### Sinhala voice clarity

The default synthesis profile is tuned slightly slower with reduced voice variation to
make syllables easier to distinguish:

- `SINHALA_TTS_LENGTH_SCALE=1.12` — slower speech (higher values are slower)
- `SINHALA_TTS_NOISE_SCALE=0.55` — less random variation
- `SINHALA_TTS_NOISE_W=0.65` — steadier phoneme duration variation

These are synthesis tuning settings, not a model upgrade. If pronunciation remains
unclear for particular Sinhala words, the limitation may be in the medium-quality
Dilu model or its Sinhala phoneme mapping; parameter changes cannot correct every
pronunciation. The values can be adjusted in Render environment variables and tested
with the same short Sinhala sample before changing the whole service.

### Memory tuning

The default configuration favors a smaller memory footprint:

- `PIPER_ORT_THREADS=1`
- `PIPER_ORT_INTER_OP_THREADS=1`
- `PIPER_ORT_DISABLE_CPU_MEM_ARENA=true`
- `PIPER_ORT_DISABLE_MEM_PATTERN=true`
- `PIPER_ORT_GRAPH_OPT_LEVEL=basic`

The two memory options can be set to `false` to re-enable ONNX Runtime's CPU memory
arena or memory-pattern planner. `PIPER_ORT_GRAPH_OPT_LEVEL` accepts `disable`,
`basic`, `extended`, or `all`. Higher optimization levels may improve speed but
can use more memory. If the instance still restarts during inference, its RAM limit
may be below the model's working-set requirement; use a larger-memory Render instance.
These settings reduce memory pressure but cannot guarantee operation within a hard
platform memory limit.

## Local setup

Install Python 3.11+, Docker's OS dependency `espeak-ng` (or the platform equivalent),
then:

```bash
pip install -r requirements.txt
export SINHALA_TTS_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

PowerShell:

```powershell
$env:SINHALA_TTS_API_KEY = "your-random-secret-of-at-least-32-characters"
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

## License

The Dilu model is MIT licensed. It is a single-speaker female voice trained for standard
Sinhala; mixed-language pronunciation and rare words may vary. Model card:
https://huggingface.co/chaturadilan/dilu-tts
