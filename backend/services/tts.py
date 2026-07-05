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

VOICES = {
    "en-US-Neural2-A", "en-US-Neural2-C", "en-US-Neural2-D",
    "en-US-Neural2-F", "en-US-Neural2-H", "en-US-Neural2-I",
    "en-GB-Neural2-A", "en-GB-Neural2-B",
    "en-IN-Neural2-A", "en-IN-Neural2-B", "en-IN-Neural2-C", "en-IN-Neural2-D",
}

_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
_MAX_CHARS = 1000  # Google Cloud TTS accepts up to 5,000 bytes of input per request
_CHUNK_BYTES = 32768


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
        "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": SAMPLE_RATE},
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_TTS_URL, params={"key": _api_key()}, json=payload)
        resp.raise_for_status()
        wav_bytes = base64.b64decode(resp.json()["audioContent"])

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return wf.readframes(wf.getnframes())


async def synthesize_stream(text: str, voice: str = DEFAULT_VOICE) -> AsyncIterator[bytes]:
    if voice not in VOICES:
        voice = DEFAULT_VOICE

    start = time.perf_counter()
    total_bytes = 0
    first_chunk_elapsed = None

    for sentence in _split_sentences(text):
        pcm = await _synthesize_pcm(sentence, voice)
        for i in range(0, len(pcm), _CHUNK_BYTES):
            chunk = pcm[i : i + _CHUNK_BYTES]
            if first_chunk_elapsed is None:
                first_chunk_elapsed = time.perf_counter() - start
            total_bytes += len(chunk)
            yield chunk

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
