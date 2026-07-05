import os
import re
import uuid
from contextvars import ContextVar
from pathlib import Path

import aiosqlite
from langchain.agents import create_agent
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from services.tools import build_tools
from services.callbacks import TimingCallbackHandler

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

current_thread_id: ContextVar[str] = ContextVar("current_thread_id", default="")

DEFAULT_SYSTEM = """You are ARIA, a voice assistant for the Kumar family in Hyderabad, India. Primary user: Naveen (software engineer). You also speak with children.

RESPONSE STYLE — this is voice output, not text:
- 1 to 3 sentences for simple queries, 5 max for complex ones.
- Plain speech only. No markdown, no bullets, no asterisks, no numbered lists, ever.
- Speak numbers and times naturally: "half past two", "28 degrees", not "2:30 PM" or "28°C".
- Speak dates naturally: "15th June Monday at 9 AM", never "2026-06-15 09:00" or "Mon, 15 Jun".
- No filler phrases like "Certainly!" or "Great question!" — answer directly.
- Match the language: English by default, Hindi if addressed in Hindi, Hinglish if mixed.

TOOLS: Use silently — just give the result. Never say "let me search" or "checking now".
BACKGROUND: If the user says "do this in background", "send me later", "don't wait", or implies they don't want to hold — use run_in_background immediately and say "Sure, I'll do this offline and send you the result on Telegram."

CONTEXT: IST timezone, Celsius, kilometers, INR. Use Indian English ("switch off" not "turn off").

CHILDREN: If the speaker seems to be a child, keep answers age-appropriate and positive. Redirect sensitive topics: "That's a great thing to ask your dad!"

IF UNSURE: Say so briefly. Confirm before any destructive action."""

DB_PATH = Path("memory/checkpoints.db")

_conn: aiosqlite.Connection | None = None
_checkpointer: AsyncSqliteSaver | None = None
_agent = None


def get_model() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(model=GEMINI_MODEL, google_api_key=os.environ["GOOGLE_GEMINI_API_KEY"])


async def init():
    global _conn, _checkpointer, _agent
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(str(DB_PATH))
    _checkpointer = AsyncSqliteSaver(_conn)
    await _checkpointer.setup()
    _agent = create_agent(
        model=get_model(),
        tools=build_tools(),
        checkpointer=_checkpointer,
    )


async def close():
    if _conn:
        await _conn.close()


async def process(text: str, thread_id: str | None = None, system_prompt: str = DEFAULT_SYSTEM) -> str:
    if _agent is None:
        raise RuntimeError("Agent not initialized — call llm.init() first.")

    if thread_id is None:
        thread_id = str(uuid.uuid4())

    config = {"configurable": {"thread_id": thread_id}}
    state = await _agent.aget_state(config)
    is_new_thread = not state.values.get("messages")

    messages = []
    if is_new_thread:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": text})

    _token = current_thread_id.set(thread_id)
    result = await _agent.ainvoke(
        {"messages": messages},
        config={**config, "callbacks": [TimingCallbackHandler()]},
    )
    current_thread_id.reset(_token)
    return _strip_markdown(result["messages"][-1].content)


def _strip_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'^\s*[-*•]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+[.)]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{2,}', ' ', text)
    text = re.sub(r'\n', ' ', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()
