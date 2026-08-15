import asyncio
import base64
import io
import os
import re
import time
import wave
from typing import AsyncIterator

import httpx

from services.request_timer import get_timer

SAMPLE_RATE = 24000
DEFAULT_VOICE = "en-IN-Neural2-B"
VOLUME_GAIN_DB = 10.0  # Google's neural voices synthesize quieter than OpenAI's TTS by default (0dB); boost at the source rather than relying solely on the ESP32's digital gain, which clips more easily. Range: -96.0 to 16.0.

VOICES = {
    "en-US-Neural2-A", "en-US-Neural2-C", "en-US-Neural2-D",
    "en-US-Neural2-F", "en-US-Neural2-H", "en-US-Neural2-I",
    "en-GB-Neural2-A", "en-GB-Neural2-B",
    "en-IN-Neural2-A", "en-IN-Neural2-B", "en-IN-Neural2-C", "en-IN-Neural2-D",
    "en-IN-Chirp3-HD-Orus", "en-IN-Chirp3-HD-Aoede", "en-IN-Chirp3-HD-Kore",
}

_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
_MAX_CHARS = 1000  # Google Cloud TTS accepts up to 5,000 bytes of input per request
_CHUNK_BYTES = 8192  # ESP32 "WebSockets" client hard-caps incoming frames at WEBSOCKETS_MAX_DATA_SIZE (15*1024);
# anything larger gets silently disconnected (close code 1009) before the sketch's callback even runs.

_BYTES_PER_SECOND = SAMPLE_RATE * 2  # 16-bit mono PCM real-time playback rate (48000 B/s)
_MAX_LEAD_SECONDS = 2.0  # allow the backend to run this far ahead of real-time before throttling

TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "google").lower()
LOCAL_TTS_VOICE = os.environ.get("LOCAL_TTS_VOICE", "").strip()


def _api_key() -> str:
    key = os.environ.get("GOOGLE_TTS_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_TTS_API_KEY is not set — required for Google Cloud TTS.")
    return key


def _language_code(voice: str) -> str:
    parts = voice.split("-")
    return "-".join(parts[:2])


def _split_sentences(text: str) -> list[str]:
    """Split text into sentence-sized chunks under _MAX_CHARS each."""
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks, current = [], ""
    for part in parts:
        if not part:
            continue
        if len(current) + len(part) + 1 <= _MAX_CHARS:
            current = f"{current} {part}".strip()
        else:
            if current:
                chunks.append(current)
            # If a single sentence is still too long, hard-split it
            while len(part) > _MAX_CHARS:
                chunks.append(part[:_MAX_CHARS])
                part = part[_MAX_CHARS:]
            current = part
    if current:
        chunks.append(current)
    return chunks or [text[:_MAX_CHARS]]


async def _synthesize_pcm(text: str, voice: str) -> bytes:
    payload = {
        "input": {"text": text},
        "voice": {"languageCode": _language_code(voice), "name": voice},
        "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": SAMPLE_RATE, "volumeGainDb": VOLUME_GAIN_DB},
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_TTS_URL, params={"key": _api_key()}, json=payload)
        resp.raise_for_status()
        wav_bytes = base64.b64decode(resp.json()["audioContent"])

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return wf.readframes(wf.getnframes())


def _read_ieee_extended(data: bytes) -> float:
    """Decode the 80-bit big-endian IEEE extended float used by AIFF's COMM.sampleRate."""
    expon = ((data[0] & 0x7F) << 8) | data[1]
    sign = -1 if data[0] & 0x80 else 1
    himant = int.from_bytes(data[2:6], "big")
    lomant = int.from_bytes(data[6:10], "big")
    if expon == 0 and himant == 0 and lomant == 0:
        return 0.0
    expon -= 16383
    return sign * (himant * 4294967296.0 + lomant) * (2.0 ** (expon - 63))


def _aiff_to_wav(data: bytes) -> bytes:
    """macOS's NSSpeechSynthesizer writes AIFF-C with 'twos' (big-endian PCM) compression —
    Python's stdlib `aifc` module rejects that compression tag, and the `chunk` module was
    removed in Python 3.13+, so parse the IFF chunk structure by hand with `struct` instead."""
    import array
    import struct

    if data[:4] != b"FORM" or data[8:12] not in (b"AIFF", b"AIFC"):
        raise ValueError("not an AIFF/AIFF-C file")

    nchannels = sampwidth = framerate = None
    frames = b""
    pos = 12
    end = 8 + struct.unpack(">L", data[4:8])[0]
    while pos + 8 <= end:
        name = data[pos:pos + 4]
        size = struct.unpack(">L", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if name == b"COMM":
            nchannels, _nframes, sampwidth_bits = struct.unpack(">hlh", body[:8])
            sampwidth = (sampwidth_bits + 7) // 8
            framerate = int(_read_ieee_extended(body[8:18]))
        elif name == b"SSND":
            offset, _blocksize = struct.unpack(">ll", body[:8])
            frames = body[8 + offset:]
        pos += 8 + size + (size & 1)  # chunks are padded to an even size

    if sampwidth == 2:  # 'twos' is big-endian 16-bit PCM — byteswap to little-endian for WAV
        samples = array.array("h")
        samples.frombytes(frames)
        samples.byteswap()
        frames = samples.tobytes()

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(nchannels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(frames)
    return buf.getvalue()


def _resample_pcm16(pcm: bytes, orig_rate: int, target_rate: int) -> bytes:
    if orig_rate == target_rate:
        return pcm
    import numpy as np
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    target_len = int(len(samples) * target_rate / orig_rate)
    resampled = np.interp(
        np.linspace(0, len(samples), target_len, endpoint=False), np.arange(len(samples)), samples
    )
    return resampled.astype(np.int16).tobytes()


def _local_synthesize_pcm_sync(text: str) -> bytes:
    """Returns raw PCM at SAMPLE_RATE/mono/16-bit — same contract as the Google path,
    so callers (incl. /ws/voice, which assumes headerless raw PCM chunks) work unchanged."""
    import pyttsx3, tempfile
    engine = pyttsx3.init()
    if LOCAL_TTS_VOICE:
        engine.setProperty("voice", LOCAL_TTS_VOICE)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    try:
        engine.save_to_file(text, path)
        engine.runAndWait()
        with open(path, "rb") as fh:
            data = fh.read()
    finally:
        os.unlink(path)

    if data[:4] == b"FORM":  # macOS NSSpeechSynthesizer writes AIFF regardless of the .wav extension
        data = _aiff_to_wav(data)

    with wave.open(io.BytesIO(data), "rb") as wf:
        rate, pcm = wf.getframerate(), wf.readframes(wf.getnframes())
    return _resample_pcm16(pcm, rate, SAMPLE_RATE)


async def _local_synthesize_pcm(text: str) -> bytes:
    # NOTE: not offloaded via asyncio.to_thread — on macOS, pyttsx3's NSSpeechSynthesizer
    # driver must run on the main thread to pump its run loop; off the main thread,
    # runAndWait() returns before synthesis finishes, producing an empty/truncated file.
    return _local_synthesize_pcm_sync(text)


async def _local_synthesize_stream(text: str) -> AsyncIterator[bytes]:
    pcm = await _local_synthesize_pcm(text)
    for i in range(0, len(pcm), _CHUNK_BYTES):
        yield pcm[i : i + _CHUNK_BYTES]


async def _pace_chunks(chunks: AsyncIterator[bytes], start: float, pace: bool) -> AsyncIterator[bytes]:
    """Rate-limit an async byte-chunk stream to ~real-time playback speed.
    `start` = perf_counter() at t=0 of the stream (captured before synthesis begins,
    so synthesis time already counts toward the real-time budget)."""
    sent_bytes = 0
    async for chunk in chunks:
        sent_bytes += len(chunk)
        if pace:
            target_elapsed = sent_bytes / _BYTES_PER_SECOND
            actual_elapsed = time.perf_counter() - start
            lead = target_elapsed - actual_elapsed
            if lead > _MAX_LEAD_SECONDS:
                await asyncio.sleep(lead - _MAX_LEAD_SECONDS)
        yield chunk


async def synthesize_stream(text: str, voice: str = DEFAULT_VOICE, pace: bool = False) -> AsyncIterator[bytes]:
    if TTS_PROVIDER == "local":
        start = time.perf_counter()
        total_bytes = 0
        first_chunk_elapsed = None
        async for chunk in _pace_chunks(_local_synthesize_stream(text), start, pace):
            if first_chunk_elapsed is None:
                first_chunk_elapsed = time.perf_counter() - start
            total_bytes += len(chunk)
            yield chunk
        # NOTE: elapsed includes real-time pacing sleep when pace=True — this is expected
        # (reflects true end-to-end stream duration), not a regression.
        elapsed = time.perf_counter() - start
        t = get_timer()
        if t:
            first_chunk_str = f"{first_chunk_elapsed:.2f}s" if first_chunk_elapsed is not None else "n/a"
            t.record("TTS (local pyttsx3)", elapsed, f"{total_bytes:,} bytes  first_chunk={first_chunk_str}")
        return

    if voice not in VOICES:
        voice = DEFAULT_VOICE

    start = time.perf_counter()
    total_bytes = 0
    first_chunk_elapsed = None

    async def _raw_google_chunks():
        for sentence in _split_sentences(text):
            pcm = await _synthesize_pcm(sentence, voice)
            for i in range(0, len(pcm), _CHUNK_BYTES):
                yield pcm[i : i + _CHUNK_BYTES]

    async for chunk in _pace_chunks(_raw_google_chunks(), start, pace):
        if first_chunk_elapsed is None:
            first_chunk_elapsed = time.perf_counter() - start
        total_bytes += len(chunk)
        yield chunk

    # NOTE: elapsed includes real-time pacing sleep when pace=True — this is expected
    # (reflects true end-to-end stream duration), not a regression.
    elapsed = time.perf_counter() - start
    t = get_timer()
    if t:
        first_chunk_str = f"{first_chunk_elapsed:.2f}s" if first_chunk_elapsed is not None else "n/a"
        t.record("TTS (Google Cloud)", elapsed, f"{total_bytes:,} bytes  voice={voice}  first_chunk={first_chunk_str}")


async def synthesize(text: str, voice: str = DEFAULT_VOICE) -> bytes:
    chunks = []
    async for chunk in synthesize_stream(text, voice):
        chunks.append(chunk)
    pcm = b"".join(chunks)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()
