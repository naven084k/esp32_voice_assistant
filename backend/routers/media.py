import os

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import FileResponse

from services.yt_song import get_temp_path

router = APIRouter()

# uuid4 hex - rejects "/", "..", and any non-hex input before it ever reaches the filesystem.
_DOWNLOAD_ID_PATTERN = r"^[0-9a-f]{32}$"


@router.get("/media/{download_id}.mp3")
async def get_temp_song(download_id: str = Path(pattern=_DOWNLOAD_ID_PATTERN)):
    """Serves a yt-dlp-downloaded MP3 that's still pending ESP32 write-confirmation (see
    services/yt_song.py). Deliberately unauthenticated, same as /ws/voice - the ESP32 has no way
    to attach the dashboard's auth header. Only files currently registered as pending are servable
    (an id that's well-formed but not registered, e.g. already confirmed/expired, 404s) - this is
    the actual access control, not just the id's unguessability."""
    temp_path = get_temp_path(download_id)
    if not temp_path or not os.path.isfile(temp_path):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(temp_path, media_type="audio/mpeg")
