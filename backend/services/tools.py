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
