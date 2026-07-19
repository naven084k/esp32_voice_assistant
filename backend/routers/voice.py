import json
import uuid
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from services import stt, llm, tts
from services.request_timer import new_timer, get_timer

router = APIRouter()

SUPPORTED_TYPES = {"audio/wav", "audio/mpeg", "audio/mp4", "audio/webm", "audio/ogg", "application/octet-stream"}


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
        wav_bytes = await tts.synthesize(reply, voice)
        return StreamingResponse(
            iter([wav_bytes]),
            media_type="audio/wav",
            headers={"X-Transcript": transcript, "X-Reply": reply, "X-Thread-Id": tid},
        )
    except HTTPException:
        raise
    except Exception as e:
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
      Server → client:
        - text frame {"type": "transcript", "text": "..."}
        - text frame {"type": "reply", "text": "..."}
        - binary frames: streamed MP3 audio
        - text frame {"type": "audio_end"}
        - text frame {"type": "error", "detail": "..."}
    """
    await websocket.accept()
    audio_buffer = bytearray()
    config = {
        "system_prompt": "You are a helpful voice assistant. Keep responses concise and conversational.",
        "voice": tts.DEFAULT_VOICE,
        "thread_id": str(uuid.uuid4()),
    }

    try:
        while True:
            data = await websocket.receive()

            if data.get("type") == "websocket.disconnect":
                break

            if "bytes" in data and data["bytes"]:
                audio_buffer.extend(data["bytes"])

            elif "text" in data:
                msg = json.loads(data["text"])

                if msg.get("type") == "config":
                    config.update({k: v for k, v in msg.items() if k in ("system_prompt", "voice", "thread_id")})

                elif msg.get("type") == "end":
                    if not audio_buffer:
                        await websocket.send_json({"type": "error", "detail": "No audio received"})
                        continue

                    new_timer(label="ws/voice", thread_id=config["thread_id"])
                    try:
                        transcript = await stt.transcribe(bytes(audio_buffer))
                        await websocket.send_json({"type": "transcript", "text": transcript})

                        if not transcript.strip():
                            await websocket.send_json({"type": "error", "detail": "Could not understand audio"})
                            continue

                        reply = await llm.process(transcript, thread_id=config["thread_id"], system_prompt=config["system_prompt"])
                        await websocket.send_json({"type": "reply", "text": reply})

                        async for chunk in tts.synthesize_stream(reply, config["voice"], pace=True):
                            await websocket.send_bytes(chunk)

                        await websocket.send_json({"type": "audio_end"})
                    except WebSocketDisconnect:
                        raise  # bubble up to outer handler — client left cleanly
                    except Exception as e:
                        try:
                            await websocket.send_json({"type": "error", "detail": str(e)})
                        except Exception:
                            pass  # connection may already be closed
                    finally:
                        t = get_timer()
                        if t:
                            t.log_table()
                        audio_buffer.clear()

    except WebSocketDisconnect:
        pass
