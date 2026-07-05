"""
Manual WebSocket voice pipeline tester.

Usage:
    python tests/ws_client.py path/to/audio.wav [--voice nova] [--system-prompt "You are a pirate."]

Output:
    Prints transcript and LLM reply, saves TTS response to response.mp3
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import websockets


async def run(audio_path: str, voice: str, system_prompt: str):
    uri = "ws://localhost:8000/api/ws/voice"
    audio_bytes = Path(audio_path).read_bytes()

    print(f"Connecting to {uri} ...")
    async with websockets.connect(uri, max_size=10 * 1024 * 1024) as ws:
        # Optional config
        await ws.send(json.dumps({"type": "config", "voice": voice, "system_prompt": system_prompt}))

        # Stream audio in 4 KB chunks
        chunk_size = 4096
        for i in range(0, len(audio_bytes), chunk_size):
            await ws.send(audio_bytes[i : i + chunk_size])
        print(f"Sent {len(audio_bytes):,} bytes of audio in {-(-len(audio_bytes) // chunk_size)} chunks")

        # Signal end of audio
        await ws.send(json.dumps({"type": "end"}))

        # Collect response
        audio_out = bytearray()
        while True:
            msg = await ws.recv()
            if isinstance(msg, bytes):
                audio_out.extend(msg)
            else:
                data = json.loads(msg)
                if data["type"] == "transcript":
                    print(f"\nTranscript : {data['text']}")
                elif data["type"] == "reply":
                    print(f"LLM reply  : {data['text']}")
                elif data["type"] == "audio_end":
                    break
                elif data["type"] == "error":
                    print(f"ERROR: {data['detail']}", file=sys.stderr)
                    break

        if audio_out:
            import io, wave
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(bytes(audio_out))
            out_path = "response.wav"
            Path(out_path).write_bytes(buf.getvalue())
            print(f"\nAudio saved: {out_path} ({len(audio_out):,} bytes PCM → {len(buf.getvalue()):,} bytes WAV)")


def main():
    parser = argparse.ArgumentParser(description="WebSocket voice pipeline tester")
    parser.add_argument("audio", help="Path to audio file (WAV, MP3, etc.)")
    parser.add_argument("--voice", default="en-IN-Neural2-B",
                        choices=["en-US-Neural2-A", "en-US-Neural2-C", "en-US-Neural2-D",
                                 "en-US-Neural2-F", "en-US-Neural2-H", "en-US-Neural2-I",
                                 "en-GB-Neural2-A", "en-GB-Neural2-B",
                                 "en-IN-Neural2-A", "en-IN-Neural2-B",
                                 "en-IN-Neural2-C", "en-IN-Neural2-D"])
    parser.add_argument("--system-prompt", default="You are a helpful voice assistant.", dest="system_prompt")
    args = parser.parse_args()

    asyncio.run(run(args.audio, args.voice, args.system_prompt))


if __name__ == "__main__":
    main()
