import base64
import io
import os
import time
import wave

import httpx

from services.request_timer import get_timer

_STT_URL = "https://speech.googleapis.com/v1/speech:recognize"
_LANGUAGE_CODE = "en-IN"           # matches default TTS voice locale (en-IN-Neural2-B)
_ESP32_SAMPLE_RATE = 16000         # esp32/*.ino: headerless raw PCM, no WAV wrapper
_TELEGRAM_OPUS_SAMPLE_RATE = 48000 # Telegram voice notes: OGG/Opus, libopus default

STT_PROVIDER = os.environ.get("STT_PROVIDER", "google").lower()
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "base")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
_whisper_model = None


def _api_key() -> str:
    key = os.environ.get("GOOGLE_TTS_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_TTS_API_KEY is not set — required for Google Cloud Speech-to-Text.")
    return key


def _build_recognition_config(audio_bytes: bytes) -> tuple[dict, bytes]:
    if audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            rate = wf.getframerate()
            channels = wf.getnchannels()
            pcm = wf.readframes(wf.getnframes())
        return {
            "encoding": "LINEAR16",
            "sampleRateHertz": rate,
            "audioChannelCount": channels,
            "languageCode": _LANGUAGE_CODE,
            "model": "latest_short",
        }, pcm

    if audio_bytes[:4] == b"OggS":
        return (
            {
                "encoding": "OGG_OPUS",
                "sampleRateHertz": _TELEGRAM_OPUS_SAMPLE_RATE,
                "audioChannelCount": 1,
                "languageCode": _LANGUAGE_CODE,
                "model": "latest_short",
            },
            audio_bytes,
        )

    return {
        "encoding": "LINEAR16",
        "sampleRateHertz": _ESP32_SAMPLE_RATE,
        "audioChannelCount": 1,
        "languageCode": _LANGUAGE_CODE,
        "model": "latest_short",
    }, audio_bytes


async def _google_transcribe(audio_bytes: bytes) -> str:
    config, content = _build_recognition_config(audio_bytes)
    payload = {"config": config, "audio": {"content": base64.b64encode(content).decode("ascii")}}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(_STT_URL, params={"key": _api_key()}, json=payload)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])
    return " ".join(
        r["alternatives"][0]["transcript"].strip() for r in results if r.get("alternatives")
    ).strip()


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type="int8")
    return _whisper_model


def _pcm16_to_float32(pcm: bytes):
    import numpy as np
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def _resample_linear(samples, orig_rate: int, target_rate: int):
    import numpy as np
    if orig_rate == target_rate:
        return samples
    target_len = int(len(samples) * target_rate / orig_rate)
    return np.interp(
        np.linspace(0, len(samples), target_len, endpoint=False), np.arange(len(samples)), samples
    ).astype(np.float32)


def _local_transcribe_sync(audio_bytes: bytes) -> str:
    model = _get_whisper_model()
    if audio_bytes[:4] == b"OggS":
        segments, _ = model.transcribe(io.BytesIO(audio_bytes), language="en")
    else:
        if audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                rate, pcm = wf.getframerate(), wf.readframes(wf.getnframes())
            samples = _resample_linear(_pcm16_to_float32(pcm), rate, 16000)
        else:
            samples = _pcm16_to_float32(audio_bytes)  # ESP32: already 16kHz mono
        segments, _ = model.transcribe(samples, language="en")
    return " ".join(s.text.strip() for s in segments).strip()


async def _local_transcribe(audio_bytes: bytes) -> str:
    import asyncio
    return await asyncio.to_thread(_local_transcribe_sync, audio_bytes)


async def transcribe(audio_bytes: bytes, filename: str = "audio.wav") -> str:
    start = time.perf_counter()
    if STT_PROVIDER == "local":
        result = await _local_transcribe(audio_bytes)
        label = "STT (local Whisper)"
    else:
        result = await _google_transcribe(audio_bytes)
        label = "STT (Google Cloud)"
    elapsed = time.perf_counter() - start
    t = get_timer()
    if t:
        t.record(label, elapsed, f'"{result[:40]}"')
    return result
