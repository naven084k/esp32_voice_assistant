"""
Proactive task-due reminders for ARIA.

Polls memory/tasks.db for pending, time-specific tasks whose due time has passed and haven't been
spoken yet (services/task_tools.get_due_reminders/mark_reminded), and pushes a spoken reminder to
the ESP32 via routers.voice.speak_unprompted - which only delivers it if the device is currently
connected AND idle (no turn in flight), so a reminder never interrupts an active conversation.

Once it starts playing, it keeps repeating - untouched, on its own, roughly every REPEAT_GAP_SECONDS
- until the user taps the button to dismiss it. Two independent signals catch that: speak_unprompted
reports "acknowledged" if the tap lands *while a push is actively streaming* (cancel_turn_task() has
something in flight to cancel); routers.voice.interaction_count() catches a tap that lands *between*
repeats instead, when nothing is in flight for cancellation to catch at all - without it, a tap
landing in that gap would be silently missed and the reminder would just keep repeating on schedule.
If the device is offline or already busy with something else, the reminder isn't nagged into the
tight repeat loop at all - it just waits for the next normal POLL_SECONDS tick, so it gets spoken
whenever the device is next connected and idle, however late that is. No other delivery path (e.g.
Telegram) is attempted.
"""
import asyncio
import logging

from services.task_tools import get_due_reminders, mark_reminded

logger = logging.getLogger("voice_agent.reminders")

POLL_SECONDS = 30
REPEAT_GAP_SECONDS = 2  # pause between repeats of a still-unacknowledged reminder, so it doesn't
                         # come out as one unbroken slur of "Reminder: X. Reminder: X. ..."

_task: asyncio.Task | None = None


def _speak_text(rows: list[dict]) -> str:
    titles = [r["title"] for r in rows]
    if len(titles) == 1:
        return f"Reminder: {titles[0]}."
    return f"You have {len(titles)} reminders: " + ", ".join(titles[:-1]) + f", and {titles[-1]}."


async def _poll_loop():
    from routers.voice import speak_unprompted, interaction_count

    while True:
        try:
            await asyncio.sleep(POLL_SECONDS)
            due = get_due_reminders()
            if not due:
                continue
            text = _speak_text(due)
            baseline = interaction_count()
            while True:
                outcome = await speak_unprompted(text)
                dismissed = outcome == "acknowledged" or interaction_count() != baseline
                if dismissed:
                    for row in due:
                        mark_reminded(row["id"])
                    break
                if outcome == "declined":
                    break  # not connected/idle right now - retry on the next normal poll tick
                # "delivered": played through untouched - nobody dismissed it, so say it again,
                # unless a tap lands during this very gap (checked once more before repeating)
                await asyncio.sleep(REPEAT_GAP_SECONDS)
                if interaction_count() != baseline:
                    for row in due:
                        mark_reminded(row["id"])
                    break
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
