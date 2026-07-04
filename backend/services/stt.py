import asyncio
import os
import tempfile
import time
from faster_whisper import WhisperModel
from services.request_timer import get_timer

_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel("base", device="auto", compute_type="int8")
    return _model


def _run_transcribe(audio_bytes: bytes, filename: str) -> str:
    suffix = os.path.splitext(filename)[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name
    try:
        segments, _ = _get_model().transcribe(tmp_path, beam_size=5)
        return " ".join(seg.text.strip() for seg in segments).strip()
    finally:
        os.unlink(tmp_path)


async def transcribe(audio_bytes: bytes, filename: str = "audio.wav") -> str:
    start = time.perf_counter()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run_transcribe, audio_bytes, filename)
    elapsed = time.perf_counter() - start
    t = get_timer()
    if t:
        t.record("STT (whisper/base)", elapsed, f'"{result[:40]}"')
    return result
