# ARIA Voice Agent — Backend

A voice assistant backend built with FastAPI and LangChain, using local STT and Google Cloud services for the LLM and TTS.

**Pipeline:** Mic → Whisper (local STT) → LangGraph Agent (Gemini LLM + Tavily search) → Google Cloud TTS → Speaker

---

## Features

- **Local STT** — faster-whisper `base` model runs on-device (no cloud)
- **Cloud TTS** — Google Cloud Text-to-Speech, streamed as PCM chunks
- **LLM Agent** — Google Gemini via LangGraph with web search and datetime tools
- **Persistent memory** — SQLite-backed conversation history survives restarts
- **Streaming playback** — audio plays as it's generated, no buffering delay
- **Hardware-ready** — WebSocket protocol works with ESP32 push-button clients
- **Request timing** — tabular per-request latency log for every layer

---

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI + uvicorn |
| Agent | LangChain `create_agent` + LangGraph |
| STT | faster-whisper (local, `base` model) |
| TTS | Google Cloud Text-to-Speech (REST API, streamed as PCM) |
| Search tool | Tavily Search API |
| Memory | SQLite via `langgraph-checkpoint-sqlite` |
| LLM | Google Gemini (`gemini-2.5-flash` by default) |

---

## Setup

### 1. Clone and create virtual environment

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```
GOOGLE_GEMINI_API_KEY=...      # LLM — get one at https://aistudio.google.com/apikey
GOOGLE_TTS_API_KEY=...         # Google Cloud TTS — enable "Cloud Text-to-Speech API" in GCP, then create an API key
TAVILY_API_KEY=tvly-...        # Web search tool — free tier at app.tavily.com
```

> Only the Whisper STT model is downloaded automatically on first startup (~150 MB) — TTS and LLM are cloud calls, so no local model download needed for those.

### 3. Start the server

```bash
uvicorn main:app --reload
```

On first run you will see:

```
[STT] Loading Whisper 'base' model...
INFO: Application startup complete.
```

---

## Run automatically on startup (Linux / Xubuntu, systemd)

This sets up a **systemd service** that starts the server at boot (no login required) and restarts it automatically if it crashes. The service process runs as your own regular Linux user (not root) — `sudo` is only needed to install the unit file. It binds to `127.0.0.1` only (not reachable from other devices on your network).

> This runs at boot, before anyone logs in. If you'd rather it only start when *you* log in (and don't want to use `sudo` at all), see the "User service" alternative in the Gotchas section below.

### 1. Do one manual run first

Before automating anything, start the server manually on the target machine once, following the [Setup](#setup) steps above, and let it fully boot:

```bash
cd /ABSOLUTE/PATH/TO/backend
source .venv/bin/activate
uvicorn main:app
```

Confirm you see `INFO: Application startup complete.` This ensures `.env` is correct (Gemini/Google TTS keys valid) and the Whisper model has already been downloaded — you don't want that happening silently the first time systemd starts it. Once confirmed, stop it with `Ctrl+C`.

### 2. Create a logs folder

```bash
mkdir -p /ABSOLUTE/PATH/TO/backend/logs
```

### 3. Create the systemd unit file

Create `/etc/systemd/system/aria-voiceagent.service` (requires `sudo` to create) with the following contents. **Replace every `/ABSOLUTE/PATH/TO/backend`** with the real path on the target machine (run `pwd` inside the `backend` folder to get it), and replace `YOUR_LINUX_USERNAME` with your actual login (`whoami`):

```ini
[Unit]
Description=ARIA Voice Agent Backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_LINUX_USERNAME
WorkingDirectory=/ABSOLUTE/PATH/TO/backend
ExecStart=/ABSOLUTE/PATH/TO/backend/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:/ABSOLUTE/PATH/TO/backend/logs/stdout.log
StandardError=append:/ABSOLUTE/PATH/TO/backend/logs/stderr.log

[Install]
WantedBy=multi-user.target
```

Notes on the choices above:
- `WorkingDirectory` matters beyond convention — `memory/checkpoints.db` and `memory/tasks.db` are resolved **relative to the process's CWD**, so getting this wrong silently creates fresh/empty databases elsewhere.
- `User=` keeps the process running as your own account (with access to your `.env`, home directory, etc.) even though installing the unit file itself needs `sudo`.
- `Restart=on-failure` + `RestartSec=10` restarts the process only on a crash (not a clean stop), throttled to once per 10s so a bad `GOOGLE_GEMINI_API_KEY`/`GOOGLE_TTS_API_KEY` doesn't cause a restart storm.
- `After=network-online.target` / `Wants=network-online.target` is a best-effort wait for network before starting, so cold-boot Whisper downloads, Gemini/Google TTS calls, or Telegram polling don't fail immediately (requires `NetworkManager-wait-online.service` or `systemd-networkd-wait-online.service` to be active, which is the default on most Ubuntu/Xubuntu installs).

### 4. Enable and start it

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aria-voiceagent.service
```

It will now also start automatically on every boot.

### 5. Verify it's running

```bash
curl http://127.0.0.1:8000/health
tail -f /ABSOLUTE/PATH/TO/backend/logs/stdout.log
sudo systemctl status aria-voiceagent.service
```

### 6. Manage it

```bash
# Stop
sudo systemctl stop aria-voiceagent.service

# Restart (after editing .env or the unit file)
sudo systemctl restart aria-voiceagent.service

# Check status / recent logs
sudo systemctl status aria-voiceagent.service
sudo journalctl -u aria-voiceagent.service -f
```

### 7. Uninstall

```bash
sudo systemctl disable --now aria-voiceagent.service
sudo rm /etc/systemd/system/aria-voiceagent.service
sudo systemctl daemon-reload
```

### Gotchas

- **First run needs internet** to download the Whisper model and reach the Gemini/Google TTS APIs — step 1 above avoids this happening silently on the first systemd-triggered start.
- If `TELEGRAM_BOT_TOKEN` is set in `.env`, the bot starts polling Telegram at every startup, which needs network access at boot.
- Set `DASHBOARD_ACCESS_KEY` explicitly in `.env` before deploying this way. If left unset, a temporary key is generated and only printed to the startup log — annoying to dig out of `logs/stdout.log` versus just setting it yourself.
- **User service alternative** (no `sudo`, but only starts when you log in): put the same `[Service]`/`[Unit]` content (minus the `User=` line) in `~/.config/systemd/user/aria-voiceagent.service`, then run `systemctl --user daemon-reload && systemctl --user enable --now aria-voiceagent.service`. To have it start at boot even without an active login session, additionally run `loginctl enable-linger YOUR_LINUX_USERNAME`.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/api/chat` | Text chat (JSON in, JSON out) |
| `POST` | `/api/voice/chat` | Audio file in, WAV out (multipart) |
| `WS` | `/api/ws/voice` | Streaming voice pipeline |
| `GET` | `/chat-ui` | Web chat interface |
| `GET` | `/dashboard` | Request/tool-call activity dashboard |

Interactive docs: `http://localhost:8000/docs`

### Text chat

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the weather in Hyderabad?"}'
```

### Voice chat (HTTP)

```bash
curl -X POST http://localhost:8000/api/voice/chat \
  -F "audio=@recording.wav" \
  -F "voice=en-IN-Neural2-B" \
  --output reply.wav
```

### WebSocket voice protocol

```
Client → Server:
  binary frames          raw PCM audio chunks (16-bit, 16 kHz, mono)
  {"type":"end"}         signal end of audio input
  {"type":"config",...}  optional: set voice, thread_id, system_prompt

Server → Client:
  {"type":"transcript","text":"..."}   Whisper result
  {"type":"reply","text":"..."}        LLM text reply
  binary frames                        Google Cloud TTS PCM chunks (16-bit, 24 kHz, mono, 32 KB each)
  {"type":"audio_end"}                 stream complete
```

**Conversation memory:** pass `thread_id` to maintain context across turns. Omit for stateless.

---

## Test Clients

```bash
# Automated unit tests (no server needed, all mocked)
pytest tests/test_client.py -v

# Terminal text chat
python tests/text_client.py

# Live mic + speaker (streaming playback)
python tests/mic_client.py

# Send audio file, get response.wav
python tests/ws_client.py tests/fixtures/sample.wav
```

```bash
# List audio devices
python tests/mic_client.py --list-devices

# Pick specific mic/speaker
python tests/mic_client.py --input-device 0 --output-device 1 --voice en-IN-Neural2-B
```

---

## Available TTS Voices (Google Cloud, Neural2)

| Voice | Style |
|---|---|
| `en-IN-Neural2-B` | Indian English (default) |
| `en-IN-Neural2-A` / `-C` / `-D` | Indian English |
| `en-US-Neural2-D` / `-A` / `-C` / `-F` / `-H` / `-I` | American English |
| `en-GB-Neural2-A` / `-B` | British English |

Full voice catalog: https://cloud.google.com/text-to-speech/docs/voices

---

## ESP32 Hardware Client

A physical push-button voice client using an ESP32 + INMP441 mic + MAX98357A speaker.  
See [`esp32/voice_button.ino`](esp32/voice_button.ino) for the full Arduino sketch.

**Wiring:**

| Component | ESP32 Pin |
|---|---|
| Button | GPIO 15 (+ 10kΩ to 3.3V) |
| INMP441 WS | GPIO 25 |
| INMP441 SCK | GPIO 26 |
| INMP441 SD | GPIO 27 |
| MAX98357A WS | GPIO 32 |
| MAX98357A SCK | GPIO 33 |
| MAX98357A SD | GPIO 34 |

---

## Project Structure

```
backend/
├── main.py                  # FastAPI app + lifespan
├── requirements.txt
├── .env.example
├── routers/
│   ├── chat.py              # POST /api/chat
│   └── voice.py             # POST /api/voice/chat + WS /api/ws/voice
├── services/
│   ├── llm.py               # LangGraph agent + memory (Gemini)
│   ├── stt.py               # faster-whisper STT
│   ├── tts.py               # Google Cloud TTS (REST API)
│   ├── tools.py             # datetime + Tavily web search
│   ├── callbacks.py         # LangChain timing callbacks
│   └── request_timer.py     # Per-request tabular latency logging
├── tests/
│   ├── test_client.py       # pytest smoke tests (mocked)
│   ├── text_client.py       # terminal text chat
│   ├── mic_client.py        # live mic + streaming speaker
│   └── ws_client.py         # WebSocket file-based tester
├── esp32/
│   └── voice_button.ino     # ESP32 Arduino push-button client
└── memory/
    └── checkpoints.db       # SQLite conversation memory (auto-created)
```

---

## Latency Profile (typical, Mac M-series)

| Layer | Typical |
|---|---|
| STT — whisper/base | 0.4 – 0.8s |
| LLM — Gemini 2.5 Flash | 0.5 – 1.5s |
| Tool (Tavily search) | 0.5 – 1.5s |
| TTS — Google Cloud | 0.3 – 0.9s (network-dependent) |
| **Total (no tool)** | **~1.5 – 3s** |
| **Total (with search)** | **~3 – 5s** |

> These are local-model figures re-measured with cloud LLM/TTS in mind — actual latency now also depends on network round-trip to Google's APIs.
