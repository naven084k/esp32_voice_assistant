"""
Automated smoke tests — no live server or API key needed (Gemini calls are mocked).

Run:
    pytest tests/test_client.py -v
"""
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("GOOGLE_GEMINI_API_KEY", "test-key")
os.environ.setdefault("GOOGLE_TTS_API_KEY", "test-key")

from httpx import AsyncClient, ASGITransport
from main import app

FIXTURE_WAV = Path(__file__).parent / "fixtures" / "sample.wav"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_chat(client):
    with patch("routers.chat.llm.ainvoke", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value.content = "Hello! How can I help?"
        resp = await client.post("/api/chat", json={"message": "Hi"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "Hello! How can I help?"


@pytest.mark.anyio
async def test_voice_chat(client):
    with (
        patch("routers.voice.stt.transcribe", new_callable=AsyncMock) as mock_stt,
        patch("routers.voice.llm.process", new_callable=AsyncMock) as mock_llm,
        patch("routers.voice.tts.synthesize_stream") as mock_tts,
    ):
        mock_stt.return_value = "Hello from audio"
        mock_llm.return_value = "Hi there!"

        async def fake_stream(*_args, **_kwargs):
            yield b"fake-mp3-bytes"

        mock_tts.return_value = fake_stream()

        with open(FIXTURE_WAV, "rb") as f:
            resp = await client.post(
                "/api/voice/chat",
                files={"audio": ("sample.wav", f, "audio/wav")},
            )

    assert resp.status_code == 200
    assert resp.headers["x-transcript"] == "Hello from audio"
    assert resp.headers["x-reply"] == "Hi there!"
    assert resp.content == b"fake-mp3-bytes"


@pytest.mark.anyio
async def test_voice_chat_unsupported_type(client):
    resp = await client.post(
        "/api/voice/chat",
        files={"audio": ("test.txt", b"not audio", "text/plain")},
    )
    assert resp.status_code == 415
