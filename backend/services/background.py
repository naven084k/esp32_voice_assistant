"""
Background deep-analysis agent for ARIA.
Spawns a stateless research agent and delivers the summary via Telegram.
"""
import asyncio
import logging
import re

_RESEARCH_SYSTEM = """You are a deep research agent working on a background task for ARIA.

Your job:
- Analyse the task thoroughly using whatever tools are needed
- Use web_search at least 2-3 times to gather comprehensive, up-to-date information from multiple angles
- Cross-reference sources, identify key facts, surface insights the user will care about
- Produce a clear, complete written summary — this goes to Telegram as text, not a brief voice reply
- Be thorough. The user explicitly asked for background processing so they expect depth, not brevity
"""


def _extract(content) -> str:
    """Extract plain text from str, list of blocks, or any content type."""
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
        )
    return str(content)


def _clean(text: str) -> str:
    """Strip markdown symbols so Telegram plain-text mode doesn't choke."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*',     r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__',     r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_(.+?)_',       r'\1', text, flags=re.DOTALL)
    text = re.sub(r'```[\s\S]*?```', '',   text)
    text = re.sub(r'`(.+?)`',       r'\1', text)
    text = re.sub(r'^#{1,6}\s+',    '',    text, flags=re.MULTILINE)
    return text.strip()


async def _send_telegram(chat_id: int, text: str):
    from routers.telegram_bot import get_app
    app = get_app()
    if app:
        for i in range(0, len(text), 4000):
            await app.bot.send_message(chat_id=chat_id, text=text[i:i + 4000])


async def _execute(task: str, thread_id: str):
    try:
        from langchain.agents import create_agent
        from services.tools import build_tools

        deep_tools = [
            t for t in build_tools()
            if getattr(t, "name", None) != "run_in_background"
        ]

        agent = create_agent(
            model="openai:gpt-4o-mini",
            tools=deep_tools,
        )

        result = await agent.ainvoke({
            "messages": [
                {"role": "system", "content": _RESEARCH_SYSTEM},
                {"role": "user",   "content": task},
            ]
        })

        reply = _clean(_extract(result["messages"][-1].content))

        if thread_id.startswith("tg_"):
            chat_id = int(thread_id.split("_")[1])
            await _send_telegram(chat_id, f"📊 Analysis complete:\n\n{reply}")

    except Exception as e:
        logging.getLogger("voice_agent").error(f"[BG] Task failed: {e}", exc_info=True)
        if thread_id.startswith("tg_"):
            try:
                chat_id = int(thread_id.split("_")[1])
                await _send_telegram(chat_id, f"❌ Background task failed: {e}")
            except Exception:
                pass


def schedule(task: str, thread_id: str):
    asyncio.create_task(_execute(task, thread_id))
