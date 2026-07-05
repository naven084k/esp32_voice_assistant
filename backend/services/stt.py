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


async def _run_transcribe(audio_bytes: bytes) -> str:
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


async def transcribe(audio_bytes: bytes, filename: str = "audio.wav") -> str:
    start = time.perf_counter()
    result = await _run_transcribe(audio_bytes)
    elapsed = time.perf_counter() - start
    t = get_timer()
    if t:
        t.record("STT (Google Cloud)", elapsed, f'"{result[:40]}"')
    return result
