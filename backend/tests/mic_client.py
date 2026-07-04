"""
Live microphone voice client — push-to-talk with streaming playback.

Flow:
  1. Press Enter → start recording from mic
  2. Press Enter again → stop, send to server
  3. Server transcribes, LLM replies, TTS streams back
  4. Audio plays chunk-by-chunk as it arrives (no wait for full response)
  5. Repeat (Ctrl+C to quit)

Usage:
    python tests/mic_client.py
    python tests/mic_client.py --list-devices
    python tests/mic_client.py --input-device 1 --output-device 3
    python tests/mic_client.py --voice nova --system-prompt "You are a pirate."

Requirements:
    pip install sounddevice numpy miniaudio websockets
"""
import argparse
import asyncio
import io
import json
import queue
import sys
import threading
import time
import urllib.request
import wave

import numpy as np
import sounddevice as sd
import websockets

SAMPLE_RATE = 16000
DTYPE = "int16"
TTS_SAMPLE_RATE = 24000  # Kokoro native output rate


# ─── Audio devices ────────────────────────────────────────────────────────────

def list_devices():
    devices = sd.query_devices()
    print("\nAvailable audio devices:")
    print(f"  {'ID':<4} {'Type':<8} Name")
    print("  " + "-" * 50)
    default_in, default_out = sd.default.device
    for i, d in enumerate(devices):
        tags = []
        if d["max_input_channels"] > 0:
            tags.append("input")
        if d["max_output_channels"] > 0:
            tags.append("output")
        marker = ""
        if i == default_in and i == default_out:
            marker = " ← default in/out"
        elif i == default_in:
            marker = " ← default input"
        elif i == default_out:
            marker = " ← default output"
        print(f"  {i:<4} {'/'.join(tags):<8} {d['name']}{marker}")
    print()


# ─── Microphone recording ──────────────────────────────────────────────────────

def record_until_enter(input_device) -> np.ndarray:
    device_info = sd.query_devices(input_device, kind="input")
    channels = int(device_info["max_input_channels"])
    frames = []
    stop_event = threading.Event()

    def callback(indata, _frames, _time, _status):
        if not stop_event.is_set():
            frames.append(indata.copy())

    print("  Recording... press Enter to stop.")
    with sd.InputStream(device=input_device, samplerate=SAMPLE_RATE,
                        channels=channels, dtype=DTYPE, callback=callback):
        input()
        stop_event.set()

    if not frames:
        return np.zeros((0,), dtype=np.int16)
    audio = np.concatenate(frames, axis=0)
    if audio.ndim > 1 and audio.shape[1] > 1:
        audio = audio.mean(axis=1)
    return audio.astype(np.int16).reshape(-1)


def to_wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.astype(np.int16).tobytes())
    return buf.getvalue()


# ─── Streaming playback ────────────────────────────────────────────────────────

def start_streaming_player(output_device) -> tuple[queue.Queue, threading.Thread]:
    """
    Starts a background thread that plays raw PCM int16 chunks from a queue
    directly via sounddevice — no encoding/decoding needed.
    Put bytes chunks in the queue as they arrive; put None to signal end.
    """
    chunk_q: queue.Queue[bytes | None] = queue.Queue()

    def _player():
        try:
            with sd.OutputStream(
                device=output_device,
                samplerate=TTS_SAMPLE_RATE,
                channels=1,
                dtype="int16",
            ) as out:
                while True:
                    chunk = chunk_q.get()
                    if chunk is None:
                        break
                    out.write(np.frombuffer(chunk, dtype=np.int16))
                # sd.OutputStream closes immediately on exit — any audio still
                # in the hardware buffer gets cut off. Sleep for the stream's
                # latency so the last chunk finishes playing before we close.
                time.sleep(out.latency + 0.1)
        except Exception as e:
            print(f"\n  [Player] {e}", file=sys.stderr)

    t = threading.Thread(target=_player, daemon=True)
    t.start()
    return chunk_q, t


# ─── Server check ─────────────────────────────────────────────────────────────

def check_server(base_url: str):
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=3) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
    except OSError as e:
        print(f"\nERROR: Cannot reach server at {base_url}\n       {e}")
        print(f"\nStart the server first:\n  uvicorn main:app --reload\n")
        sys.exit(1)


# ─── Main loop ────────────────────────────────────────────────────────────────

async def run(voice: str, system_prompt: str, input_device, output_device,
              base_url: str = "http://localhost:8000"):
    check_server(base_url)
    uri = base_url.replace("http", "ws") + "/api/ws/voice"

    in_name  = sd.query_devices(input_device  if input_device  is not None else sd.default.device[0])["name"]
    out_name = sd.query_devices(output_device if output_device is not None else sd.default.device[1])["name"]
    print(f"Mic     : {in_name}")
    print(f"Speaker : {out_name}")
    print(f"Connecting to {uri} ...")

    async with websockets.connect(uri, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "config", "voice": voice,
                                  "system_prompt": system_prompt}))
        print("Connected. Press Ctrl+C to quit.\n")

        loop = asyncio.get_event_loop()

        while True:
            try:
                print("Press Enter to start recording...")
                await loop.run_in_executor(None, input)
            except EOFError:
                break

            audio = await loop.run_in_executor(None, record_until_enter, input_device)
            if len(audio) == 0:
                print("  No audio captured, skipping.\n")
                continue

            duration = len(audio) / SAMPLE_RATE
            print(f"  Captured {duration:.1f}s — sending to server...")

            wav_bytes = to_wav_bytes(audio)
            for i in range(0, len(wav_bytes), 4096):
                await ws.send(wav_bytes[i : i + 4096])
            await ws.send(json.dumps({"type": "end"}))

            # Start streaming player before first chunk arrives
            chunk_q, player_thread = start_streaming_player(output_device)
            playing = False

            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    if not playing:
                        print("  Playing response...")
                        playing = True
                    chunk_q.put(msg)          # decoded + played immediately
                else:
                    data = json.loads(msg)
                    if data["type"] == "transcript":
                        print(f"\n  You     : {data['text']}")
                    elif data["type"] == "reply":
                        print(f"  Bot     : {data['text']}")
                    elif data["type"] == "audio_end":
                        chunk_q.put(None)     # signal player to finish
                        break
                    elif data["type"] == "error":
                        chunk_q.put(None)
                        print(f"  Error   : {data['detail']}", file=sys.stderr)
                        break

            # Wait for player to drain remaining samples
            await loop.run_in_executor(None, player_thread.join, 30)
            print()


def main():
    parser = argparse.ArgumentParser(description="Live mic voice client (streaming playback)")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--input-device",  type=int, default=None, metavar="ID")
    parser.add_argument("--output-device", type=int, default=None, metavar="ID")
    parser.add_argument("--voice", default="am_adam",
                        choices=["af_bella", "af_sarah", "af_sky", "af",
                                 "am_adam", "am_michael",
                                 "bf_emma", "bf_isabella", "bm_george", "bm_lewis"])
    parser.add_argument("--system-prompt", default="You are a helpful voice assistant.",
                        dest="system_prompt")
    parser.add_argument("--server", default="http://localhost:8000", metavar="URL")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    try:
        asyncio.run(run(args.voice, args.system_prompt,
                        args.input_device, args.output_device, args.server))
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
