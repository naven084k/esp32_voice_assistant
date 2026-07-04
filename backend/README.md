# ARIA Voice Agent — Backend

A local-first voice assistant backend built with FastAPI, LangChain, and on-device AI models.

**Pipeline:** Mic → Whisper (local STT) → LangGraph Agent (OpenAI LLM + Tavily search) → Kokoro (local TTS) → Speaker

---

## Features

- **Local STT** — faster-whisper `base` model runs on-device (no cloud)
- **Local TTS** — Kokoro ONNX, streams PCM audio directly (no cloud)
- **LLM Agent** — OpenAI `gpt-4o-mini` via LangGraph with web search and datetime tools
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
| TTS | Kokoro ONNX (local, streams PCM) |
| Search tool | Tavily Search API |
| Memory | SQLite via `langgraph-checkpoint-sqlite` |
| LLM | OpenAI `gpt-4o-mini` |

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
OPENAI_API_KEY=sk-...          # LLM (gpt-4o-mini)
TAVILY_API_KEY=tvly-...        # Web search tool — free tier at app.tavily.com
```

> Whisper and Kokoro models are downloaded automatically on first startup (~230 MB total).

### 3. Start the server

```bash
uvicorn main:app --reload
```

On first run you will see:

```
[STT] Loading Whisper 'base' model...
[TTS] Downloading kokoro-v1.0.onnx from GitHub releases ...
[TTS] Kokoro ready.
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

Confirm you see `INFO: Application startup complete.` This ensures `.env` is correct and the Whisper/Kokoro models (~230 MB) have already been downloaded — you don't want that happening silently the first time systemd starts it. Once confirmed, stop it with `Ctrl+C`.

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
- `Restart=on-failure` + `RestartSec=10` restarts the process only on a crash (not a clean stop), throttled to once per 10s so a bad `OPENAI_API_KEY` doesn't cause a restart storm.
- `After=network-online.target` / `Wants=network-online.target` is a best-effort wait for network before starting, so cold-boot Kokoro downloads or Telegram polling don't fail immediately (requires `NetworkManager-wait-online.service` or `systemd-networkd-wait-online.service` to be active, which is the default on most Ubuntu/Xubuntu installs).

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

- **First run needs internet** to download the Kokoro/Whisper models — step 1 above avoids this happening silently on the first systemd-triggered start.
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
  -F "voice=am_adam" \
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
  binary frames                        Kokoro PCM chunks (16-bit, 24 kHz, mono, 32 KB each)
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
python tests/mic_client.py --input-device 0 --output-device 1 --voice am_adam
```

---

## Available TTS Voices (Kokoro)

| Voice | Style |
|---|---|
| `am_adam` | American male (default) |
| `am_michael` | American male |
| `af_bella` | American female |
| `af_sarah` | American female |
| `bf_emma` | British female |
| `bm_george` | British male |

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
│   ├── llm.py               # LangGraph agent + memory
│   ├── stt.py               # faster-whisper STT
│   ├── tts.py               # Kokoro ONNX TTS
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
├── memory/
│   └── checkpoints.db       # SQLite conversation memory (auto-created)
└── models/
    ├── kokoro-v1.0.onnx     # Kokoro TTS model (auto-downloaded)
    └── voices-v1.0.bin      # Kokoro voice embeddings (auto-downloaded)
```

---

## Latency Profile (typical, Mac M-series)

| Layer | Typical |
|---|---|
| STT — whisper/base | 0.4 – 0.8s |
| LLM — gpt-4o-mini | 0.8 – 2.0s |
| Tool (Tavily search) | 0.5 – 1.5s |
| TTS — Kokoro | 0.3 – 0.8s |
| **Total (no tool)** | **~1.5 – 3s** |
| **Total (with search)** | **~3 – 5s** |
