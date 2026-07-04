"""
Telegram bot interface for ARIA.
Each Telegram chat gets its own thread_id → persistent memory per user.

Supports:
  - Text messages   → LLM reply
  - Voice notes     → STT → LLM → text reply (transcript shown)
  - /start          → welcome message
  - /tasks          → list pending tasks (todos & reminders, unified)
  - /clear          → start a fresh conversation thread
"""
import logging
import os

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from services import llm, stt
from services.task_tools import list_tasks, check_due_tasks

logger = logging.getLogger("voice_agent.telegram")

_app: Application | None = None


def get_app() -> Application | None:
    return _app


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _thread_id(update: Update) -> str:
    """One persistent memory thread per Telegram chat."""
    return f"tg_{update.effective_chat.id}"


async def _typing(update: Update):
    await update.effective_chat.send_action(ChatAction.TYPING)


def _chunk(text: str, size: int = 4000):
    """Split long replies into Telegram-sized chunks (max 4096 chars)."""
    for i in range(0, len(text), size):
        yield text[i : i + size]


# ─── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "there"
    await update.message.reply_text(
        f"Hi {name}! I'm ARIA, your voice assistant. Send me a message or a voice note.\n\n"
        "Commands:\n"
        "/tasks — show pending tasks & reminders\n"
        "/clear — start a fresh conversation"
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _typing(update)
    due = check_due_tasks.invoke({})
    result = list_tasks.invoke({"filter": "pending"})
    text = due if "overdue" in due.lower() else ""
    text += ("\n\n" if text else "") + result
    await update.message.reply_text(text.strip() or "No tasks.")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Store a fresh thread_id in user context data
    context.user_data["thread_override"] = f"tg_{update.effective_chat.id}_{update.message.message_id}"
    await update.message.reply_text("Started a fresh conversation. Previous context cleared.")


# ─── Message handlers ──────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return
    await _typing(update)
    thread_id = context.user_data.get("thread_override") or _thread_id(update)
    try:
        reply = await llm.process(text, thread_id=thread_id)
        for chunk in _chunk(reply):
            await update.message.reply_text(chunk)
    except Exception as e:
        logger.error(f"[Telegram] text handler error: {e}", exc_info=True)
        await update.message.reply_text("Sorry, something went wrong. Please try again.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _typing(update)
    thread_id = context.user_data.get("thread_override") or _thread_id(update)
    try:
        # Download Telegram voice note (OGG/Opus)
        voice_file = await update.message.voice.get_file()
        audio_bytes = bytes(await voice_file.download_as_bytearray())

        # Transcribe with local Whisper
        transcript = await stt.transcribe(audio_bytes, "voice.ogg")
        if not transcript:
            await update.message.reply_text("Couldn't hear that clearly. Please try again.")
            return

        # Process with LLM agent
        reply = await llm.process(transcript, thread_id=thread_id)

        # Send transcript + reply
        response = f'🎤 "{transcript}"\n\n{reply}'
        for chunk in _chunk(response):
            await update.message.reply_text(chunk)

    except Exception as e:
        logger.error(f"[Telegram] voice handler error: {e}", exc_info=True)
        await update.message.reply_text("Couldn't process the voice message. Please try again.")


async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a text or voice message — I'll do my best to help!")


# ─── Bot lifecycle ────────────────────────────────────────────────────────────

async def start_bot():
    global _app
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.info("[Telegram] TELEGRAM_BOT_TOKEN not set — bot disabled.")
        return

    _app = Application.builder().token(token).build()

    _app.add_handler(CommandHandler("start", cmd_start))
    _app.add_handler(CommandHandler("tasks", cmd_tasks))
    _app.add_handler(CommandHandler("clear", cmd_clear))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    _app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    _app.add_handler(MessageHandler(~filters.TEXT & ~filters.VOICE & ~filters.COMMAND, handle_unsupported))

    await _app.initialize()
    await _app.start()
    await _app.updater.start_polling(drop_pending_updates=True)
    logger.info("[Telegram] Bot started — polling for updates.")


async def stop_bot():
    global _app
    if _app:
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
        logger.info("[Telegram] Bot stopped.")
