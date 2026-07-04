from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import chat, voice, dashboard
from routers.telegram_bot import start_bot, stop_bot
from services import llm, tts


@asynccontextmanager
async def lifespan(app: FastAPI):
    await llm.init()
    await asyncio.get_event_loop().run_in_executor(None, tts._get_kokoro)
    await start_bot()
    yield
    await stop_bot()
    await llm.close()


app = FastAPI(title="LangChain Voice Agent", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router, prefix="/api")
app.include_router(voice.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(dashboard.page_router)


@app.get("/health")
def health():
    return {"status": "ok"}
