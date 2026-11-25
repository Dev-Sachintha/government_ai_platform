# Enterprise-Grade Local AI & Document Reporting Platform

A multi-user, RBAC-protected local AI platform built with Streamlit, SQLite, Ollama, and PyPDF.

## Features
- **Government Services & Document Search:** Automatic extraction and searching of ingested PDF documents alongside SQLite tables.
- **Interactive AI Assistant:** Natural language SQL query generation and PDF context-aware document Q&A via Ollama.
- **Audit Logging & Security:** Detailed audit logs, inactivity timeouts, sidebar live countdown timer, and client-side activity tracking.
- **Database Restoration & Validation:** Backup restoration with pre-check validation (`PRAGMA integrity_check` & schema verification).
- **Automated Workflows:** Background scheduler for weekly report generation and email dispatch.

## Requirements
- Python 3.10+
- [Ollama](https://ollama.com) running locally with the `llama3` model (`ollama pull llama3`)

## Quick Start
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Run PDF Ingestion (if you have `Downloads.zip` with government PDFs):
   ```bash
   python pdf_ingest.py
   ```
3. Launch the Streamlit application:
   ```bash
   python -m streamlit run app.py
   ```

## Website Sources (admin)
Admins can add website links in the **🌐 Website Sources** tab. The page text is fetched,
split into small sections and stored in the database (`web_sources`, `web_pages` tables).
The chat assistant searches these sections together with the ingested PDFs and mentions the
website URL when it uses one.

- Works with normal web pages, PDF links and plain-text pages.
- Pages that need JavaScript to show their content cannot be read.
- Use 🔄 to fetch a page again after it changes, 🗑️ to remove it.
- Security: only public http/https addresses are allowed (localhost and private network
  addresses are blocked), pages over 5 MB are refused, and every change is written to `audit_logs`.
- Analysts can ask questions about the websites but cannot add or remove them.

## Image & PDF Library (admin)
Admins add images (PNG, JPG, WEBP, GIF) and PDF files in the **🖼️ Image & PDF Library** tab.
Every image and every PDF page is stored in the database as a picture plus searchable text:
the admin's description/keywords, text extracted from PDF pages, and (optionally) an AI
description made by a local vision model.

In the chat, ask for what you want to see, e.g. *"show me the Lotus Tower"* or
*"show the passport application form"*. The assistant searches the library and displays the
matching pictures (up to 3) under its answer. A picture is also shown without asking when
its title/description clearly matches the question.

- **Scanned PDFs / pictures with no text:** click 🤖 *Describe* so a vision model can describe
  them and read their text. One-time setup: `ollama pull llava` (smaller: `ollama pull moondream`).
  Up to 30 items are processed per click.
- Limits: images 10 MB, PDFs 25 MB / 150 pages. Duplicates are skipped.
- Needs `pypdfium2` and `Pillow` (in `requirements.txt`) to show PDF pages as pictures.
- Analysts can ask the chat to show library files but cannot add, edit or delete them.
- The AI can never read the internal tables (audit log, library, website data) through SQL.

## Deploying on Streamlit Community Cloud
The cloud server cannot reach Ollama on your own computer, so the chat needs a hosted model.

1. Get an API key (e.g. a free one from Groq: https://console.groq.com/keys).
2. In Streamlit Cloud open your app: **Manage app > ⋮ > Settings > Secrets** and paste the
   lines from `secrets_example.toml` (with your real key and passwords). Save; the app restarts.
3. Never put the key or passwords in GitHub. Secrets stay in Streamlit's settings only.

Supported models (`LLM_PROVIDER`): `ollama` (local, default), `openai` (any OpenAI-compatible API:
Groq, OpenRouter, Gemini, OpenAI...), `anthropic` (Claude API). Running locally with Ollama needs no secrets.

**Important on Streamlit Cloud**
- Saved data (websites, library, audit log) is temporary and is lost when the app restarts.
  Admins: use **💾 Backup DB** in the sidebar regularly and **🔄 Restore DB** after a restart.
- Set `ADMIN_PASSWORD` / `ANALYST_PASSWORD` in Secrets. Without them the old default passwords
  are used and the app shows a warning.

## Adding documents (PDF handbooks etc.)
- Add PDFs and pictures in the **🖼️ Image & PDF Library** tab. Each PDF page is stored with its text,
  so the chat can answer from it (up to 150 pages / 25 MB per PDF).
- **Restore DB** is only for backups made with **Download backup**. A PDF, Word file or picture
  (even if renamed to .db) is rejected with a message that points you to the Library tab.
- The chat answers from the best-matching part of each page. Ask with the words the document uses
  (for example *"Trincomalee computer science 2022/2023"*).

## Permanent storage (fixes "websites / library files disappear")
Streamlit Cloud wipes the app's disk on every restart, sleep or redeploy, so anything added at
runtime is lost. The app now **auto-saves the database to a private GitHub data repo and restores it on startup**.

One-time setup:
1. Create a **private** GitHub repository only for data (e.g. `gov-ai-data`) with a README.
2. GitHub > Settings > Developer settings > *Fine-grained personal access tokens* > create a token for
   **only that repo** with **Contents: Read and write**.
3. In Streamlit: *Manage app > Settings > Secrets*, add:
   ```toml
   PERSIST_GITHUB_TOKEN = "github_pat_..."
   PERSIST_GITHUB_REPO  = "your-name/gov-ai-data"
   ```
4. Reboot the app. Sidebar > *Session & System Admin* > **Permanent storage** shows "Auto-saved at ...".

Notes: saving happens a few seconds after any change (use **Save now** to force it). Use a separate data repo,
never the app repo, or every save would redeploy the app. Keep the repo private: it holds your data.

## Talking avatar (🔊 Listen)
When you press **🔊 Listen** under an AI answer, a picture panel (`static/avatar.jpg`) pops up in the
bottom-right corner and the character's mouth moves while the browser reads the answer aloud.
It hides automatically when the speech ends, or press **⏹ Stop**. To use another face, replace
`static/avatar.jpg` (720x720) and adjust the mouth numbers (`CX`, `YS`, `SX` ...) in `avatar_js.py`.
Needs `enableStaticServing = true` in `.streamlit/config.toml` (already set).

## Voice buttons (📋 Copy, 🔊 Listen, ⏹️ Stop, 🎤 Speak)
Copy/Listen/Stop controls for browser-native languages and the Speak dictation bar use small iframe
components (`voice_ui.py`, scripts in `avatar_js.py`). They use the main page's speech engine /
microphone through `window.parent`. Use Chrome or Edge and allow the microphone when asked.
Sinhala Listen uses the separate server-side Piper service described below and returns a WAV audio player.


## Self-hosted Sinhala Text-to-Speech (TTS)

Sinhala audio is generated by a separate FastAPI service using the open-source
[Piper Dilu Sinhala voice](https://huggingface.co/chaturadilan/dilu-tts). The Streamlit
server calls this service server-to-server and receives a WAV file. The API key is never
sent to the browser. Other languages continue to use installed browser speech voices.

### Deploy the Sinhala TTS service

1. In Render, create a **Web Service** from this repository.
2. Set **Root Directory** to `sinhala_tts`.
3. Set **Runtime** to Docker (Render detects the Dockerfile).
4. Add an environment variable named `SINHALA_TTS_API_KEY` with a long random secret
   (at least 32 characters). Do not commit this key to GitHub.
5. Set the health check path to `/health` and deploy. The first start downloads the
   approximately 61 MB voice model from Hugging Face.
6. Copy the service URL, for example `https://your-sinhala-tts-service.onrender.com`.

### Connect Streamlit to the service

In Streamlit Community Cloud, open **Manage app → Settings → Secrets** and add:

```toml
SINHALA_TTS_URL = "https://your-sinhala-tts-service.onrender.com"
SINHALA_TTS_API_KEY = "the-same-long-random-secret"
```

Save the secrets and reboot the Streamlit app. In AI Chat, select **Sinhala (සිංහල)**,
press **Translate** if required, then press **🔊 Listen (Sinhala)**. The generated WAV
appears in the audio player.

### Run the TTS service locally

From the repository root:

```bash
cd sinhala_tts
python -m venv .venv
# Activate the environment, then:
pip install -r requirements.txt
# Install the operating-system espeak-ng package before starting the service.
export SINHALA_TTS_API_KEY="replace-with-a-long-random-secret"
uvicorn main:app --host 0.0.0.0 --port 8000
```

Windows PowerShell:

```powershell
$env:SINHALA_TTS_API_KEY = "replace-with-a-long-random-secret"
uvicorn main:app --host 0.0.0.0 --port 8000
```

The model is a single-speaker medium-quality Sinhala female voice. It is optimized for
standard Sinhala text; unusual words and English/Sinhala code-switching may be pronounced
less naturally. The model is distributed under the MIT license. The service requires
CPU/RAM sufficient to load Piper and the ONNX model; a 512 MB free hosting instance may
not be sufficient. Free Render services also sleep after inactivity and may take time to
wake up. See [Render's free-service limitations](https://render.com/docs/free).

## Advanced Maths links
Website Sources tab > **📐 Advanced Maths links (one-click)** adds a ready-made list of maths websites
(MathWorld, Encyclopedia of Mathematics, Paul's Online Math Notes, MIT OCW, LibreTexts, Wikipedia topic pages ...).
Edit the list in `math_links.py`. Sites that block automated reading are reported and skipped.

## HoloMotion 3D tab (🖐️ gesture-controlled 3D models)
Added from the HoloMotion 3D project by G. Charan Teja (MIT licence, see `holomotion/LICENSE`).
Open the **🖐️ HoloMotion 3D** tab, click **▶ Launch**, then **Start with camera** and allow the webcam.
It is served by Streamlit's component server from the `holomotion/` folder (no npm build needed).
Hand tracking runs in the browser; the MediaPipe engine is loaded from jsDelivr (needs internet).
Use Chrome or Edge. The viewer is only started when you click Launch, so it does not slow the rest of the app.

## Add your own 3D model (HoloMotion tab)
In the HoloMotion viewer press **➕** (or drag a file onto the viewer) and pick a **.glb, .gltf, .obj, .stl or .ply** file.
It is turned into glowing particles and appears under the **My Models** tab; use the normal gestures with it.
- **🧭** rotates the orientation if the model lies on its side (CAD/STL files are usually Z-up).
- **🗑** removes all models you added. Added models are remembered in your browser (IndexedDB), not on the server.
- Colours are kept when the file has vertex colours or material colours. glTF with Draco/Meshopt compression or an
  external .bin file is not supported: export a plain .glb.

## 📷 Scan (object -> AI -> information)
* **📷 Scan tab:** take a photo with the camera (or upload one) and press **Scan**. The AI vision model identifies the
  main object and the information comes up. Needs a model that can read pictures (`LLM_VISION_MODEL`; the Claude and
  most OpenAI-compatible models can; for Ollama use `llava`).
* **Admins add things one by one** in the same tab (🛠️ Admin: scan catalogue): name, aliases, information, link and an
  optional reference photo. If a scanned object matches an item, that item's official information is shown;
  otherwise the AI's estimate is shown and marked "may be wrong".
* **🔍 button in HoloMotion 3D:** turn the camera on, hold an object in front of it and press 🔍 - the same result
  appears in a panel, with a "View 3D model" button when a matching 3D model exists.
* Files: `scan.py` (database + AI), `scan_ui.py` (tab), `holomotion_tab.py` (viewer bridge).

## User Management and Screen Monitor
- The initial `admin` and `analyst` accounts are created in SQLite on first launch, using `ADMIN_PASSWORD` / `ANALYST_PASSWORD` secrets or the documented development defaults.
- Both Admin and Analyst roles can open **👥 User Management** and create additional Admin or Analyst accounts. New passwords are stored as salted PBKDF2-SHA256 hashes, not plaintext.
- Both roles have a **🖥️ Screen Monitor** button in the sidebar and a Screen Monitor tab. Screen capture starts only after the signed-in user clicks Start and approves the browser's screen/window selection prompt. The preview stays in that browser and is not sent to the server. Use Chrome or Edge on HTTPS or localhost.

## Mobile Phone Monitor (Admin / Analyst only)

The **📱 Mobile Phone Monitor** dashboard is available only to Admin and Analyst accounts. It shows paired Android phones, model/Android version, battery percentage, connection status, resolution, and the latest screen frame. The Android owner must start sharing in the companion app and approve Android's MediaProjection system prompt. A persistent Android notification is shown while sharing; the owner can stop at any time. The feature does not collect microphone audio, camera images, messages, keystrokes, or touch input.

### Deploy the monitor API separately

Streamlit Community Cloud does not provide a persistent inbound API endpoint for an Android companion app. Therefore the project includes a small separate FastAPI service in `mobile_monitor_service/`. Deploy that folder as a private HTTPS web service (the included `render.yaml` is a Render blueprint):

1. Create a long random secret (at least 32 characters). Set it as `MOBILE_MONITOR_API_KEY` in the API service environment.
2. Deploy `mobile_monitor_service/` using `pip install -r requirements.txt` and `uvicorn main:app --host 0.0.0.0 --port $PORT`.
3. In Streamlit Cloud → Manage app → Settings → Secrets, set the same key and the API service URL:
   ```toml
   MOBILE_MONITOR_API_URL = "https://YOUR-MONITOR-SERVICE.onrender.com"
   MOBILE_MONITOR_API_KEY = "the-same-long-random-secret"
   ```
4. Reboot the Streamlit app. Do not commit the key to GitHub. Use HTTPS only.

### Build and install the Android companion

Open `android_companion/` in Android Studio (JDK 17 recommended), allow Gradle sync, then build and install the debug APK on the Android phone. Enter the HTTPS API URL and the same pairing key, tap **Start Screen Sharing**, and approve Android's system permission prompt. Android 10+ is recommended. Keep the phone awake and connected to the internet for continuous updates.

### Data handling and limitations

The API uses a shared secret header, keeps device telemetry and the latest JPEG frame in service memory only, and expires screen frames after three minutes. Restarting the API clears its in-memory device/frame list. Use a private deployment, rotate the shared key if exposed, and restrict access to the service where possible. Screen preview is refreshed as JPEG snapshots (approximately every 1.5 seconds), not a high-frame-rate video stream. This implementation is for explicitly authorized devices only; it has no stealth mode or remote-control capability.

## 🖥️ Screen Monitor for other computers (consent-based)
Uses the same monitor service as the phone monitor (`mobile_monitor_service/`, deployed on Render) and the same
`MOBILE_MONITOR_API_URL` / `MOBILE_MONITOR_API_KEY` secrets.

1. On the other computer, open `https://<your-service>.onrender.com/pc` in Chrome or Edge.
2. Enter a computer name and the pairing key, press **Start sharing this screen**, choose a screen and approve the
   browser prompt. The page shows a red "your screen is being shared" banner and the browser shows its own indicator.
3. Admins/analysts see it in the **Screen Monitor** tab (live picture every few seconds, view only).
4. The person presses **Stop sharing** at any time; the last picture is deleted from the service immediately.

Nothing is captured without the user choosing a screen; there is no remote control, sound, camera, microphone,
keyboard or file access.

## Roles and what each can see
| Role | Tabs |
|---|---|
| **user** | AI Workspace & Chat, Scan, HoloMotion 3D (nothing else is created or shown) |
| **analyst** | all tabs; can create `user` and `analyst` accounts; can disable/enable `user` accounts |
| **admin** | all tabs; can create any account; can disable/enable any account except their own |

Add ordinary users in **👥 User Management** (username, temporary password of 10+ characters, role = `user`).
An unknown or missing role is treated as `user` (least privilege). Older databases are upgraded automatically
(the `app_users` table is rebuilt once so the `user` role is allowed; existing accounts and passwords are kept).

## 📈 PC Hardware, Network & System Activity Monitoring (consent-based)
The **PC System Monitoring** tab adds CPU/RAM/GPU/disk metrics, supported temperature sensors, network throughput,
running process summaries, uptime and recent Windows error events. It uses a local agent because a hosted Streamlit
app cannot directly inspect the hardware of a visitor's computer.

1. Deploy `mobile_monitor_service/` as described above. The same `MOBILE_MONITOR_API_URL` and
   `MOBILE_MONITOR_API_KEY` secrets are reused.
2. On each computer whose owner has agreed to share telemetry, install Python 3.10+ and copy
   `mobile_monitor_service/pc_monitor_agent.py` to that computer.
3. In PowerShell run:
   ```powershell
   py -m pip install psutil requests
   py pc_monitor_agent.py --url "https://YOUR-MONITOR-SERVICE" --key "YOUR-PAIRING-KEY" --name "Office PC"
   ```
4. The computer appears in **📈 PC System Monitoring** for admins/analysts. The agent sends a snapshot every
   five seconds. Press `Ctrl+C` to stop sharing.

The agent sends hardware/network metrics, process names and recent Windows error summaries only. It does not
capture screen/audio/video, keystrokes or files and provides no remote control. Temperature sensors depend on
hardware/OS support; NVIDIA GPU details require `nvidia-smi`. The service keeps only the latest metrics in memory
(no historical charts); a service restart clears them. Error-event messages can contain computer/user-specific
text, so run the agent only with the computer owner's informed permission and protect the pairing key.

## Screen Monitor / Mobile Phone Monitor onboarding card
When no device is sharing yet, the admin/analyst sees a plain "Share your screen?" card instead of
a bare warning: the link (or app steps), **why** it's being asked (editable via the Secrets key
`SCREEN_MONITOR_REASON`), and an explicit **"If you'd rather not"** line stating that declining does
not restrict the person's access to this app or anything else. Nothing about this card can lock a
person out of the platform - it only ever explains a choice that was already optional.


### Auto Screen Monitoring (PC)

The Screen Monitor dashboard now has an **Auto Screen Monitoring** action. It creates a one-time launch link (5-minute expiry) for the currently signed-in platform account. Opening that link pre-fills the pairing key and account name on the PC sharing page; the permanent API key is not placed in the URL. The person at the PC must still click **Start sharing this screen** and choose/approve a screen in the browser. This explicit browser permission cannot be bypassed. Manual pairing at `/pc` remains available.

Deploy the updated `mobile_monitor_service/main.py` and `mobile_monitor_service/pc_share.html` together with the updated `app.py`. Keep `MOBILE_MONITOR_API_KEY` identical in the Streamlit app secrets and the monitor service environment.

## Fix: Listen and Canvas for every language (not just English)
- **🔊 Listen** used to recognise only Sinhala, Tamil, Hindi, Arabic, Chinese, Japanese, Korean and
  Russian by their script, and silently read anything else as English. `avatar_js.py`'s
  `detectLanguage()` now also recognises Bengali, Punjabi, Gujarati, Odia, Telugu, Kannada,
  Malayalam, Thai, Lao, Burmese, Khmer, Hebrew, Greek, Armenian and Georgian by their script, plus
  a light accent/word heuristic for a few Latin-script languages (French, German, Spanish,
  Portuguese, Italian, Vietnamese, Turkish, Indonesian, Swahili). Very short, accent-free sentences
  in a Latin-script language can still fall back to English — a full multi-sentence AI answer
  almost always carries enough of a language's own accents/words to be detected correctly.
- **🎨 Canvas** used to auto-open only when a reply BOTH was over 350 characters AND contained a
  &#96;&#96;&#96; code fence, so a long plain-text document (a letter, a report, an essay) with no
  code in it never opened Canvas automatically, in any language. `chat_app.py`'s
  `_should_auto_open_canvas()` now opens Canvas for a code block of any size, OR for any reply at
  least 350 characters long, regardless of script — this is a plain character count, not an
  English-only word/space check, so Sinhala and Tamil documents (which often run together with
  fewer spaces than English) are not penalised.

## Fix: Listen now actually speaks in the answer's own language, for every language
Two separate bugs were stacked on top of each other, for Sinhala and every other non-English language:
1. **Voices loaded too late.** `speechSynthesis.getVoices()` returns an empty list for the first
   fraction of a second after the page loads; Listen used to call it immediately, so it almost
   always "found" no matching voice even when one existed. It now waits for the real voice list
   (`voicesReady()` in `avatar_js.py`) before picking one, up to 1.2s.
2. **Silent failure when no voice is installed.** If the device genuinely has no Sinhala/Tamil/
   etc. voice, the browser used to either stay silent or substitute a wrong accent with no
   explanation. Listen now says exactly that, by name, for whichever language was detected (not
   hard-coded to Sinhala), and tells the person how to install that language's voice on Android or
   on a desktop browser.
This generalises to every language `detectLanguage()` can return, so the same fix covers Sinhala,
Tamil, and any language added later - nothing here is Sinhala-specific.
