"""
Local song library lookup - matches spoken text against a SQLite index (`data/songs_index.db`) of
the tracks already on the ESP32's SD card, and resolves a match to its on-card path. Unlike
`radio.py`/Tavily-backed `download_song`, this never hits the network - the index and the SD layout
are both static, prepared ahead of time.

The index used to be a hand-edited JSON file (`SONGS_INDEX_PATH`); on first connection, if the
`songs` table doesn't exist yet and that JSON file is present, its entries are imported once. After
that the JSON file is never read again - edit the library via a SQLite client instead.
"""
import difflib
import json
import os
import re
import sqlite3
from functools import lru_cache

SONGS_INDEX_DB_PATH = os.environ.get(
    "SONGS_INDEX_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "songs_index.db")
)
# Legacy hand-edited JSON index - read only, once, to seed SONGS_INDEX_DB_PATH the first time
# it's created. Irrelevant afterward.
SONGS_INDEX_PATH = os.environ.get(
    "SONGS_INDEX_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "songs_index.json")
)
SONGS_ROOT = os.environ.get("SONGS_ROOT", "/Naveen/songs").rstrip("/")

_MATCH_THRESHOLD = 0.45

_LIST_COLUMNS = ("genre", "moods", "themes", "keywords", "voice_aliases")


def _conn() -> sqlite3.Connection:
    db_path = SONGS_INDEX_DB_PATH
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    existing = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "songs" not in existing:
        c.executescript("""
            CREATE TABLE songs (
                id            TEXT PRIMARY KEY,
                title         TEXT NOT NULL,
                album         TEXT NOT NULL,
                path          TEXT DEFAULT '',
                language      TEXT DEFAULT 'Unknown',
                category      TEXT DEFAULT '',
                genre         TEXT DEFAULT '[]',
                moods         TEXT DEFAULT '[]',
                themes        TEXT DEFAULT '[]',
                energy        TEXT DEFAULT 'medium',
                tempo         TEXT DEFAULT 'medium',
                description   TEXT DEFAULT '',
                keywords      TEXT DEFAULT '[]',
                voice_aliases TEXT DEFAULT '[]'
            );
        """)
        if os.path.isfile(SONGS_INDEX_PATH):
            with open(SONGS_INDEX_PATH, encoding="utf-8") as f:
                legacy_songs = json.load(f)["songs"]
            # A handful of entries in the legacy JSON share the same auto-generated "id" slug
            # despite being distinct tracks (e.g. a Remix/Reprise of the same title) - disambiguate
            # rather than dropping them via INSERT OR IGNORE, which would silently lose real songs.
            seen_ids: dict[str, int] = {}
            for song in legacy_songs:
                song_id = song["id"]
                if song_id in seen_ids:
                    seen_ids[song_id] += 1
                    song = {**song, "id": f"{song_id}_{seen_ids[song_id]}"}
                else:
                    seen_ids[song_id] = 1
                _insert_song(c, song)
        c.commit()
    return c


def _insert_song(c: sqlite3.Connection, song: dict) -> None:
    # OR IGNORE: the legacy JSON index has a handful of duplicate "id" values (a pre-existing
    # data-quality quirk) - skip repeats during the one-time import rather than aborting it.
    # add_song() already checks for an existing id before calling this, so it never relies on
    # the IGNORE for its own inserts.
    c.execute(
        """
        INSERT OR IGNORE INTO songs (id, title, album, path, language, category, genre, moods,
                                      themes, energy, tempo, description, keywords, voice_aliases)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            song["id"], song["title"], song["album"], song.get("path", ""),
            song.get("language", "Unknown"), song.get("category", ""),
            json.dumps(song.get("genre", [])), json.dumps(song.get("moods", [])),
            json.dumps(song.get("themes", [])), song.get("energy", "medium"),
            song.get("tempo", "medium"), song.get("description", ""),
            json.dumps(song.get("keywords", [])), json.dumps(song.get("voice_aliases", [])),
        ),
    )


def _row_to_song(row: sqlite3.Row) -> dict:
    song = dict(row)
    for col in _LIST_COLUMNS:
        song[col] = json.loads(song[col] or "[]")
    return song


@lru_cache(maxsize=1)
def _load_songs() -> list[dict]:
    c = _conn()
    try:
        return [_row_to_song(r) for r in c.execute("SELECT * FROM songs")]
    finally:
        c.close()


def list_songs() -> list[dict]:
    """Public read-only wrapper around _load_songs() - for the /data admin page."""
    return _load_songs()


# Editable columns for update_song() - excludes "id" (the primary key, never repointed by an
# edit) and whitelists exactly the table's columns so user-supplied field names from the admin
# API can't be interpolated into the UPDATE statement's column list.
_EDITABLE_COLUMNS = {
    "title", "album", "path", "language", "category", "genre", "moods", "themes",
    "energy", "tempo", "description", "keywords", "voice_aliases",
}


def update_song(song_id: str, **fields) -> dict | None:
    """Partial update of an existing song's metadata (admin page edit). Only keys present in
    `fields` are changed; list-valued fields are re-encoded as JSON. Returns the updated row, or
    None if song_id doesn't exist. Clears the LRU cache so the edit is visible immediately."""
    c = _conn()
    try:
        existing = c.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
        if not existing:
            return None

        sets, vals = [], []
        for key, value in fields.items():
            if key not in _EDITABLE_COLUMNS:
                continue
            if key in _LIST_COLUMNS:
                value = json.dumps(value or [])
            sets.append(f"{key}=?")
            vals.append(value)
        if sets:
            vals.append(song_id)
            c.execute(f"UPDATE songs SET {', '.join(sets)} WHERE id=?", vals)
            c.commit()

        updated = c.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
    finally:
        c.close()

    _load_songs.cache_clear()
    return _row_to_song(updated)


def delete_song(song_id: str) -> bool:
    """Removes a song from the index (admin page delete). Returns False if song_id didn't
    exist. Doesn't touch the underlying SD-card file - index only."""
    c = _conn()
    try:
        cur = c.execute("DELETE FROM songs WHERE id=?", (song_id,))
        c.commit()
        deleted = cur.rowcount > 0
    finally:
        c.close()

    if deleted:
        _load_songs.cache_clear()
    return deleted


def _song_path(song: dict) -> str:
    # Optional per-song override: set "path" (relative to SONGS_ROOT) in the index for any entry
    # whose real on-card filename doesn't cleanly match "{album}/{title}.mp3" - e.g. files kept
    # with their original download-site names (track number prefix, "[www.site.com]" suffix) or
    # sitting flat in SONGS_ROOT with no album subfolder.
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


def add_song(title: str, filename: str, album: str | None = None) -> dict:
    """Insert a new song into the index and return the entry. `album` becomes both the on-card
    subfolder and the index's album field - defaults to "Downloads" when the caller has no real
    album/artist metadata for the track. Clears the LRU cache so subsequent find_song() calls see
    it immediately."""
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    song_id = f"downloads_{slug}"

    c = _conn()
    try:
        existing = c.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
        if existing:
            return _to_result(_row_to_song(existing))

        album = album or "Downloads"
        entry = {
            "id": song_id,
            "title": title,
            "album": album,
            "path": f"{album}/{filename}",
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
        _insert_song(c, entry)
        c.commit()
    finally:
        c.close()

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
