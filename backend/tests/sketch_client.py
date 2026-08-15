"""
Voice sketch client — a host-side stand-in for the ESP32 firmware's voice flow
(backend/esp32/voice_button.ino), for testing the backend without hardware.

Uses the host mic/speaker instead of the INMP441/MAX98357A, and Enter-key presses
instead of the touch pad. Push-to-talk: Enter to start recording, Enter to stop
and send.

    On connect: sends {"type": "greet"} once, mirroring the ESP32's own first-connect-since-boot
    greeting (no Enter press needed) - so playback starts immediately on launch.
    IDLE --(Enter)--> RECORDING
    RECORDING --(Enter)--> send audio --> PROCESSING --> SPEAKING
    SPEAKING --(reply finishes)--> IDLE
    SPEAKING --(Enter)--> RECORDING (barge-in; remainder of reply is discarded)
    A queued device action after the reply (radio / download_song / play_song) --> MUSIC
    MUSIC --(Enter)--> RECORDING   MUSIC --(finishes naturally)--> IDLE

Usage:
    python tests/sketch_client.py
    python tests/sketch_client.py --list-devices
    python tests/sketch_client.py --server http://localhost:8000 --voice en-IN-Neural2-B

Press Enter at any time: start recording / stop & send / barge-in / stop music.

Requirements: sounddevice, numpy, websockets, httpx (all already in requirements.txt).
`ffplay` (part of ffmpeg) on PATH is optional - if present, radio streams and downloaded
songs are actually played through it; otherwise those device actions are logged only.
"""
import argparse
import asyncio
import io
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave

import httpx
import numpy as np
import sounddevice as sd
import websockets

SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 24000  # matches Google Cloud TTS output rate, same as mic_client.py

SONG_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

HAS_FFPLAY = shutil.which("ffplay") is not None


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


def check_server(base_url: str):
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=3) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
    except OSError as e:
        print(f"\nERROR: Cannot reach server at {base_url}\n       {e}")
        print("\nStart the server first:\n  uvicorn main:app --reload\n")
        sys.exit(1)


def to_wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


class SketchClient:
    """One instance = one ESP32 "boot session": a single persistent WS connection plus
    the same recording/speaking/music state the firmware tracks across loop() iterations."""

    def __init__(self, ws, loop, args):
        self.ws = ws
        self.loop = loop
        self.args = args

        self.state = "IDLE"
        self.recording = False
        self.waiting_for_reply = False
        self.ignore_incoming_audio = False
        self.music_playing = False
        self.music_kind = None  # "radio" | "song"

        self.tap_event = threading.Event()
        self.mic_q: "queue.Queue[np.ndarray]" = queue.Queue()
        self.rec_chunks: list[bytes] = []
        self.rec_bytes = 0

        self.player_q: "queue.Queue[bytes | None] | None" = None
        self.player_thread: threading.Thread | None = None

        self.music_proc: subprocess.Popen | None = None

    # ---- input plumbing (mic + "touch" key) --------------------------------------------------

    def mic_callback(self, indata, _frames, _time_info, _status):
        if self.recording:
            mono = indata[:, 0] if indata.ndim > 1 else indata.reshape(-1)
            self.mic_q.put(mono.astype(np.int16).copy())

    def key_listener(self):
        while True:
            try:
                if sys.stdin.readline() == "":
                    break  # stdin closed
            except Exception:
                break
            self.tap_event.set()

    # ---- state transitions -------------------------------------------------------------------

    async def send_greet(self):
        """Mirrors the ESP32 firmware's one-time {"type": "greet"} on its first successful
        connect since boot (see routers/voice.py's run_greeting_turn) - one SketchClient
        instance = one "boot session", so this fires exactly once per script run. Starts the
        player before sending, not after (unlike send_audio()), since the static-text greeting
        has no LLM round-trip to absorb the setup delay."""
        self.state = "PROCESSING"
        self.waiting_for_reply = True
        self.player_q, self.player_thread = self._start_player()
        await self.ws.send(json.dumps({"type": "greet"}))

    async def handle_tap(self):
        if self.music_playing:
            print("[touch] stopping music to record")
            self.stop_music()
            self.start_recording()
            return
        if self.waiting_for_reply:
            print("[touch] barge-in - stopping playback to record")
            self.ignore_incoming_audio = True
            self.waiting_for_reply = False
            self.stop_player()
            await self.ws.send(json.dumps({"type": "interrupt"}))
            self.start_recording()
            return
        if self.recording:
            self.recording = False
            if self.rec_bytes > 0:
                print("<<< sending <<<")
                await self.send_audio()
            else:
                print("  No audio captured, back to idle.")
                self.state = "IDLE"
            return
        if self.state == "IDLE":
            print(">>> recording... press Enter to stop & send <<<")
            self.start_recording()

    def start_recording(self):
        self._drain_mic_queue()
        self.rec_chunks = []
        self.rec_bytes = 0
        self.recording = True
        self.state = "RECORDING"
        print("Recording... press Enter to stop & send.")

    def _drain_mic_queue(self):
        try:
            while True:
                self.mic_q.get_nowait()
        except queue.Empty:
            pass

    def collect_mic_data(self):
        try:
            while True:
                chunk = self.mic_q.get_nowait()
                data = chunk.tobytes()
                self.rec_chunks.append(data)
                self.rec_bytes += len(data)
        except queue.Empty:
            pass

    async def send_audio(self):
        self.state = "PROCESSING"
        pcm = b"".join(self.rec_chunks)
        duration = len(pcm) / 2 / SAMPLE_RATE
        print(f"  Sending {duration:.1f}s of audio...")

        wav_bytes = to_wav_bytes(pcm)
        for i in range(0, len(wav_bytes), 4096):
            await self.ws.send(wav_bytes[i : i + 4096])
        await self.ws.send(json.dumps({"type": "end"}))

        self._drain_mic_queue()

        self.waiting_for_reply = True
        self.player_q, self.player_thread = self._start_player()

    # ---- TTS reply playback ------------------------------------------------------------------

    def _start_player(self):
        q: "queue.Queue[bytes | None]" = queue.Queue()

        def _player():
            try:
                with sd.OutputStream(
                    device=self.args.output_device, samplerate=TTS_SAMPLE_RATE,
                    channels=1, dtype="int16",
                ) as out:
                    while True:
                        chunk = q.get()
                        if chunk is None:
                            break
                        out.write(np.frombuffer(chunk, dtype=np.int16))
                    time.sleep(out.latency + 0.1)
            except Exception as e:
                print(f"\n  [player] {e}", file=sys.stderr)
            self.loop.call_soon_threadsafe(self.on_playback_complete)

        t = threading.Thread(target=_player, daemon=True)
        t.start()
        return q, t

    def stop_player(self):
        if not self.player_q:
            return
        try:
            while True:
                self.player_q.get_nowait()
        except queue.Empty:
            pass
        self.player_q.put(None)
        self.player_q = None

    def on_playback_complete(self):
        if self.state != "SPEAKING":
            return
        self.waiting_for_reply = False
        print("[reply complete] Press Enter to ask again.\n")
        self.state = "IDLE"

    # ---- device actions: radio / download_song / play_song -----------------------------------

    def begin_music_state(self, kind: str):
        self.stop_player()
        self.waiting_for_reply = False
        self.music_playing = True
        self.music_kind = kind
        self.state = "MUSIC"

    def start_radio(self, url: str, name: str):
        self.begin_music_state("radio")
        print(f"[radio] playing {name} ({url})")
        if HAS_FFPLAY:
            self.music_proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", url],
                stdin=subprocess.DEVNULL,
            )
        else:
            print("  (ffplay not found on PATH - install ffmpeg to actually hear this stream)")

    def play_song(self, path: str, title: str):
        self.begin_music_state("song")
        print(f"[song] play_song request: {title} ({path})")
        print("  (no SD card on this host - path logged only, not played)")

    def stop_music(self):
        if self.music_proc and self.music_proc.poll() is None:
            self.music_proc.terminate()
        self.music_proc = None
        self.music_playing = False
        self.music_kind = None

    async def download_and_play_song(self, url: str, title: str, path: str = ""):
        self.begin_music_state("song")
        if path:
            print(f"[song] downloading '{title}' from {url} (SD path: {path})")
        else:
            print(f"[song] downloading '{title}' from {url}...")
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            total = 0
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        print(f"[song] download failed: HTTP {resp.status_code}")
                        tmp.close()
                        self.stop_music()
                        return
                    async for part in resp.aiter_bytes():
                        tmp.write(part)
                        total += len(part)
                        if total > SONG_MAX_DOWNLOAD_BYTES:
                            print("[song] download exceeded max size - aborting")
                            tmp.close()
                            self.stop_music()
                            return
            tmp.close()
        except Exception as e:
            print(f"[song] download failed: {e}")
            self.stop_music()
            return

        if total == 0:
            print("[song] download produced no data - aborting playback")
            self.stop_music()
            return

        print(f"[song] downloaded {total} bytes, starting playback")
        if not self.music_playing:
            return
        if HAS_FFPLAY:
            self.music_proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp.name],
                stdin=subprocess.DEVNULL,
            )
        else:
            print("  (ffplay not found on PATH - install ffmpeg to actually hear this song)")

    # ---- WS receive loop ---------------------------------------------------------------------

    async def recv_loop(self):
        async for msg in self.ws:
            if isinstance(msg, (bytes, bytearray)):
                if not self.ignore_incoming_audio and self.player_q:
                    if self.state != "SPEAKING":
                        self.state = "SPEAKING"
                        print("Speaking...")
                    self.player_q.put(bytes(msg))
                continue

            data = json.loads(msg)
            t = data.get("type")

            if t == "audio_end":
                if self.ignore_incoming_audio:
                    self.ignore_incoming_audio = False
                    print("(discarded remainder of interrupted reply)")
                elif self.player_q:
                    self.player_q.put(None)
                else:
                    self.waiting_for_reply = False
                    self.state = "IDLE"
                    print("Press Enter to ask again.\n")
                continue
            if t == "error":
                if self.ignore_incoming_audio:
                    self.ignore_incoming_audio = False
                    print("(discarded error from interrupted reply)")
                else:
                    print(f"  Error: {data.get('detail')}", file=sys.stderr)
                    self.waiting_for_reply = False
                    self.stop_player()
                    self.state = "IDLE"
                continue

            if self.ignore_incoming_audio:
                continue

            if t == "transcript":
                print(f"\n  You  : {data['text']}")
            elif t == "reply":
                print(f"  ARIA : {data['text']}")
                if self.state == "IDLE":
                    # Server-initiated push (task-due reminder) - no tap preceded this, so no
                    # player has been started yet; mirror send_greet()/send_audio()'s setup so
                    # the audio that follows actually gets played instead of silently dropped.
                    self.state = "PROCESSING"
                    self.waiting_for_reply = True
                    self.player_q, self.player_thread = self._start_player()
            elif t == "radio":
                self.start_radio(data.get("url", ""), data.get("name", ""))
            elif t == "stop_radio":
                if self.music_playing:
                    self.stop_music()
                    self.state = "IDLE"
            elif t == "download_song":
                asyncio.create_task(self.download_and_play_song(data.get("url", ""), data.get("title", ""), data.get("path", "")))
            elif t == "play_song":
                self.play_song(data.get("path", ""), data.get("title", ""))
            elif t == "stop_song":
                if self.music_playing:
                    self.stop_music()
                    self.state = "IDLE"

    # ---- main tick loop ----------------------------------------------------------------------

    async def state_loop(self):
        while True:
            await asyncio.sleep(0.02)

            if self.tap_event.is_set():
                self.tap_event.clear()
                await self.handle_tap()

            if self.music_playing:
                if self.music_proc and self.music_proc.poll() is not None:
                    print("[music] playback finished -> idle")
                    self.stop_music()
                    self.state = "IDLE"
                continue

            if self.recording:
                self.collect_mic_data()


async def run(args):
    check_server(args.server)
    uri = args.server.replace("http", "ws") + "/api/ws/voice"

    in_name = sd.query_devices(args.input_device if args.input_device is not None else sd.default.device[0])["name"]
    out_name = sd.query_devices(args.output_device if args.output_device is not None else sd.default.device[1])["name"]
    print(f"Mic     : {in_name}")
    print(f"Speaker : {out_name}")
    print(f"ffplay  : {'found' if HAS_FFPLAY else 'not found (radio/song playback will be logged only)'}")
    print(f"Connecting to {uri} ...")

    loop = asyncio.get_event_loop()

    async with websockets.connect(uri, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "config", "voice": args.voice, "system_prompt": args.system_prompt}))
        print("Connected.\n")
        print("Press Enter to start recording. Press Enter again to stop & send.")
        print("During playback, Enter = barge-in. During music, Enter = stop & record.")
        print("Ctrl+C to quit.\n")

        client = SketchClient(ws, loop, args)
        await client.send_greet()

        device_info = sd.query_devices(args.input_device, kind="input") if args.input_device is not None \
            else sd.query_devices(sd.default.device[0], kind="input")
        channels = int(device_info["max_input_channels"]) or 1

        threading.Thread(target=client.key_listener, daemon=True).start()

        with sd.InputStream(
            device=args.input_device, samplerate=SAMPLE_RATE, channels=channels,
            dtype="int16", blocksize=256, callback=client.mic_callback,
        ):
            await asyncio.gather(client.recv_loop(), client.state_loop())


def main():
    parser = argparse.ArgumentParser(description="Test client for the ESP32 voice flow (push-to-talk)")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--input-device", type=int, default=None, metavar="ID")
    parser.add_argument("--output-device", type=int, default=None, metavar="ID")
    parser.add_argument("--voice", default="en-IN-Neural2-B")
    parser.add_argument("--system-prompt", dest="system_prompt", default=(
        "You are ARIA, a voice assistant for the Kumar family in Hyderabad, India."
    ))
    parser.add_argument("--server", default="http://localhost:8000", metavar="URL")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nBye!")


if __name__ == "__main__":
    main()
