import os
from datetime import datetime
from tavily import AsyncTavilyClient
from langchain_core.tools import tool


@tool
def get_current_datetime(query: str = "") -> str:
    """Returns the current date and time. Use when the user asks about the current time or date."""
    return datetime.now().strftime("%A, %B %d, %Y %H:%M:%S")


@tool
async def web_search(query: str) -> str:
    """Search the web for real-time information. Use this for weather, news, sports scores,
    stock prices, current events, or anything that may have changed recently or requires
    up-to-date information beyond training knowledge."""
    client = AsyncTavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    response = await client.search(query, max_results=3)
    results = response.get("results", [])
    if not results:
        return "No results found."
    return "\n\n".join(f"{r['title']}\n{r['content']}" for r in results)


@tool
async def play_radio(station: str) -> str:
    """Use when the user asks to play an internet radio station - e.g. 'play radio 98.3 FM',
    'tune in to Radio Mirchi', 'play some FM radio', 'play Big FM', 'play AP9 FM', 'play Melody
    Radio', 'play station 3'. `station` should be the station name/frequency/number exactly as the
    user said it - a handful of known-good stations (AP9 FM, Melody Radio, Asura Radio, Mahi Radio,
    All India Radio, RadioBoss Stream 33, referenceable by name or by 1-based station number) are
    checked first; anything else falls back to a live web search."""
    from services.llm import current_thread_id, queue_device_action
    from services.radio import search_station

    result = await search_station(station)
    if not result:
        return f"Couldn't find a radio station matching '{station}'."
    queue_device_action(current_thread_id.get(), {
        "type": "radio", "url": result["url"], "name": result["name"],
    })
    return f"Playing {result['name']} now."


@tool
def stop_radio(query: str = "") -> str:
    """Use when the user asks to stop the radio or turn off the internet radio that's playing."""
    from services.llm import current_thread_id, queue_device_action
    queue_device_action(current_thread_id.get(), {"type": "stop_radio"})
    return "Stopped the radio."


@tool
async def download_song(song: str) -> str:
    """Use when the user asks to play a specific song/track (not a radio station) - e.g.
    'play Tum Hi Ho', 'download and play <song> by <artist>'. Searches for a direct
    downloadable audio file link (.mp3/.wav) and, if found, queues a device download+play
    action. The song is also added to the local music index so future requests use play_song
    instead. Tell the user if no downloadable link was found."""
    import re
    from services.llm import current_thread_id, queue_device_action
    from services.song_index import add_song

    client = AsyncTavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    response = await client.search(f"{song} mp3 download free direct file link", max_results=10)
    results = response.get("results", [])
    audio_url = None
    title = song
    for r in results:
        url = r.get("url", "")
        if url.lower().split("?")[0].endswith((".mp3", ".wav")):
            audio_url = url
            title = r.get("title") or song
            break
    if not audio_url:
        return f"Couldn't find a direct playable audio link for '{song}'."

    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    filename = f"{slug}.mp3"
    indexed = add_song(title, filename)

    queue_device_action(current_thread_id.get(), {
        "type": "download_song",
        "url": audio_url,
        "title": title,
        "path": indexed["path"],
    })
    return f"Downloading and playing {title} now."


@tool
async def play_song(song: str) -> str:
    """Use when the user asks to play a specific song, or a mood/genre/language match, from the
    local music library - e.g. 'play O Rangula Chilaka', 'play something romantic', 'play a telugu
    melody song'. Prefer this over download_song - only fall back to download_song if this returns
    no match and the user still wants that specific track."""
    from services.llm import current_thread_id, queue_device_action
    from services.song_index import find_song

    result = find_song(song)
    if not result:
        return f"Couldn't find '{song}' in the music library."
    queue_device_action(current_thread_id.get(), {
        "type": "play_song", "path": result["path"], "title": result["title"],
    })
    return f"Playing {result['title']} now."


@tool
async def play_song_queue(query: str, count: int = 5) -> str:
    """Use when the user asks to play multiple/several songs back-to-back, a playlist, or "some"/
    "a few" songs matching a mood/genre/language/artist - e.g. 'play some Arijit Singh songs',
    'play a few romantic songs', 'play some telugu melodies', 'play party songs'. Finds up to
    `count` matching tracks in the local music library and queues them to auto-play one after
    another without needing to ask again per song. Use play_song instead for a single specific
    track request."""
    from services.llm import current_thread_id, queue_device_action
    from services.song_index import find_songs

    results = find_songs(query, limit=count)
    if not results:
        return f"Couldn't find any songs matching '{query}' in the music library."
    queue_device_action(current_thread_id.get(), {
        "type": "play_song_queue",
        "songs": [{"path": r["path"], "title": r["title"]} for r in results],
    })
    names = ", ".join(r["title"] for r in results)
    return f"Queued {len(results)} songs: {names}."


@tool
def stop_song(query: str = "") -> str:
    """Use when the user asks to stop the song (or song queue/playlist) that's currently playing
    from the local music library - also clears any remaining queued songs from play_song_queue."""
    from services.llm import current_thread_id, queue_device_action
    queue_device_action(current_thread_id.get(), {"type": "stop_song"})
    return "Stopped the song."


@tool
async def run_in_background(task: str) -> str:
    """Use this when the user asks you to do something in the background, offline, or later —
    e.g. 'search for X and send me the results', 'do this in the background', 'let me know later'.
    Schedules a deep-analysis agent, sends the result to Telegram when done, and returns immediately."""
    from services.llm import current_thread_id
    from services.background import schedule
    thread_id = current_thread_id.get()
    if not thread_id:
        return "Cannot run in background — no active session found."
    schedule(task, thread_id)
    return "Sure, I'll do this offline and send you the result on Telegram."


def build_tools():
    from services.math_tools import (
        calculate, convert_units, convert_temperature,
        scientific_calc, statistics_calc, financial_calc,
        convert_currency, get_time_in_timezone, convert_timezone,
    )
    from services.task_tools import (
        add_task, list_tasks, complete_task, delete_task, update_task,
        check_due_tasks, clear_tasks,
    )
    return [
        get_current_datetime,
        run_in_background,
        web_search,
        play_radio,
        stop_radio,
        download_song,
        play_song,
        play_song_queue,
        stop_song,
        calculate,
        convert_units,
        convert_temperature,
        scientific_calc,
        statistics_calc,
        financial_calc,
        convert_currency,
        get_time_in_timezone,
        convert_timezone,
        add_task,
        list_tasks,
        complete_task,
        delete_task,
        update_task,
        check_due_tasks,
        clear_tasks,
    ]
