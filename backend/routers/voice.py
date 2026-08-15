import asyncio
import json
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketState

from services import stt, llm, tts, yt_song
from services.request_timer import new_timer, get_timer

router = APIRouter()
logger = logging.getLogger("voice_agent.voice")

SUPPORTED_TYPES = {"audio/wav", "audio/mpeg", "audio/mp4", "audio/webm", "audio/ogg", "application/octet-stream"}

_IST = ZoneInfo("Asia/Kolkata")


class _ActiveConnection:
    """The single currently-connected /ws/voice client (ESP32 or tests/sketch_client.py) - tracked
    at module scope so services/reminders.py can push a spoken reminder without the device having
    asked for one. `turn_task` mirrors whatever the connection's own run loop has in flight, so
    speak_unprompted() below can tell whether it's actually safe to send right now. `interactions`
    counts genuine user-initiated activity ("interrupt" / a real "end" / "greet") - see
    interaction_count() below for why a repeating reminder needs this in addition to watching for
    its own push getting cancelled."""

    def __init__(self, websocket: WebSocket, config: dict):
        self.websocket = websocket
        self.config = config
        self.turn_task: asyncio.Task | None = None
        self.interactions = 0


_active: _ActiveConnection | None = None


def interaction_count() -> int:
    """Snapshot of the active connection's interaction counter, or -1 if nothing's connected.

    services/reminders.py's repeat loop uses this as a backstop alongside speak_unprompted()'s
    "acknowledged" outcome: that outcome only fires if the tap happens to land *while a push is
    actively in flight*, so cancel_turn_task() has something to cancel. A tap landing in the pause
    between repeats hits an idle connection - cancel_turn_task() finds nothing to do, so nothing
    would otherwise tell the caller the user actually responded, and it would just repeat again on
    schedule. Comparing this counter before/after (see reminders.py) catches that case too, since
    it changes on any genuine user-initiated message, not just ones that collide with a push.
    """
    return _active.interactions if _active is not None else -1


async def speak_unprompted(text: str) -> str:
    """Speaks `text` over the current /ws/voice connection, but only if one is connected and idle
    (no turn already in flight) - used by services/reminders.py to announce due tasks without ever
    interrupting an active conversation.

    Returns one of:
      "delivered"    - played all the way through with nobody tapping to interrupt it. The caller
                        should say it again (services/reminders.py loops on this) - a reminder that
                        played out untouched hasn't been acknowledged.
      "acknowledged" - the user tapped mid-playback, which starts a new recording and (once that
                        recording finishes and sends "end") makes the WS receive loop's
                        cancel_turn_task() cancel this push - the same path a real barge-in takes.
                        The caller should stop repeating and mark the reminder as delivered.
      "declined"     - not connected, already busy with something else, or the connection dropped
                        out from under us mid-push - not an acknowledgment, just try again on the
                        caller's next normal poll rather than looping tightly.

    Reuses the same {"type": "reply"} + streamed audio + {"type": "audio_end"} shape a normal turn
    ends with (no preceding "transcript", same as the no-STT greet flow) - the ESP32 firmware arms
    playback for it exactly the way it does for greet/replies (see voice_button.ino's "reply"
    handler), so no separate wire message type is needed. Known limitation: the device tracks
    music playback locally, so if a song/radio stream happens to be playing when this fires, the
    firmware has no way to know to duck it - the reminder is skipped for now and retried later
    once the device is idle again from the backend's point of view.
    """
    conn = _active
    if conn is None or (conn.turn_task is not None and not conn.turn_task.done()):
        return "declined"

    async def _push():
        await conn.websocket.send_json({"type": "reply", "text": text})
        async for chunk in tts.synthesize_stream(text, conn.config["voice"], pace=True):
            await conn.websocket.send_bytes(chunk)
        await conn.websocket.send_json({"type": "audio_end"})

    task = asyncio.create_task(_push())
    conn.turn_task = task
    try:
        await task
        return "delivered"
    except asyncio.CancelledError:
        # Distinguish "the WS receive loop's cancel_turn_task() cancelled *this push*" from "our
        # own caller/task was cancelled" (e.g. reminders.stop() during shutdown), which does need
        # to propagate. Task.cancelling() (3.11+) tells them apart; it's 0 here unless something
        # cancelled the task that's *running this coroutine*, not the inner push task itself.
        if asyncio.current_task().cancelling():
            raise
        # cancel_turn_task() runs for two different reasons: a real barge-in tap (connection still
        # very much alive) - the acknowledgment we're looking for - or as part of the WS handler's
        # own shutdown cleanup on disconnect (connection already gone). client_state flips to
        # DISCONNECTED the moment the receive loop sees the disconnect message, before cleanup
        # even starts, so it reliably tells the two apart.
        if conn.websocket.client_state == WebSocketState.CONNECTED:
            return "acknowledged"
        return "declined"
    except Exception as e:
        logger.error(f"[ws/voice] reminder push failed: {e}", exc_info=True)
        return "declined"


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

      A "reply" + audio + "audio_end" sequence can also arrive with no client message preceding it
      at all (no "transcript") - services/reminders.py pushing a spoken task-due reminder via
      speak_unprompted() below, only when this connection is idle. The firmware treats this exactly
      like the no-transcript greet flow.

    Turn processing (STT → LLM → TTS) runs as a background task rather than being awaited inline,
    so this loop keeps reading incoming frames (in particular "interrupt") the whole time a turn is
    in flight instead of being blocked until that turn finishes.
    """
    global _active
    await websocket.accept()
    audio_buffer = bytearray()
    config = {
        "system_prompt": "You are a helpful voice assistant. Keep responses concise and conversational.",
        "voice": tts.DEFAULT_VOICE,
        "thread_id": str(uuid.uuid4()),
    }
    conn = _ActiveConnection(websocket, config)
    _active = conn

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
        fresh "end" arriving while a previous turn is somehow still running. conn.turn_task
        (rather than a plain local var) so speak_unprompted() can see it too."""
        if conn.turn_task and not conn.turn_task.done():
            conn.turn_task.cancel()
            try:
                await conn.turn_task
            except BaseException:
                pass
        conn.turn_task = None

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
                    conn.interactions += 1
                    was_running = conn.turn_task is not None and not conn.turn_task.done()
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

                    conn.interactions += 1
                    await cancel_turn_task()  # defensive: shouldn't normally still be running here
                    audio_bytes = bytes(audio_buffer)
                    audio_buffer.clear()
                    conn.turn_task = asyncio.create_task(run_turn(audio_bytes, dict(config)))

                elif mtype == "greet":
                    conn.interactions += 1
                    await cancel_turn_task()  # defensive, mirrors the "end" branch
                    conn.turn_task = asyncio.create_task(run_greeting_turn(dict(config)))

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
        if _active is conn:
            _active = None
