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
# any time, no firmware re-flash needed; names are best-guess labels derived from each URL, rename
# freely.
KNOWN_STATIONS = [
    {"index": 1, "name": "AP9 FM",              "url": "https://stream.ap9fm.in/radio/8000/radio.mp3"},
    {"index": 2, "name": "Melody Radio",        "url": "https://a1.asurahosting.com/listen/melody_radio/radio.mp3"},
    {"index": 3, "name": "All India Radio 081", "url": "https://air.pc.cdn.bitgravity.com/air/live/pbaudio081/chunklist.m3u8"},
    {"index": 4, "name": "RadioBoss Stream 33", "url": "https://c8.radioboss.fm/stream/33"},
    {"index": 5, "name": "Asura Radio",         "url": "https://a1.asurahosting.com:9580/radio.mp3"},
    {"index": 6, "name": "Mahi Radio",          "url": "https://mahi.radioca.st/stream"},
    {"index": 7, "name": "All India Radio 021", "url": "https://air.pc.cdn.bitgravity.com/air/live/pbaudio021/chunklist.m3u8"},
    {"index": 8, "name": "All India Radio 022", "url": "https://air.pc.cdn.bitgravity.com/air/live/pbaudio022/chunklist.m3u8"},
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

    # Name match - substring either direction, so "melody" matches "Melody Radio" and vice versa.
    for station in KNOWN_STATIONS:
        name = station["name"].lower()
        if q in name or name in q:
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
