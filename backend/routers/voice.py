import asyncio
import json
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from services import stt, llm, tts, yt_song
from services.request_timer import new_timer, get_timer

router = APIRouter()
logger = logging.getLogger("voice_agent.voice")

SUPPORTED_TYPES = {"audio/wav", "audio/mpeg", "audio/mp4", "audio/webm", "audio/ogg", "application/octet-stream"}

_IST = ZoneInfo("Asia/Kolkata")


@router.post("/voice/chat")
async def voice_chat(
    audio: UploadFile = File(...),
    system_prompt: str = Form("You are a helpful voice assistant. Keep responses concise and conversational."),
    voice: str = Form(tts.DEFAULT_VOICE),
    thread_id: str = Form(None),
):
    if audio.content_type not in SUPPORTED_TYPES:
        raise HTTPException(status_code=415, detail=f"Unsupported audio type: {audio.content_type}")

    tid = thread_id or str(uuid.uuid4())
    audio_bytes = await audio.read()

    new_timer(label="voice/chat", thread_id=tid)
    try:
        transcript = await stt.transcribe(audio_bytes, audio.filename or "audio.wav")
        if not transcript.strip():
            raise HTTPException(status_code=422, detail="Could not understand audio")
        reply = await llm.process(transcript, thread_id=tid, system_prompt=system_prompt)
        llm.pop_device_action(tid)  # no persistent connection here to deliver a queued device action to
        wav_bytes = await tts.synthesize(reply, voice)
        return StreamingResponse(
            iter([wav_bytes]),
            media_type="audio/wav",
            headers={"X-Transcript": transcript, "X-Reply": reply, "X-Thread-Id": tid},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[voice/chat] turn failed (thread={tid}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        t = get_timer()
        if t:
            t.log_table()


@router.websocket("/ws/voice")
async def voice_ws(websocket: WebSocket):
    """
    WebSocket voice pipeline protocol:
      Client → server:
        - binary frames: raw audio chunks
        - text frame {"type": "end"}         — signals end of audio input
        - text frame {"type": "config", ...} — optional: set system_prompt / voice / thread_id
        - text frame {"type": "interrupt"}   — barge-in: abort whatever turn is in flight (STT/LLM/
          still-streaming TTS) right away and elicit an immediate audio_end boundary marker, instead
          of the server pacing out the rest of an already-abandoned reply before it can move on
        - text frame {"type": "download_ack", "download_id": "...", "success": true|false} — the
          device's own write-confirmation for a "download_song" action (see services/yt_song.py),
          independent of any turn - handled inline below rather than inside run_turn()
        - text frame {"type": "greet"}       — sent once by the device on its first successful
          connect since boot; replies with a static, time-of-day-aware greeting (no STT, no LLM
          call, so it starts speaking immediately) so ARIA proactively greets with no tap required
      Server → client:
        - text frame {"type": "transcript", "text": "..."}
        - text frame {"type": "reply", "text": "..."}
        - binary frames: streamed MP3 audio
        - text frame {"type": "audio_end"}   — also sent (with no reply/audio of its own) as the
          boundary marker for a turn that got cut short by a client "interrupt"
        - text frame {"type": "error", "detail": "..."}
        - after audio_end, an optional queued device action: {"type": "radio", ...} /
          {"type": "stop_radio"} /
          {"type": "download_song", "download_id": "...", "url": "...", "title": "...", "path": "..."} /
          {"type": "play_song", "path": "...", "title": "..."} / {"type": "stop_song"}

    Turn processing (STT → LLM → TTS) runs as a background task rather than being awaited inline,
    so this loop keeps reading incoming frames (in particular "interrupt") the whole time a turn is
    in flight instead of being blocked until that turn finishes.
    """
    await websocket.accept()
    audio_buffer = bytearray()
    config = {
        "system_prompt": "You are a helpful voice assistant. Keep responses concise and conversational.",
        "voice": tts.DEFAULT_VOICE,
        "thread_id": str(uuid.uuid4()),
    }
    turn_task: asyncio.Task | None = None

    async def run_turn(audio_bytes: bytes, cfg: dict):
        new_timer(label="ws/voice", thread_id=cfg["thread_id"])
        host = websocket.headers.get("host")
        if host:
            llm.set_ws_host(cfg["thread_id"], host)
        try:
            transcript = await stt.transcribe(audio_bytes)
            await websocket.send_json({"type": "transcript", "text": transcript})

            if not transcript.strip():
                await websocket.send_json({"type": "error", "detail": "Could not understand audio"})
                return

            reply = await llm.process(transcript, thread_id=cfg["thread_id"], system_prompt=cfg["system_prompt"])
            await websocket.send_json({"type": "reply", "text": reply})

            async for chunk in tts.synthesize_stream(reply, cfg["voice"], pace=True):
                await websocket.send_bytes(chunk)

            await websocket.send_json({"type": "audio_end"})

            # Delivered only now (rather than the instant play_radio/stop_radio/download_song/
            # play_song/stop_song ran) so the spoken confirmation finishes before the
            # station/song audio cuts in.
            pending_action = llm.pop_device_action(cfg["thread_id"])
            if pending_action:
                await websocket.send_json(pending_action)
        except WebSocketDisconnect:
            raise  # bubble up to outer handler — client left cleanly
        except Exception as e:
            logger.error(f"[ws/voice] turn failed (thread={cfg['thread_id']}, audio_bytes={len(audio_bytes)}): {e}", exc_info=True)
            discarded = llm.pop_device_action(cfg["thread_id"])  # never delivered for this turn
            if discarded and discarded.get("type") == "download_song":
                # Otherwise this download's temp file/registry entry leaks forever - no ack will
                # ever arrive for an action the device was never actually sent.
                yt_song.confirm_download(discarded["download_id"], success=False)
            try:
                await websocket.send_json({"type": "error", "detail": str(e)})
            except Exception:
                pass  # connection may already be closed
        finally:
            t = get_timer()
            if t:
                t.log_table()

    async def run_greeting_turn(cfg: dict):
        """Sibling to run_turn() for the device's one-time {"type": "greet"} on first connect
        since boot — static text rather than an llm.process() call, so the greeting starts
        speaking immediately instead of waiting on a model round-trip."""
        try:
            hour = datetime.now(_IST).hour
            period = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
            reply = (
                f"Good {period}! I'm Tara. Tap and speak anytime — I can chat, play music, "
                f"set reminders, and more."
            )
            await websocket.send_json({"type": "reply", "text": reply})

            async for chunk in tts.synthesize_stream(reply, cfg["voice"], pace=True):
                await websocket.send_bytes(chunk)

            await websocket.send_json({"type": "audio_end"})
        except WebSocketDisconnect:
            raise  # bubble up to outer handler — client left cleanly
        except Exception as e:
            logger.error(f"[ws/voice] greeting turn failed (thread={cfg['thread_id']}): {e}", exc_info=True)
            try:
                await websocket.send_json({"type": "error", "detail": str(e)})
            except Exception:
                pass  # connection may already be closed

    async def cancel_turn_task():
        """Abort whatever turn is currently in flight and swallow its cancellation/whatever
        exception it was already failing with — used by both "interrupt" and (defensively) a
        fresh "end" arriving while a previous turn is somehow still running."""
        nonlocal turn_task
        if turn_task and not turn_task.done():
            turn_task.cancel()
            try:
                await turn_task
            except BaseException:
                pass
        turn_task = None

    try:
        while True:
            data = await websocket.receive()

            if data.get("type") == "websocket.disconnect":
                break

            if "bytes" in data and data["bytes"]:
                audio_buffer.extend(data["bytes"])

            elif "text" in data:
                msg = json.loads(data["text"])
                mtype = msg.get("type")

                if mtype == "config":
                    config.update({k: v for k, v in msg.items() if k in ("system_prompt", "voice", "thread_id")})

                elif mtype == "interrupt":
                    was_running = turn_task is not None and not turn_task.done()
                    await cancel_turn_task()
                    audio_buffer.clear()
                    if was_running:
                        # Boundary marker for the turn we just cut short - mirrors the natural
                        # audio_end a completed turn would have sent, so the client's existing
                        # "discard until audio_end" barge-in handling needs no protocol-specific case.
                        try:
                            await websocket.send_json({"type": "audio_end"})
                        except Exception:
                            pass

                elif mtype == "end":
                    if not audio_buffer:
                        await websocket.send_json({"type": "error", "detail": "No audio received"})
                        continue

                    await cancel_turn_task()  # defensive: shouldn't normally still be running here
                    audio_bytes = bytes(audio_buffer)
                    audio_buffer.clear()
                    turn_task = asyncio.create_task(run_turn(audio_bytes, dict(config)))

                elif mtype == "greet":
                    await cancel_turn_task()  # defensive, mirrors the "end" branch
                    turn_task = asyncio.create_task(run_greeting_turn(dict(config)))

                elif mtype == "download_ack":
                    # Independent of any turn - the device sends this whenever it finishes writing
                    # (or fails to write) a download_song action to SD card, possibly long after
                    # the turn that queued it has already finished.
                    download_id = msg.get("download_id")
                    if download_id:
                        yt_song.confirm_download(download_id, bool(msg.get("success")))

    except WebSocketDisconnect:
        pass
    finally:
        await cancel_turn_task()
        # Deliberately NOT cleaning up any download still pending for this connection here - the
        # ESP32's download itself completes over a separate HTTP connection, so a WS drop doesn't
        # mean the download failed; the ack can legitimately arrive after the device reconnects.
        # yt_song._sweep_stale() (age-based, run opportunistically on the next download request)
        # is the sole backstop for downloads that are genuinely abandoned.
        llm.clear_ws_host(config["thread_id"])
