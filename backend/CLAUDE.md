d # CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ARIA — a FastAPI voice-assistant backend for a family ESP32 hardware client (push-to-talk mic/speaker
device), plus a Telegram bot and a couple of auth-gated web dashboards. Pipeline: mic audio → STT →
LangGraph agent (LLM + tools) → TTS → speaker, streamed over a WebSocket.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in API keys

# Run the server
uvicorn main:app --reload

# Tests
pytest                          # asyncio_mode=auto (pytest.ini) — no @pytest.mark.asyncio needed
pytest tests/test_foo.py::test_name   # single test

# Manual/hardware-less testing client (interactive, real mic+speaker, drives /ws/voice)
python tests/sketch_client.py
python tests/sketch_client.py --list-devices
python tests/sketch_client.py --server http://localhost:8000 --voice en-IN-Neural2-B
```

There is currently no automated pytest suite checked in (`tests/` only has the interactive
`sketch_client.py` + a fixture WAV) — `pytest.ini` and `requirements.txt` are set up for one, so add
`test_*.py` files there if/when tests are written.

## Architecture

### Request flow

Three entry points converge on the same agent (`services/llm.py`):
- `POST /api/voice/chat` (routers/voice.py) — one-shot audio in, WAV out, stateless HTTP.
- `WS /api/ws/voice` (routers/voice.py) — the ESP32/`sketch_client.py` protocol: persistent
  connection, binary audio frames in, binary MP3 chunks streamed out, JSON control frames both ways.
  Documented in full in the docstring at the top of `voice_ws()`.
- Telegram (routers/telegram_bot.py) — text or voice notes, one persistent thread per chat
  (`tg_{chat_id}`).
- `POST /api/chat` / `/api/chat/ui` (routers/chat.py) — text-only, the latter gated behind the
  dashboard key for the `/chat-ui` browser page.

Every turn is `stt.transcribe()` → `llm.process()` → `tts.synthesize*()`. `services/llm.py` wraps a
single LangGraph `create_agent` (`langchain.agents.create_agent`) with a SQLite checkpointer
(`memory/checkpoints.db`, via `AsyncSqliteSaver`) — one `thread_id` = one persistent conversation.
`llm.process()` strips markdown from the reply since output is spoken, not read.

### Cross-cutting state: plain dicts, not ContextVars

Tools run inside LangGraph, which may execute each tool call in its own asyncio Task (context
copied at creation time). A `ContextVar.set()` inside a tool wouldn't propagate back out to the
router that awaited the agent. So anything a tool needs to hand back to the WS/HTTP layer after
the agent call returns uses a module-level `dict` keyed by `thread_id` instead:
- `services/llm.py`: `_pending_device_actions` (queue_device_action/pop_device_action) — a tool
  (e.g. `play_radio`, `download_song`, `play_song`) queues a device-bound action; the router
  delivers it only *after* the TTS reply has fully finished streaming, so the spoken confirmation
  finishes before the station/song audio cuts in.
- `services/llm.py`: `_ws_hosts` (set_ws_host/get_ws_host) — the WS connection's own Host header,
  needed by `download_song` to build a URL the ESP32 can reach back through (it only ever talks to
  this backend through a cloudflared tunnel hostname that rotates on restart).
- `services/yt_song.py`: `_pending` — downloads awaiting the ESP32's SD-card write confirmation
  (`download_ack`), not cleaned up by thread_id/connection since the ack can legitimately arrive
  after a WS reconnect; `_sweep_stale()` is the sole age-based backstop.

`current_thread_id` (a real ContextVar) is the exception — it's set once per turn around the whole
`agent.ainvoke()` call in `llm.process()`, so it's readable synchronously by tool code without
needing to be passed through every tool signature.

### Tools (services/tools.py, math_tools.py, task_tools.py)

`build_tools()` in `services/tools.py` assembles the full tool list handed to `create_agent`.
Tools import from `services.llm` *inside the function body*, not at module level — avoids a
circular import (`llm.py` imports `build_tools` from `tools.py`).

Music has three tiers, tried in order by the LLM's own tool choice: `play_song`/`play_song_queue`
(local SD-card library, `services/song_index.py`, SQLite-backed, difflib fuzzy match against
title/alias/keywords) → `download_song` (`services/yt_song.py`, yt-dlp search+download, only after
explicit user confirmation per the system prompt) → nothing (LLM says it can't find it). Radio is
separate: `play_radio`/`stop_radio` (`services/radio.py`) checks a curated `KNOWN_STATIONS` list
first, falls back to the Radio Browser API.

`run_in_background` (services/background.py) spawns a second, independent stateless
`create_agent()` (no checkpointer) via `asyncio.create_task`, for slow multi-step research the user
explicitly said not to wait for; result is pushed to Telegram, keyed off `thread_id` starting with
`tg_`.

### Config: provider switches via env vars

`LLM_PROVIDER` (gemini/openai/ollama), `STT_PROVIDER` (google/local), `TTS_PROVIDER` (google/local)
each pick an implementation at runtime — see `.env.example` for the full matrix. Only the Google
Cloud paths are exercised in the current system prompt/latency numbers in README; local
STT/TTS (faster-whisper/pyttsx3) exist as fallbacks with different guarantees (e.g. local TTS
returns a full WAV, not a true stream — see the note in `.env.example`).

### Dashboards (auth-gated)

`/dashboard` (routers/dashboard.py) and `/data` (routers/data_admin.py) are single-file HTML+JS
pages served inline from Python string constants, gated behind `services/auth.verify_key`
(`X-Dashboard-Key` header checked against `DASHBOARD_ACCESS_KEY`, `secrets.compare_digest`).
`/dashboard` is read-only request/tool-call tracing (`services/request_timer.py`'s in-memory ring
buffer of the last 100 requests, populated by `TimingCallbackHandler` in `services/callbacks.py`).
`/data` is read/write over the actual persisted state — conversation threads (thread-level only;
LangGraph checkpoints are versioned graph snapshots, not a flat message list, so no
per-message edit), tasks, and the song index. Neither is reachable from the ESP32 itself — the
device's own on-device web server (`initSdFileServer()` in `voice_button.ino`) is a separate thing
that only serves its SD card.

`/api/media/{id}.mp3` (routers/media.py) and `/ws/voice` are deliberately unauthenticated — the
ESP32 has no way to attach the dashboard's auth header. The media route's access control is that
only a currently-registered pending download id is servable, not the id's unguessability.

### Firmware (esp32/*.ino)

Arduino sketches for the physical client, not built/tested from this Python repo. `voice_button.ino`
is the primary one — push-to-talk WS client, plus an SD-card file browser/MP3 player on port 8080
that shares I2S output with the voice pipeline. Changing the WS protocol (routers/voice.py) means
updating the matching handling in `voice_button.ino` and in `tests/sketch_client.py` (the
host-side stand-in used to test without hardware).
