import asyncio
import io
import re
import time
import urllib.request
import wave
from pathlib import Path
from typing import AsyncIterator

import numpy as np
from kokoro_onnx import Kokoro

from services.request_timer import get_timer

SAMPLE_RATE = 24000
DEFAULT_VOICE = "am_adam"

MODELS_DIR = Path("models")
_MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODEL_FILE  = MODELS_DIR / "kokoro-v1.0.onnx"
VOICES_FILE = MODELS_DIR / "voices-v1.0.bin"

VOICES = {
    "af_bella", "af_sarah", "af_sky", "af_nicole",
    "am_adam", "am_michael",
    "bf_emma", "bf_isabella",
    "bm_george", "bm_lewis",
}

_kokoro: Kokoro | None = None


def _download_if_missing():
    MODELS_DIR.mkdir(exist_ok=True)
    for dest, name in [(MODEL_FILE, "kokoro-v1.0.onnx"), (VOICES_FILE, "voices-v1.0.bin")]:
        if not dest.exists():
            urllib.request.urlretrieve(f"{_MODEL_URL}/{name}", dest)


_MAX_CHARS = 300  # Kokoro's 510-token limit ≈ ~300 characters safely


def _get_kokoro() -> Kokoro:
    global _kokoro
    if _kokoro is None:
        _download_if_missing()
        _kokoro = Kokoro(str(MODEL_FILE), str(VOICES_FILE))
    return _kokoro


def _split_sentences(text: str) -> list[str]:
    """Split text into sentence-sized chunks under _MAX_CHARS each."""
    # Split on sentence boundaries
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


async def synthesize_stream(text: str, voice: str = DEFAULT_VOICE) -> AsyncIterator[bytes]:
    if voice not in VOICES:
        voice = DEFAULT_VOICE

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _get_kokoro)

    start = time.perf_counter()
    total_bytes = 0
    CHUNK_BYTES = 32768

    for sentence in _split_sentences(text):
        async for samples, _ in _get_kokoro().create_stream(sentence, voice=voice, speed=1.0, lang="en-us"):
            pcm = (np.array(samples, dtype=np.float32) * 32767).clip(-32768, 32767).astype(np.int16)
            raw = pcm.tobytes()
            for i in range(0, len(raw), CHUNK_BYTES):
                chunk = raw[i : i + CHUNK_BYTES]
                total_bytes += len(chunk)
                yield chunk

    elapsed = time.perf_counter() - start
    t = get_timer()
    if t:
        t.record("TTS (Kokoro)", elapsed, f"{total_bytes:,} bytes  voice={voice}")


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
