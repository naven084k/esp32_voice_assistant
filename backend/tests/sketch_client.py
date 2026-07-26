"""
Voice sketch client — a host-side stand-in for the ESP32 firmware's hands-free voice
flow (backend/esp32/voice_button.ino), for testing the backend without hardware.

Mirrors the firmware's state machine (loop() / vadListenTick() / drainTtsRing() /
webSocketEvent()) using the host mic/speaker instead of the INMP441/MAX98357A, and
Enter-key presses instead of the touch pad:

    IDLE --(tap: Enter)--> LISTENING
    LISTENING --(2s trailing silence after speech)--> send audio --> SPEAKING
    LISTENING --(10s, no speech at all)--> IDLE (abandoned, nothing sent)
    LISTENING --(tap)--> IDLE (cancelled, nothing sent)
    SPEAKING --(reply audio finishes naturally)--> LISTENING (auto-continue, no tap needed)
    SPEAKING --(tap)--> LISTENING immediately (barge-in; remainder of the old reply is discarded)
    A queued device action after the reply (radio / download_song / play_song) --> MUSIC
    MUSIC --(tap)--> LISTENING   MUSIC --(finishes naturally)--> IDLE

Usage:
    python tests/sketch_client.py
    python tests/sketch_client.py --list-devices
    python tests/sketch_client.py --vad-threshold 400   # host mics vary a lot - tune this
    python tests/sketch_client.py --server http://localhost:8000 --voice en-IN-Neural2-B

Press Enter at any time to simulate a touch (start listening / cancel / barge-in / stop music).

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

# Same constants/semantics as backend/esp32/voice_button.ino - kept in sync by hand.
VAD_RMS_THRESHOLD = 600
VAD_TRAILING_SILENCE_MS = 2000
VAD_NO_SPEECH_TIMEOUT_MS = 10000
RECORD_SECONDS = 15
RECORD_BUFFER_BYTES = SAMPLE_RATE * 2 * RECORD_SECONDS

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
    the same listening/speaking/music state the firmware tracks across loop() iterations."""

    def __init__(self, ws, loop, args):
        self.ws = ws
        self.loop = loop
        self.args = args

        self.state = "IDLE"
        self.listening = False
        self.waiting_for_reply = False
        self.ignore_incoming_audio = False
        self.music_playing = False
        self.music_kind = None  # "radio" | "song"

        self.tap_event = threading.Event()
        self.mic_q: "queue.Queue[np.ndarray]" = queue.Queue()
        self.rec_chunks: list[bytes] = []
        self.rec_bytes = 0
        self.vad_speech_started = False
        self.vad_listen_start = 0.0
        self.vad_last_speech = 0.0
        self.vad_frames_over = 0
        self._last_rms_log = 0.0

        self.player_q: "queue.Queue[bytes | None] | None" = None
        self.player_thread: threading.Thread | None = None

        self.music_proc: subprocess.Popen | None = None

    # ---- input plumbing (mic + "touch" key) --------------------------------------------------

    def mic_callback(self, indata, _frames, _time_info, _status):
        if self.listening:
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

    # ---- state transitions (mirror loop()'s tap-handling priority order) ------------------------

    async def handle_tap(self):
        if self.music_playing:
            print("[touch] stopping music to listen")
            self.stop_music()
            self.start_listening()
            return
        if self.waiting_for_reply:
            print("[touch] barge-in - stopping playback to listen again")
            self.ignore_incoming_audio = True
            self.waiting_for_reply = False
            self.stop_player()
            # Tells the backend to actually abort the in-flight turn (STT/LLM/still-streaming,
            # real-time-paced TTS) instead of dutifully finishing it before it can look at anything
            # we send next - without this, the server has no idea we've stopped listening and the
            # next request sits queued behind however long the abandoned reply had left to run.
            await self.ws.send(json.dumps({"type": "interrupt"}))
            self.start_listening()
            return
        if self.listening:
            self.listening = False
            self.state = "IDLE"
            print("[touch] cancelled listening, back to idle")
            return
        if self.state == "IDLE":
            print(">>> tap - waking up & listening <<<")
            self.start_listening()

    def start_listening(self):
        self._drain_mic_queue()
        self.rec_chunks = []
        self.rec_bytes = 0
        self.vad_speech_started = False
        self.vad_listen_start = time.monotonic()
        self.vad_last_speech = 0.0
        self.vad_frames_over = 0
        self._last_rms_log = 0.0
        self.listening = True
        self.state = "LISTENING"
        print("Listening...")

    def _drain_mic_queue(self):
        # Chunks already enqueued before this listen started (leftover backlog from the previous
        # turn, since production can outpace vad_tick()'s ~20ms poll cadence) must not bleed into
        # the new recording - otherwise the tail of the last utterance gets spliced onto the front
        # of this one.
        try:
            while True:
                self.mic_q.get_nowait()
        except queue.Empty:
            pass

    # ---- VAD (mirrors vadListenTick()) -----------------------------------------------------------

    async def vad_tick(self):
        # Drain everything currently queued (not just one chunk) so consumption can't lag behind
        # the mic's production rate and build up a backlog that bleeds into the next turn.
        try:
            while True:
                chunk = self.mic_q.get_nowait()
                should_send = self._process_chunk(chunk)
                if should_send:
                    await self.send_audio()
                    return
                if not self.listening:  # VAD timeout fired inside _process_chunk
                    return
        except queue.Empty:
            pass

    def _process_chunk(self, chunk: np.ndarray) -> bool:
        """Returns True if the buffered recording should now be sent."""
        n = min(len(chunk) * 2, RECORD_BUFFER_BYTES - self.rec_bytes)
        if n > 0:
            data = chunk.tobytes()[:n]
            self.rec_chunks.append(data)
            self.rec_bytes += len(data)

        rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2))) if len(chunk) else 0.0
        now = time.monotonic()
        if now - self._last_rms_log > 0.5:
            self._last_rms_log = now
            print(f"  [vad] rms={rms:.0f}" + ("" if self.vad_speech_started else " (waiting for speech)"))

        if rms > self.args.vad_threshold:
            self.vad_frames_over += 1
            # Require a few consecutive over-threshold chunks (not just one) before committing to
            # "speech started" - a laptop mic's noise floor is far spikier than the INMP441's, so a
            # single stray chunk (a click, a fan gust) would otherwise false-trigger constantly.
            if not self.vad_speech_started and self.vad_frames_over >= self.args.vad_min_frames:
                print(f"[VAD] speech detected (rms={rms:.0f})")
                self.vad_speech_started = True
            if self.vad_speech_started:
                self.vad_last_speech = now
        else:
            self.vad_frames_over = 0

        hard_cap_hit = self.rec_bytes >= RECORD_BUFFER_BYTES
        silence_ms = (now - self.vad_last_speech) * 1000 if self.vad_speech_started else 0
        if hard_cap_hit or (self.vad_speech_started and silence_ms > self.args.vad_silence_ms):
            self.listening = False
            print("<<< VAD SEND (hit hard cap)" if hard_cap_hit else "<<< VAD SEND (2s pause)")
            return True

        if not self.vad_speech_started and (now - self.vad_listen_start) * 1000 > self.args.vad_timeout_ms:
            self.listening = False
            self.state = "IDLE"
            print("<<< VAD TIMEOUT - no speech detected, back to idle")

        return False

    async def send_audio(self):
        self.state = "PROCESSING"
        pcm = b"".join(self.rec_chunks)
        duration = len(pcm) / 2 / SAMPLE_RATE
        print(f"  Sending {duration:.1f}s of audio...")

        wav_bytes = to_wav_bytes(pcm)
        for i in range(0, len(wav_bytes), 4096):
            await self.ws.send(wav_bytes[i : i + 4096])
        await self.ws.send(json.dumps({"type": "end"}))

        # listening is already False (set by the caller before send_audio() was awaited), so the
        # mic callback stopped enqueuing - but flush anyway in case a chunk or two landed in the
        # queue right at that boundary, so none of it can leak into the *next* listen.
        self._drain_mic_queue()

        self.waiting_for_reply = True
        # Don't touch ignore_incoming_audio here: if this new turn's request goes out before the
        # *previous* (barge-in-interrupted) turn's audio_end/error has arrived on the wire, resetting
        # it now would let that old turn's trailing bytes leak into this turn's fresh player_q below,
        # and then its audio_end would be mistaken for this turn's completion. It's only ever cleared
        # in recv_loop() when the interrupted turn's own audio_end/error actually shows up.
        self.player_q, self.player_thread = self._start_player()

    # ---- TTS reply playback (mirrors drainTtsRing()) ---------------------------------------------

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
            return  # already handled elsewhere (barge-in) - don't double-resume
        self.waiting_for_reply = False
        print("[reply complete]")
        # Per the target flow, a finished regular reply re-enters Listening, not Idle.
        self.start_listening()

    # ---- device actions: radio / download_song (mirror startRadio()/downloadAndPlaySong()) ------

    def begin_music_state(self, kind: str):
        self.stop_player()  # abandon any leftover reply audio, same as musicPlaying pre-empting drainTtsRing()
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
        # The real device reads this straight off its SD card; this host simulator has no SD card
        # to read from, so it just logs the request instead of actually playing anything.
        self.begin_music_state("song")
        print(f"[song] play_song request: {title} ({path})")
        print("  (no SD card on this host - path logged only, not played)")

    def stop_music(self):
        if self.music_proc and self.music_proc.poll() is None:
            self.music_proc.terminate()
        self.music_proc = None
        self.music_playing = False
        self.music_kind = None

    async def download_and_play_song(self, url: str, title: str):
        self.begin_music_state("song")
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
            return  # stopped (touch/barge-in) while the download was in flight
        if HAS_FFPLAY:
            self.music_proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp.name],
                stdin=subprocess.DEVNULL,
            )
        else:
            print("  (ffplay not found on PATH - install ffmpeg to actually hear this song)")

    # ---- WS receive loop (mirrors webSocketEvent()) ----------------------------------------------

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

            # audio_end/error are the two possible ways a turn ends - one of them always arrives,
            # so this is the only place ignore_incoming_audio gets reset. Handled before the ignore
            # gate below (unlike transcript/reply/radio/etc, which belong entirely to the turn we
            # already walked away from and must never be acted on once ignoring).
            if t == "audio_end":
                if self.ignore_incoming_audio:
                    self.ignore_incoming_audio = False
                    print("(discarded remainder of interrupted reply)")
                elif self.player_q:
                    self.player_q.put(None)
                else:
                    # Reply had no audio bytes at all (e.g. errored before TTS) - nothing queued to drain.
                    self.waiting_for_reply = False
                    self.start_listening()
                continue
            if t == "error":
                if self.ignore_incoming_audio:
                    self.ignore_incoming_audio = False
                    print("(discarded error from interrupted reply)")
                else:
                    print(f"  Error: {data.get('detail')}", file=sys.stderr)
                    self.waiting_for_reply = False
                    self.stop_player()
                    self.start_listening()
                continue

            if self.ignore_incoming_audio:
                continue  # transcript/reply/device-action for a turn we already barged past

            if t == "transcript":
                print(f"\n  You  : {data['text']}")
            elif t == "reply":
                print(f"  ARIA : {data['text']}")
            elif t == "radio":
                self.start_radio(data.get("url", ""), data.get("name", ""))
            elif t == "stop_radio":
                # Only actually in effect if music/radio is still playing on this end - e.g. a
                # tap-interrupt may have already stopped it locally before this turn's reply (with
                # its queued stop_radio action, sent only after audio_end per protocol) comes back.
                # Forcing state to IDLE unconditionally here would stomp "SPEAKING" while the current
                # reply's TTS audio is still draining, and on_playback_complete()'s guard would then
                # see state != "SPEAKING" and silently skip resuming listening once playback finishes.
                if self.music_playing:
                    self.stop_music()
                    self.state = "IDLE"
            elif t == "download_song":
                asyncio.create_task(self.download_and_play_song(data.get("url", ""), data.get("title", "")))
            elif t == "play_song":
                self.play_song(data.get("path", ""), data.get("title", ""))
            elif t == "stop_song":
                # Same caveat as stop_radio above - only in effect if still playing on this end.
                if self.music_playing:
                    self.stop_music()
                    self.state = "IDLE"

    # ---- main tick loop (mirrors loop()'s branching order) ---------------------------------------

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

            if self.listening:
                await self.vad_tick()


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
        print("Press Enter to simulate a touch: start listening / cancel / barge-in / stop music.")
        print("Ctrl+C to quit.\n")

        client = SketchClient(ws, loop, args)

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
    parser = argparse.ArgumentParser(description="Test client mirroring the ESP32 firmware's hands-free voice flow")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--input-device", type=int, default=None, metavar="ID")
    parser.add_argument("--output-device", type=int, default=None, metavar="ID")
    parser.add_argument("--voice", default="en-IN-Neural2-B")
    parser.add_argument("--system-prompt", dest="system_prompt", default=(
        "You are ARIA, a voice assistant for the Kumar family in Hyderabad, India."
    ))
    parser.add_argument("--server", default="http://localhost:8000", metavar="URL")
    parser.add_argument("--vad-threshold", type=int, default=VAD_RMS_THRESHOLD, dest="vad_threshold",
                        help="RMS threshold that counts as speech - host mics vary a lot, tune this by "
                             "watching the live '[vad] rms=...' log while listening")
    parser.add_argument("--vad-silence-ms", type=int, default=VAD_TRAILING_SILENCE_MS, dest="vad_silence_ms")
    parser.add_argument("--vad-timeout-ms", type=int, default=VAD_NO_SPEECH_TIMEOUT_MS, dest="vad_timeout_ms")
    parser.add_argument("--vad-min-frames", type=int, default=3, dest="vad_min_frames",
                         help="consecutive over-threshold mic chunks (~16ms each) required before "
                              "committing to 'speech started' - guards against single noise spikes "
                              "on a laptop mic's spikier noise floor")
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
