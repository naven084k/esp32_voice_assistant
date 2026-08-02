"""
Downloads a song via yt-dlp (YouTube search + audio extraction, transcoded to MP3 by yt-dlp's
own ffmpeg postprocessor) into a local temp directory, and tracks it as "pending" until the ESP32
confirms it wrote the file to SD card - only then does the local song index get updated, and the
temp copy get deleted. See services/tools.py's download_song and routers/voice.py's download_ack
handling for the rest of the flow.
"""
import glob
import logging
import os
import re
import tempfile
import time
import uuid

from services import song_index

logger = logging.getLogger("voice_agent.yt_song")

TEMP_DIR = os.environ.get("SONG_TEMP_DIR") or os.path.join(tempfile.gettempdir(), "aria_song_downloads")
os.makedirs(TEMP_DIR, exist_ok=True)

MAX_AGE_SECONDS = int(os.environ.get("SONG_TEMP_MAX_AGE_SECONDS", "21600"))  # 6h
MAX_FILESIZE_BYTES = 20 * 1024 * 1024  # 20MB - safety net against a mis-ranked, oversized result
MAX_DURATION_SECONDS = 15 * 60  # skip full albums/compilations a search might mis-rank to #1

# thread_id -> {download_id, temp_path, sd_filename, title, created_at} for downloads still
# awaiting an ESP32 write-confirmation. Plain dict (not a ContextVar) for the same reason
# services.llm's _pending_device_actions is: set from inside a tool call, read later from the WS
# handler, potentially in a different asyncio Task.
_pending: dict[str, dict] = {}


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def _sweep_stale(max_age_seconds: int = MAX_AGE_SECONDS) -> None:
    """Deletes any file in TEMP_DIR older than max_age_seconds, regardless of _pending state -
    catches downloads whose ack never arrived (crash, dropped connection) and survives process
    restarts (the in-memory registry doesn't). No scheduler exists in this codebase, so this is
    called opportunistically at the start of every search_and_download() - an orphaned file can
    sit for up to max_age_seconds of *wall time* if no further downloads are requested in the
    meantime; accepted as a reasonable bound rather than adding a background job for this."""
    cutoff = time.time() - max_age_seconds
    for path in glob.glob(os.path.join(TEMP_DIR, "*")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


async def search_and_download(query: str) -> dict | None:
    """Searches YouTube for `query` and downloads+transcodes the best match to an MP3 in TEMP_DIR.
    Returns {"download_id", "temp_path", "title", "sd_filename"} on success, None if nothing was
    found or the download/transcode failed."""
    import asyncio

    _sweep_stale()

    download_id = uuid.uuid4().hex

    def _run() -> dict | None:
        import yt_dlp

        outtmpl = os.path.join(TEMP_DIR, f"{download_id}.%(ext)s")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "socket_timeout": 20,
            "max_filesize": MAX_FILESIZE_BYTES,
            "match_filter": yt_dlp.utils.match_filter_func(f"duration < {MAX_DURATION_SECONDS}"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "postprocessor_args": {"ffmpeg": ["-ar", "44100", "-ac", "2"]},
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=True)
        except Exception as e:
            logger.warning(f"[yt_song] download failed for '{query}': {e}")
            for leftover in glob.glob(os.path.join(TEMP_DIR, f"{download_id}.*")):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            return None

        entries = list(info.get("entries") or []) if info else []
        entry = entries[0] if entries else info
        if not entry:
            return None

        title = entry.get("title") or query
        temp_path = os.path.join(TEMP_DIR, f"{download_id}.mp3")
        if not os.path.isfile(temp_path):
            return None

        return {
            "download_id": download_id,
            "temp_path": temp_path,
            "title": title,
            "sd_filename": f"{_slugify(title)}.mp3",
        }

    return await asyncio.to_thread(_run)


def register_pending(download_id: str, *, temp_path: str, sd_filename: str, title: str, thread_id: str) -> None:
    _pending[download_id] = {
        "temp_path": temp_path,
        "sd_filename": sd_filename,
        "title": title,
        "thread_id": thread_id,
        "created_at": time.time(),
    }


def get_temp_path(download_id: str) -> str | None:
    entry = _pending.get(download_id)
    return entry["temp_path"] if entry else None


_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


def _is_local_host(host: str) -> bool:
    # A real ESP32 is a separate physical device - it can never reach the backend via
    # "localhost"/"127.0.0.1", only through a real hostname/IP (in practice, a cloudflared tunnel
    # hostname, which is always https). So a Host header of "localhost:8000" unambiguously means
    # this is a local test harness (tests/sketch_client.py, tests/mic_client.py) hitting a plain
    # `uvicorn main:app --reload` dev server - use http there rather than attempting (and failing)
    # a TLS handshake against a server that isn't serving TLS at all.
    hostname = host.split(":")[0]
    return hostname in _LOCAL_HOSTNAMES


def build_download_url(thread_id: str, download_id: str) -> str | None:
    """Builds the URL the ESP32 should fetch to download the temp MP3. Prefers PUBLIC_BASE_URL
    if set; otherwise derives from the Host header of the WS connection the device is already
    talking to us through (see services.llm.set_ws_host/get_ws_host) - necessary because the
    ESP32 only ever reaches the backend through a cloudflared tunnel hostname that rotates on
    restart, so there's no fixed "our own address" to hardcode."""
    from services.llm import get_ws_host

    base = os.environ.get("PUBLIC_BASE_URL") or ""
    base = base.rstrip("/")
    if not base:
        host = get_ws_host(thread_id)
        if not host:
            return None
        scheme = "http" if _is_local_host(host) else "https"
        base = f"{scheme}://{host}"
    return f"{base}/api/media/{download_id}.mp3"


def confirm_download(download_id: str, success: bool) -> None:
    """Called once the ESP32 acks a download (success or failure). On success, adds the song to
    the local index; either way, deletes the backend's own temp copy."""
    entry = _pending.pop(download_id, None)
    if not entry:
        return

    if success:
        try:
            song_index.add_song(entry["title"], entry["sd_filename"])
        except Exception as e:
            logger.error(f"[yt_song] add_song failed for '{entry['title']}': {e}")

    try:
        os.remove(entry["temp_path"])
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"[yt_song] failed to remove temp file {entry['temp_path']}: {e}")


def cleanup_thread(thread_id: str) -> None:
    """Deletes temp files/registry entries for any downloads still pending for this thread_id
    (e.g. the WS connection dropped before an ack arrived) - never touches the song index, since
    no confirmation was received."""
    stale_ids = [did for did, entry in _pending.items() if entry["thread_id"] == thread_id]
    for download_id in stale_ids:
        entry = _pending.pop(download_id, None)
        if not entry:
            continue
        try:
            os.remove(entry["temp_path"])
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"[yt_song] failed to remove temp file {entry['temp_path']}: {e}")
