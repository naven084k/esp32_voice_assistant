import uuid
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from services import llm
from services.auth import verify_key
from services.request_timer import new_timer, get_timer

router = APIRouter()
page_router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    system_prompt: str = "You are a helpful assistant."
    thread_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    thread_id: str
    request_id: str


async def _run_chat(req: ChatRequest) -> ChatResponse:
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


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    return await _run_chat(req)


@router.post("/chat/ui", response_model=ChatResponse, dependencies=[Depends(verify_key)])
async def chat_ui_send(req: ChatRequest):
    """Same as /chat, but gated behind the dashboard access key — used only by the /chat-ui web page."""
    return await _run_chat(req)


@router.post("/chat/ui/verify", dependencies=[Depends(verify_key)])
async def chat_ui_verify():
    return {"ok": True}


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ARIA — Chat</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; height: 100vh; display: flex; flex-direction: column;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0f1115; color: #e6e6e6;
  }
  header {
    padding: 14px 20px; border-bottom: 1px solid #23262e;
    display: flex; align-items: center; justify-content: space-between;
  }
  header h1 { font-size: 15px; margin: 0; color: #9aa0ab; letter-spacing: .02em; }
  header button {
    background: none; border: 1px solid #2c313c; color: #9aa0ab; border-radius: 6px;
    padding: 5px 10px; font-size: 11px; cursor: pointer;
  }
  header button:hover { background: #1c1f26; }
  #messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 12px; }
  .msg { max-width: 70%; padding: 10px 14px; border-radius: 12px; font-size: 13.5px; line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
  .msg.user { align-self: flex-end; background: #5b8cff; color: #fff; border-bottom-right-radius: 4px; }
  .msg.assistant { align-self: flex-start; background: #181b22; border: 1px solid #23262e; color: #dfe3ea; border-bottom-left-radius: 4px; }
  .msg.error { align-self: flex-start; background: #3a1d22; border: 1px solid #4a2530; color: #ff8a8a; }
  .msg.pending { color: #6b7280; font-style: italic; }
  #inputRow { display: flex; gap: 10px; padding: 16px 20px; border-top: 1px solid #23262e; }
  #inputRow input {
    flex: 1; padding: 10px 14px; border-radius: 8px; border: 1px solid #2c313c;
    background: #14161c; color: #e6e6e6; font-size: 13.5px;
  }
  #inputRow input:focus { outline: none; border-color: #5b8cff; }
  #inputRow button {
    padding: 10px 18px; border-radius: 8px; border: none;
    background: #5b8cff; color: #fff; font-size: 13.5px; font-weight: 600; cursor: pointer;
  }
  #inputRow button:hover { background: #4577f0; }
  #inputRow button:disabled { background: #2c313c; cursor: default; }
  .empty { color: #565b66; padding: 40px; font-size: 14px; text-align: center; }
  .hidden { display: none !important; }

  #gate {
    position: fixed; inset: 0; background: #0f1115; display: flex;
    align-items: center; justify-content: center; z-index: 10;
  }
  #gate .box { width: 300px; text-align: center; }
  #gate h2 { font-size: 16px; margin: 0 0 6px; color: #f1f2f4; }
  #gate p { font-size: 12.5px; color: #6b7280; margin: 0 0 18px; }
  #gate input {
    width: 100%; padding: 9px 12px; border-radius: 8px; border: 1px solid #2c313c;
    background: #14161c; color: #e6e6e6; font-size: 13px; margin-bottom: 10px;
  }
  #gate input:focus { outline: none; border-color: #5b8cff; }
  #gate button {
    width: 100%; padding: 9px 12px; border-radius: 8px; border: none;
    background: #5b8cff; color: #fff; font-size: 13px; font-weight: 600; cursor: pointer;
  }
  #gate button:hover { background: #4577f0; }
  #gate .err { color: #ff8a8a; font-size: 12px; margin-top: 10px; min-height: 14px; }
</style>
</head>
<body>
  <div id="gate">
    <div class="box">
      <h2>ARIA Chat</h2>
      <p>Enter the access key to start chatting.</p>
      <input id="keyInput" type="password" placeholder="Access key" autofocus>
      <button onclick="submitKey()">Unlock</button>
      <div class="err" id="gateErr"></div>
    </div>
  </div>

  <header class="hidden" id="header">
    <h1>ARIA Chat</h1>
    <button onclick="resetThread()">New conversation</button>
  </header>
  <div id="messages" class="hidden"><div class="empty">Say hello to get started.</div></div>
  <div id="inputRow" class="hidden">
    <input id="input" type="text" placeholder="Type a message..." autofocus>
    <button id="sendBtn" onclick="send()">Send</button>
  </div>

<script>
const THREAD_KEY = "ariaChatThreadId";
const KEY_STORAGE = "ariaDashboardKey";
let threadId = localStorage.getItem(THREAD_KEY) || null;

function esc(s) {
  return (s ?? "").toString().replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
}

function authHeaders() {
  return { "X-Dashboard-Key": localStorage.getItem(KEY_STORAGE) || "" };
}

function showGate(message) {
  document.getElementById("gate").classList.remove("hidden");
  document.getElementById("header").classList.add("hidden");
  document.getElementById("messages").classList.add("hidden");
  document.getElementById("inputRow").classList.add("hidden");
  document.getElementById("gateErr").textContent = message || "";
}

function unlockUI() {
  document.getElementById("gate").classList.add("hidden");
  document.getElementById("header").classList.remove("hidden");
  document.getElementById("messages").classList.remove("hidden");
  document.getElementById("inputRow").classList.remove("hidden");
  document.getElementById("input").focus();
}

async function submitKey() {
  const val = document.getElementById("keyInput").value.trim();
  if (!val) return;
  localStorage.setItem(KEY_STORAGE, val);
  const ok = await verifyKey();
  if (ok) {
    unlockUI();
  } else {
    localStorage.removeItem(KEY_STORAGE);
    showGate("Invalid key — try again.");
  }
}

async function verifyKey() {
  try {
    const res = await fetch("/api/chat/ui/verify", { method: "POST", headers: authHeaders() });
    return res.ok;
  } catch {
    return false;
  }
}

document.getElementById("keyInput").addEventListener("keydown", e => {
  if (e.key === "Enter") submitKey();
});

(async function init() {
  if (localStorage.getItem(KEY_STORAGE) && await verifyKey()) {
    unlockUI();
  } else {
    localStorage.removeItem(KEY_STORAGE);
    showGate();
  }
})();

function addMessage(role, text) {
  const box = document.getElementById("messages");
  const empty = box.querySelector(".empty");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

async function send() {
  const input = document.getElementById("input");
  const btn = document.getElementById("sendBtn");
  const text = input.value.trim();
  if (!text) return;

  addMessage("user", text);
  input.value = "";
  btn.disabled = true;
  const pending = addMessage("assistant pending", "Thinking...");

  try {
    const res = await fetch("/api/chat/ui", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ message: text, thread_id: threadId }),
    });
    if (res.status === 401) {
      pending.remove();
      localStorage.removeItem(KEY_STORAGE);
      showGate("Session expired — enter the access key again.");
      return;
    }
    const data = await res.json();
    pending.remove();
    if (!res.ok) {
      addMessage("error", data.detail || "Something went wrong.");
      return;
    }
    threadId = data.thread_id;
    localStorage.setItem(THREAD_KEY, threadId);
    addMessage("assistant", data.reply);
  } catch (err) {
    pending.remove();
    addMessage("error", "Network error: " + err.message);
  } finally {
    btn.disabled = false;
    input.focus();
  }
}

function resetThread() {
  threadId = null;
  localStorage.removeItem(THREAD_KEY);
  document.getElementById("messages").innerHTML = '<div class="empty">Say hello to get started.</div>';
}

document.getElementById("input").addEventListener("keydown", e => {
  if (e.key === "Enter") send();
});
</script>
</body>
</html>
"""


@page_router.get("/chat-ui", response_class=HTMLResponse, include_in_schema=False)
async def chat_page():
    return HTMLResponse(_PAGE)
