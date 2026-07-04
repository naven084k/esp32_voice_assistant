"""
Task tools for ARIA (formerly separate todo/reminder tools — a reminder is just
a task with a time-specific due date, so they're unified here).
Persists to memory/tasks.db (SQLite).
due_at is always normalised to IST before storing, regardless of how the LLM passes it.
"""
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import dateparser
from langchain_core.tools import tool

_IST = ZoneInfo("Asia/Kolkata")
_TIME_HINT = re.compile(r"\d{1,2}(:\d{2})?\s*(am|pm)|:\d{2}|\bnoon\b|\bmidnight\b|\bo'?clock\b", re.I)


def _parse_due(value: str) -> str:
    """Parse any date/time expression → 'YYYY-MM-DD HH:MM' (if a time was given) or
    'YYYY-MM-DD' (date-only deadline), normalised to IST. Handles ISO strings,
    'tomorrow at 3pm', 'Friday 9am', 'in 2 hours', 'tomorrow', 'Friday', etc."""
    value = value.strip()
    if not value:
        return ""
    dt = dateparser.parse(
        value,
        settings={
            "TIMEZONE": "Asia/Kolkata",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        },
    )
    if dt is None:
        return value  # unparseable — store as-is
    dt = dt.astimezone(_IST)
    if _TIME_HINT.search(value):
        return dt.strftime("%Y-%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d")


_DB_PATH = Path("memory/tasks.db")


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(_DB_PATH))
    c.row_factory = sqlite3.Row
    existing = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "tasks" not in existing:
        c.executescript("""
            CREATE TABLE tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                title        TEXT NOT NULL,
                description  TEXT DEFAULT '',
                priority     TEXT DEFAULT 'normal',
                due_at       TEXT DEFAULT '',
                status       TEXT DEFAULT 'pending',
                created_at   TEXT DEFAULT (datetime('now','localtime')),
                completed_at TEXT DEFAULT ''
            );
        """)
        if "todos" in existing:
            c.execute("""
                INSERT INTO tasks (title, description, priority, due_at, status, created_at, completed_at)
                SELECT title, description, priority, due_date, status, created_at, completed_at FROM todos
            """)
            c.execute("DROP TABLE todos")
        if "reminders" in existing:
            c.execute("""
                INSERT INTO tasks (title, description, priority, due_at, status, created_at, completed_at)
                SELECT title, description, 'normal', remind_at,
                       CASE WHEN status='dismissed' THEN 'completed' ELSE status END,
                       created_at, ''
                FROM reminders
            """)
            c.execute("DROP TABLE reminders")
        c.commit()
    return c


def _find_task(c: sqlite3.Connection, title_or_id: str):
    """Match by ID or case-insensitive partial title."""
    if title_or_id.isdigit():
        return c.execute("SELECT * FROM tasks WHERE id=?", (int(title_or_id),)).fetchone()
    return c.execute(
        "SELECT * FROM tasks WHERE lower(title) LIKE ? ORDER BY id DESC LIMIT 1",
        (f"%{title_or_id.lower()}%",),
    ).fetchone()


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{['th', 'st', 'nd', 'rd', 'th'][min(n % 10, 4)]}"


def _speak_datetime(dt: datetime) -> str:
    """Format a datetime for natural speech: '15th June Monday 9 AM' or '15th June Monday 9:30 AM'."""
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    time = f"{hour}:{dt.minute:02d} {ampm}" if dt.minute else f"{hour} {ampm}"
    return f"{_ordinal(dt.day)} {dt.strftime('%B')} {dt.strftime('%A')} {time}"


def _speak_date(date_str: str) -> str:
    """Format a YYYY-MM-DD date for natural speech: '15th June Monday'."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{_ordinal(dt.day)} {dt.strftime('%B')} {dt.strftime('%A')}"
    except ValueError:
        return date_str


def _fmt_due(due_at: str) -> str:
    if not due_at:
        return ""
    if len(due_at) > 10:
        try:
            return _speak_datetime(datetime.strptime(due_at, "%Y-%m-%d %H:%M"))
        except ValueError:
            return due_at
    return _speak_date(due_at)


def _fmt_task(row) -> str:
    due = f"  due {_fmt_due(row['due_at'])}" if row['due_at'] else ""
    done = " ✓" if row['status'] == 'completed' else ""
    pri = f" [{row['priority']}]" if row['priority'] != 'normal' else ""
    return f"#{row['id']} {row['title']}{pri}{due}{done}"


# ─── Task tools ─────────────────────────────────────────────────────────────

@tool
def add_task(title: str, due: str = "", description: str = "", priority: str = "normal") -> str:
    """Add a task — covers both todos and reminders.
    due: any date/time expression, e.g. 'tomorrow', 'Friday', '2026-06-20', 'tomorrow at 3pm', 'in 2 hours',
    or empty for no deadline. Give a specific time to have ARIA proactively alert you when it's due;
    a date alone is treated as a soft deadline.
    priority: low / normal / high
    Example: add task 'Buy groceries' due tomorrow, remind me to call the doctor tomorrow at 10 AM,
    add high priority task 'Submit report' due 2026-06-20"""
    c = _conn()
    normalized = _parse_due(due)
    c.execute(
        "INSERT INTO tasks (title, description, priority, due_at) VALUES (?,?,?,?)",
        (title.strip(), description.strip(), priority.lower(), normalized),
    )
    c.commit()
    due_text = f" — {_fmt_due(normalized)}" if normalized else ""
    return f"Added: '{title}'{due_text} [{priority} priority]"


def _rows_for_filter(c: sqlite3.Connection, filter: str):
    """Shared filter logic for list_tasks and clear_tasks."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = datetime.now().strftime("%Y-%m-%d")
    f = filter.lower().strip()

    if f == "pending":
        return c.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY priority DESC, due_at, id").fetchall()
    elif f == "completed":
        return c.execute("SELECT * FROM tasks WHERE status='completed' ORDER BY completed_at DESC LIMIT 20").fetchall()
    elif f == "high":
        return c.execute("SELECT * FROM tasks WHERE priority='high' AND status='pending' ORDER BY due_at, id").fetchall()
    elif f == "today":
        return c.execute(
            "SELECT * FROM tasks WHERE due_at LIKE ? AND status='pending' ORDER BY due_at",
            (f"{today}%",),
        ).fetchall()
    elif f == "overdue":
        return c.execute(
            """SELECT * FROM tasks WHERE status='pending' AND due_at!='' AND
               ((length(due_at)=10 AND due_at<?) OR (length(due_at)>10 AND due_at<?))
               ORDER BY due_at""",
            (today, now),
        ).fetchall()
    elif f == "upcoming":
        return c.execute(
            """SELECT * FROM tasks WHERE status='pending' AND due_at!='' AND
               ((length(due_at)=10 AND due_at>=?) OR (length(due_at)>10 AND due_at>=?))
               ORDER BY due_at""",
            (today, now),
        ).fetchall()
    else:  # all
        return c.execute("SELECT * FROM tasks ORDER BY status, due_at, id").fetchall()


@tool
def list_tasks(filter: str = "pending") -> str:
    """List tasks — covers both todos and reminders.
    filter options: 'pending', 'completed', 'all', 'high', 'today', 'overdue', 'upcoming'
    Example: show my tasks, what's pending, show completed tasks, any overdue tasks,
    what reminders do I have today"""
    c = _conn()
    rows = _rows_for_filter(c, filter)
    if not rows:
        return f"No {filter} tasks found."
    lines = [_fmt_task(r) for r in rows]
    return f"{len(rows)} {filter} task(s):\n" + "\n".join(lines)


@tool
def clear_tasks(filter: str = "pending", action: str = "complete") -> str:
    """Bulk-clear multiple tasks at once. Use this whenever the user refers to 'all' tasks
    rather than one specific task/reminder — complete_task and delete_task only handle a single item.
    filter: which tasks to target — 'pending' (default), 'high', 'today', 'overdue', 'completed', 'all'
    action: 'complete' (mark done, default) or 'delete' (remove permanently)
    Example: clear all my pending tasks, mark everything as done, delete all completed tasks,
    remove all overdue reminders"""
    c = _conn()
    rows = _rows_for_filter(c, filter)
    if not rows:
        return f"No {filter} tasks to clear."
    ids = [r['id'] for r in rows]
    placeholders = ",".join("?" * len(ids))
    if action.lower().strip() == "delete":
        c.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", ids)
    else:
        c.execute(
            f"UPDATE tasks SET status='completed', completed_at=datetime('now','localtime') WHERE id IN ({placeholders})",
            ids,
        )
    c.commit()
    verb = "Deleted" if action.lower().strip() == "delete" else "Completed"
    return f"{verb} {len(ids)} {filter} task(s)."


@tool
def complete_task(title_or_id: str) -> str:
    """Mark a task as done — covers completing a todo and dismissing a reminder.
    Use the title (partial is fine) or the task ID.
    Example: complete 'Buy groceries', done with task 3, dismiss the doctor reminder"""
    c = _conn()
    row = _find_task(c, title_or_id)
    if not row:
        return f"No task found matching '{title_or_id}'."
    if row['status'] == 'completed':
        return f"'{row['title']}' is already completed."
    c.execute(
        "UPDATE tasks SET status='completed', completed_at=datetime('now','localtime') WHERE id=?",
        (row['id'],),
    )
    c.commit()
    return f"Marked as done: '{row['title']}'"


@tool
def delete_task(title_or_id: str) -> str:
    """Delete a task permanently. Use partial title or ID.
    Example: delete task 'groceries', remove reminder 5"""
    c = _conn()
    row = _find_task(c, title_or_id)
    if not row:
        return f"No task found matching '{title_or_id}'."
    c.execute("DELETE FROM tasks WHERE id=?", (row['id'],))
    c.commit()
    return f"Deleted: '{row['title']}'"


@tool
def update_task(title_or_id: str, new_title: str = "", due: str = "", priority: str = "") -> str:
    """Update an existing task's title, due date/time, or priority.
    Example: change 'groceries' due date to Friday, set report priority to high,
    move the doctor reminder to 5pm"""
    c = _conn()
    row = _find_task(c, title_or_id)
    if not row:
        return f"No task found matching '{title_or_id}'."
    fields, vals = [], []
    if new_title:
        fields.append("title=?"); vals.append(new_title.strip())
    if due:
        fields.append("due_at=?"); vals.append(_parse_due(due))
    if priority:
        fields.append("priority=?"); vals.append(priority.lower())
    if not fields:
        return "Nothing to update — provide new_title, due, or priority."
    vals.append(row['id'])
    c.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", vals)
    c.commit()
    return f"Updated task #{row['id']}: '{row['title']}'"


@tool
def check_due_tasks() -> str:
    """Check if any time-specific tasks (reminders) are due now or overdue. Call this at the start
    of a conversation to proactively alert the user about pending reminders."""
    c = _conn()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = c.execute(
        "SELECT * FROM tasks WHERE status='pending' AND length(due_at)>10 AND due_at<=? ORDER BY due_at",
        (now,),
    ).fetchall()
    if not rows:
        return "No tasks due."
    lines = [f"• {r['title']} (was due {r['due_at']})" for r in rows]
    return f"You have {len(rows)} overdue task(s):\n" + "\n".join(lines)
