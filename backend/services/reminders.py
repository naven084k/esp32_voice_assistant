"""
Proactive task-due reminders for ARIA.

Polls memory/tasks.db for pending, time-specific tasks whose due time has passed and haven't been
spoken yet (services/task_tools.get_due_reminders/mark_reminded), and pushes a spoken reminder to
the ESP32 via routers.voice.speak_unprompted - which only delivers it if the device is currently
connected AND idle (no turn in flight), so a reminder never interrupts an active conversation. If
the device is offline or mid-turn when a task comes due, the task simply stays un-reminded and is
retried on the next poll - so it gets spoken whenever the device is next connected and idle,
however late that is. No other delivery path (e.g. Telegram) is attempted.
"""
import asyncio
import logging

from services.task_tools import get_due_reminders, mark_reminded

logger = logging.getLogger("voice_agent.reminders")

POLL_SECONDS = 30

_task: asyncio.Task | None = None


def _speak_text(rows: list[dict]) -> str:
    titles = [r["title"] for r in rows]
    if len(titles) == 1:
        return f"Reminder: {titles[0]}."
    return f"You have {len(titles)} reminders: " + ", ".join(titles[:-1]) + f", and {titles[-1]}."


async def _poll_loop():
    from routers.voice import speak_unprompted

    while True:
        try:
            await asyncio.sleep(POLL_SECONDS)
            due = get_due_reminders()
            if not due:
                continue
            if await speak_unprompted(_speak_text(due)):
                for row in due:
                    mark_reminded(row["id"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[reminders] poll failed: {e}", exc_info=True)


async def start():
    global _task
    _task = asyncio.create_task(_poll_loop())


async def stop():
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except BaseException:
            pass
        _task = None
