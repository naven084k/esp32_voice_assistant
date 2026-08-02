"""
Internet radio station lookup - checks a small curated list of known-working stream URLs first
(KNOWN_STATIONS below - update this any time without touching the ESP32 firmware, see CLAUDE.md's
WebSocket voice protocol section), then falls back to a live Radio Browser API
(https://www.radio-browser.info) search - a free, community-run directory that needs no API key.
`all.api.radio-browser.info` round-robins DNS across the currently-healthy mirror servers, so no
manual mirror selection/health-check needed.
"""
import os
import re

import httpx

RADIO_BROWSER_BASE = os.environ.get("RADIO_BROWSER_BASE", "https://all.api.radio-browser.info")
RADIO_DEFAULT_COUNTRY = os.environ.get("RADIO_DEFAULT_COUNTRY", "IN")
_HEADERS = {"User-Agent": "esp32-voice-assistant/1.0"}

# Hand-picked stream URLs manually verified to actually play on-device - the ESP32 firmware's
# internet radio path (see voice_button.ino's "Internet radio" section) uses schreibfaul1's
# ESP32-audioI2S library specifically so it can reach https:// and HLS (.m3u8) sources like these,
# which the plain http://-only Radio Browser fallback below would otherwise reject. Checked first,
# before falling back to a live Radio Browser search. `index` is a stable 1-based handle (e.g.
# "play station 3") - append new entries at the end so existing indices don't shift. Add more here
# any time, no firmware re-flash needed.
#
# `aliases`/`language`/`genre` exist purely to help _match_known_station() below identify a
# station from natural speech beyond an exact name/index match (e.g. "play some Telugu radio").
# Only set `language`/`genre` when actually confirmed (see the per-entry note) - for the handful
# of stations here that are just an unbranded stream on a generic hosting platform (Asura Hosting,
# RadioBoss, radioca.st) with no findable public identity, leave them unset rather than guess;
# `aliases` alone still covers how someone would ask for them by name.
KNOWN_STATIONS = [
    {
        "index": 1, "name": "AP9 FM", "url": "https://stream.ap9fm.in/radio/8000/radio.mp3",
        # Confirmed: Guntur, Andhra Pradesh - Telugu cultural/traditional songs plus pop/dance.
        "language": "Telugu", "genre": ["cultural", "traditional", "pop", "dance"],
        "aliases": ["AP 9 FM", "AP9", "AP nine FM", "Andhra Pradesh 9 FM", "Andhra 9 FM"],
    },
    {
        "index": 2, "name": "Melody Radio", "url": "https://a1.asurahosting.com/listen/melody_radio/radio.mp3",
        # Confirmed: Hyderabad-based, exclusively Telugu melodies (classic + new albums), 24x7.
        "language": "Telugu", "genre": ["melodies", "classics"],
        "aliases": ["Melody FM", "Telugu Melody Radio", "Telugu melodies", "Melody"],
    },
    {
        "index": 3, "name": "All India Radio 081", "url": "https://air.pc.cdn.bitgravity.com/air/live/pbaudio081/chunklist.m3u8",
        # Unconfirmed which of AIR's ~17 channels this specific pbaudio081 stream is - AIR
        # publishes channel names (Vividh Bharati, FM Gold, FM Rainbow, regional services, etc.)
        # but not a public pbaudioNNN-to-channel mapping, so language/genre are left unset here
        # rather than guess.
        "aliases": ["AIR", "Akashvani", "All India Radio", "All India Radio channel one", "All India Radio station one"],
    },
    {
        "index": 4, "name": "RadioBoss Stream 33", "url": "https://c8.radioboss.fm/stream/33",
        # RadioBoss is just automation-software/hosting (djsoft.net) - no findable public
        # identity for this specific stream beyond its raw URL.
        "aliases": ["Stream 33", "Channel 33", "RadioBoss 33"],
    },
    {
        "index": 5, "name": "Asura Radio", "url": "https://a1.asurahosting.com:9580/radio.mp3",
        # Asura Hosting is a generic ShoutCast/Icecast hosting provider (asurahosting.com) - this
        # is its bare-root default stream, no findable public station identity beyond that.
        "aliases": ["Asura", "Asura FM", "Asura station"],
    },
    {
        "index": 6, "name": "Mahi Radio", "url": "https://mahi.radioca.st/stream",
        # radioca.st is a generic Radio.co-style hosting platform, unrelated to All India Radio's
        # infrastructure - despite the name, not confirmed to be AIR's "Mahi Banswara" station.
        # No other findable public identity for this specific stream.
        "aliases": ["Mahi", "Mahi FM", "Mahi station"],
    },
    {
        "index": 7, "name": "All India Radio 021", "url": "https://air.pc.cdn.bitgravity.com/air/live/pbaudio021/chunklist.m3u8",
        # Same caveat as index 3 - one low-confidence single-source hint suggested Chennai FM Gold
        # (Tamil), but that wasn't corroborated well enough to assert as fact here.
        "aliases": ["All India Radio channel two", "All India Radio station two"],
    },
    {
        "index": 8, "name": "All India Radio 022", "url": "https://air.pc.cdn.bitgravity.com/air/live/pbaudio022/chunklist.m3u8",
        # Same caveat as index 3/7 - specific channel identity unconfirmed.
        "aliases": ["All India Radio channel three", "All India Radio station three"],
    },
]


def _match_known_station(query: str) -> dict | None:
    q = query.strip().lower()
    if not q:
        return None

    # Numeric reference ("play station 3", "channel 5") - 1-based, matches `index`.
    m = re.search(r"\d+", q)
    if m:
        idx = int(m.group())
        for station in KNOWN_STATIONS:
            if station["index"] == idx:
                return station

    # Name/alias match - substring either direction, so "melody" matches "Melody Radio" and
    # "AP9" matches "AP9 FM", regardless of which side is more specific. Picks the *longest*
    # matching candidate across all stations, not the first station in list order - several AIR
    # entries deliberately share a generic "All India Radio" alias (on top of their own more
    # specific "channel two"/"channel three" ones) to disambiguate, and a first-match scan would
    # let the generic alias on an earlier entry win before a later entry's specific one is even
    # checked.
    best_station, best_len = None, 0
    for station in KNOWN_STATIONS:
        candidates = [station["name"], *station.get("aliases", [])]
        for candidate in candidates:
            c = candidate.lower()
            if (q in c or c in q) and len(c) > best_len:
                best_station, best_len = station, len(c)
    if best_station:
        return best_station

    # Loose language/genre match for a request that names neither a specific station nor a
    # number, e.g. "play some Telugu radio" or "play a melody station".
    for station in KNOWN_STATIONS:
        tags = [t.lower() for t in (station.get("genre") or [])]
        if station.get("language"):
            tags.append(station["language"].lower())
        if any(tag in q for tag in tags):
            return station

    return None


async def search_station(query: str) -> dict | None:
    """Looks up a station by name/frequency/index (e.g. '98.3 FM', 'Radio Mirchi', 'station 3').
    Checks KNOWN_STATIONS first, then tries RADIO_DEFAULT_COUNTRY so an ambiguous FM frequency
    resolves to the expected local station, then falls back to a worldwide search.
    Returns {"name", "url"} or None."""
    known = _match_known_station(query)
    if known:
        return {"name": known["name"], "url": known["url"]}

    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
        station = await _search(client, query, RADIO_DEFAULT_COUNTRY)
        if not station:
            station = await _search(client, query, None)
        return station


async def _search(client: httpx.AsyncClient, query: str, countrycode: str | None) -> dict | None:
    params = {
        "name": query,
        "limit": 10,
        "order": "votes",
        "reverse": "true",
        "hidebroken": "true",
    }
    if countrycode:
        params["countrycode"] = countrycode
    resp = await client.get(f"{RADIO_BROWSER_BASE}/json/stations/search", params=params)
    resp.raise_for_status()
    for station in resp.json():
        url = station.get("url_resolved") or station.get("url")
        # The ESP32 firmware's internet radio engine (ESP32-audioI2S) can handle https:// streams
        # fine, but Radio Browser's directory is crowd-submitted and not manually vetted the way
        # KNOWN_STATIONS above is - an https:// entry here could still be a broken redirect, a
        # self-signed cert, or a codec the device can't decode. Stay conservative on this fallback
        # path and only propose http:// candidates; add a station to KNOWN_STATIONS instead once
        # it's actually been confirmed to work.
        if url and url.lower().startswith("http://"):
            return {"name": station.get("name") or query, "url": url}
    return None
