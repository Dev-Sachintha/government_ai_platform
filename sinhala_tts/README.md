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

The first deployment downloads the ONNX voice model from Hugging Face. Ensure the selected
instance has enough RAM for Piper and ONNX Runtime. A 512 MB free instance may be too small.

## Local setup

Install Python 3.11+, Docker's OS dependency `espeak-ng` (or the platform equivalent),
then:

```bash
pip install -r requirements.txt
export SINHALA_TTS_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn main:app --host 0.0.0.0 --port 8000
```

PowerShell:

```powershell
$env:SINHALA_TTS_API_KEY = "your-random-secret-of-at-least-32-characters"
uvicorn main:app --host 0.0.0.0 --port 8000
```

## License

The Dilu model is MIT licensed. It is a single-speaker female voice trained for standard
Sinhala; mixed-language pronunciation and rare words may vary. Model card:
https://huggingface.co/chaturadilan/dilu-tts
