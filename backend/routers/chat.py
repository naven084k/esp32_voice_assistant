import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from services import llm
from services.request_timer import new_timer, get_timer

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    system_prompt: str = "You are a helpful assistant."
    thread_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    thread_id: str
    request_id: str


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    thread_id = req.thread_id or str(uuid.uuid4())
    t = new_timer(label=req.message[:50], thread_id=thread_id)
    try:
        reply = await llm.process(req.message, thread_id=thread_id, system_prompt=req.system_prompt)
        return ChatResponse(reply=reply, thread_id=thread_id, request_id=t.id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        t = get_timer()
        if t:
            t.log_table()
