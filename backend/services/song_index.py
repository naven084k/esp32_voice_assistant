"""
Local song library lookup - matches spoken text against a pre-built JSON index
(`data/songs_index.json`) of the tracks already on the ESP32's SD card, and resolves a match to its
on-card path. Unlike `radio.py`/Tavily-backed `download_song`, this never hits the network - the
index and the SD layout are both static, prepared ahead of time.
"""
import difflib
import json
import os
import re
from functools import lru_cache

SONGS_INDEX_PATH = os.environ.get(
    "SONGS_INDEX_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "songs_index.json")
)
SONGS_ROOT = os.environ.get("SONGS_ROOT", "/Naveen/songs").rstrip("/")

_MATCH_THRESHOLD = 0.45


@lru_cache(maxsize=1)
def _load_songs() -> list[dict]:
    with open(SONGS_INDEX_PATH, encoding="utf-8") as f:
        return json.load(f)["songs"]


def _song_path(song: dict) -> str:
    # Optional per-song override: set "path" (relative to SONGS_ROOT) in songs_index.json for any
    # entry whose real on-card filename doesn't cleanly match "{album}/{title}.mp3" - e.g. files
    # kept with their original download-site names (track number prefix, "[www.site.com]" suffix)
    # or sitting flat in SONGS_ROOT with no album subfolder.
    if song.get("path"):
        return f"{SONGS_ROOT}/{song['path']}"
    return f"{SONGS_ROOT}/{song['album']}/{song['title']}.mp3"


def _score(query: str, song: dict) -> float:
    title_ratio = difflib.SequenceMatcher(None, query, song["title"].lower()).ratio()
    query_words = set(query.split())
    keywords = {k.lower() for k in song.get("keywords", [])}
    overlap = len(query_words & keywords) / len(query_words) if query_words else 0.0
    return max(title_ratio, overlap)


def _to_result(song: dict) -> dict:
    return {"title": song["title"], "album": song["album"], "path": _song_path(song)}


def downloads_path(filename: str) -> str:
    """Absolute SD-card path for a file that will live under the Downloads album, before it's
    necessarily been added to the index - lets a caller build the on-card path for a device
    action ahead of add_song() actually running (e.g. once download confirmation arrives)."""
    return f"{SONGS_ROOT}/Downloads/{filename}"


def add_song(title: str, filename: str) -> dict:
    """Append a new song to the index and return the entry. Clears the LRU cache so subsequent
    find_song() calls see it immediately."""
    with open(SONGS_INDEX_PATH, encoding="utf-8") as f:
        data = json.load(f)

    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    song_id = f"downloads_{slug}"

    for existing in data["songs"]:
        if existing["id"] == song_id:
            return _to_result(existing)

    entry = {
        "id": song_id,
        "title": title,
        "album": "Downloads",
        "path": f"Downloads/{filename}",
        "language": "Unknown",
        "category": "Downloaded",
        "genre": [],
        "moods": [],
        "themes": [],
        "energy": "medium",
        "tempo": "medium",
        "description": f"Downloaded track: {title}",
        "keywords": [w for w in title.lower().split() if len(w) > 1],
        "voice_aliases": [
            f"play {title}",
            f"play {title.lower()}",
        ],
    }
    data["songs"].append(entry)
    data["count"] = len(data["songs"])

    with open(SONGS_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    _load_songs.cache_clear()
    return _to_result(entry)


def find_song(query: str) -> dict | None:
    """Matches `query` (raw spoken text, e.g. 'play O Rangula Chilaka') against the song index.
    Returns {"title", "album", "path"} for the best match, or None if nothing matches well enough."""
    query = query.strip().lower()
    if not query:
        return None
    songs = _load_songs()

    for song in songs:
        aliases = [a.lower() for a in song.get("voice_aliases", [])]
        if query in aliases or any(query in a or a in query for a in aliases):
            return _to_result(song)

    for song in songs:
        title = song["title"].lower()
        if query in title or title in query:
            return _to_result(song)

    best, best_score = None, 0.0
    for song in songs:
        score = _score(query, song)
        if score > best_score:
            best, best_score = song, score
    return _to_result(best) if best and best_score >= _MATCH_THRESHOLD else None


def find_songs(query: str, limit: int = 10) -> list[dict]:
    """Like find_song(), but returns up to `limit` matches ordered by relevance instead of just
    the single best one - for playlist-style requests ('play some romantic songs', 'play telugu
    melodies') that a single title/alias lookup would under-serve since many songs can plausibly
    match. Alias/title hits are ranked above fuzzy-score hits; each song appears at most once."""
    query = query.strip().lower()
    if not query:
        return []
    songs = _load_songs()

    scored: list[tuple[float, dict]] = []
    for song in songs:
        aliases = [a.lower() for a in song.get("voice_aliases", [])]
        title = song["title"].lower()
        if query in aliases or any(query in a or a in query for a in aliases):
            score = 1.0
        elif query in title or title in query:
            score = 0.95
        else:
            score = _score(query, song)
        if score >= _MATCH_THRESHOLD:
            scored.append((score, song))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [_to_result(song) for _, song in scored[:limit]]
