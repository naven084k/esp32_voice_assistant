"""
/data admin page - view and edit ARIA's persistent state directly (conversation memory, tasks,
song index) from a browser, gated the same way as /dashboard. This is explicitly NOT reachable
through the ESP32's own on-device web server - that's a separate physical device with no HTTP
client to this backend's API (see backend/esp32/voice_button.ino's initSdFileServer(), which only
ever serves the SD card). This page talks to the backend's own SQLite-backed services directly.

Memory (services/llm.py) only supports thread-level view/delete, not per-message editing -
LangGraph checkpoints are versioned graph state snapshots, not a flat message list, so splicing
a single past message isn't a safe operation without going through the graph's own reducers.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from services import llm, song_index
from services.auth import verify_key
from services.task_tools import add_task, complete_task, delete_task, list_tasks_raw, update_task

router = APIRouter(dependencies=[Depends(verify_key)])
page_router = APIRouter()


# ─── Memory ─────────────────────────────────────────────────────────────────

@router.get("/memory/threads")
async def get_threads():
    return await llm.list_threads()


@router.get("/memory/threads/{thread_id}")
async def get_thread(thread_id: str):
    messages = await llm.get_thread_messages(thread_id)
    if messages is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return {"thread_id": thread_id, "messages": messages}


@router.delete("/memory/threads/{thread_id}")
async def remove_thread(thread_id: str):
    await llm.delete_thread(thread_id)
    return {"status": "deleted"}


# ─── Tasks ──────────────────────────────────────────────────────────────────

class TaskIn(BaseModel):
    title: str
    due: str = ""
    description: str = ""
    priority: str = "normal"


class TaskUpdateIn(BaseModel):
    new_title: str = ""
    due: str = ""
    priority: str = ""


@router.get("/tasks")
def get_tasks(filter: str = "all"):
    return list_tasks_raw(filter)


@router.post("/tasks")
def create_task(body: TaskIn):
    message = add_task.invoke({
        "title": body.title, "due": body.due,
        "description": body.description, "priority": body.priority,
    })
    return {"message": message, "tasks": list_tasks_raw("all")}


@router.put("/tasks/{task_id}")
def edit_task(task_id: int, body: TaskUpdateIn):
    message = update_task.invoke({
        "title_or_id": str(task_id), "new_title": body.new_title,
        "due": body.due, "priority": body.priority,
    })
    return {"message": message, "tasks": list_tasks_raw("all")}


@router.post("/tasks/{task_id}/complete")
def mark_task_complete(task_id: int):
    message = complete_task.invoke({"title_or_id": str(task_id)})
    return {"message": message, "tasks": list_tasks_raw("all")}


@router.delete("/tasks/{task_id}")
def remove_task(task_id: int):
    message = delete_task.invoke({"title_or_id": str(task_id)})
    return {"message": message, "tasks": list_tasks_raw("all")}


# ─── Songs ──────────────────────────────────────────────────────────────────

class SongUpdateIn(BaseModel):
    title: str | None = None
    album: str | None = None
    path: str | None = None
    language: str | None = None
    category: str | None = None
    energy: str | None = None
    tempo: str | None = None
    description: str | None = None
    genre: list[str] | None = None
    moods: list[str] | None = None
    themes: list[str] | None = None
    keywords: list[str] | None = None
    voice_aliases: list[str] | None = None


@router.get("/songs")
def get_songs():
    return song_index.list_songs()


@router.put("/songs/{song_id}")
def edit_song(song_id: str, body: SongUpdateIn):
    updated = song_index.update_song(song_id, **body.model_dump(exclude_unset=True))
    if updated is None:
        raise HTTPException(status_code=404, detail="Song not found")
    return updated


@router.delete("/songs/{song_id}")
def remove_song(song_id: str):
    if not song_index.delete_song(song_id):
        raise HTTPException(status_code=404, detail="Song not found")
    return {"status": "deleted"}


# ─── Page ───────────────────────────────────────────────────────────────────

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ARIA — Data</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0f1115; color: #e6e6e6;
  }
  #tabs { display: flex; gap: 4px; padding: 14px 20px 0; border-bottom: 1px solid #23262e; }
  .tab {
    padding: 8px 16px; border-radius: 8px 8px 0 0; cursor: pointer; font-size: 13px;
    color: #9aa0ab; border: 1px solid transparent;
  }
  .tab.active { color: #f1f2f4; background: #14161c; border-color: #23262e; border-bottom-color: #14161c; }
  #content { padding: 20px; max-width: 1100px; margin: 0 auto; }
  .panel { display: none; }
  .panel.active { display: block; }
  .toolbar { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; align-items: center; }
  .toolbar input, .toolbar select {
    padding: 7px 10px; border-radius: 6px; border: 1px solid #2c313c; background: #14161c;
    color: #e6e6e6; font-size: 12.5px;
  }
  button {
    padding: 7px 12px; border-radius: 6px; border: 1px solid #2c313c; background: #1c1f26;
    color: #e6e6e6; font-size: 12.5px; cursor: pointer;
  }
  button:hover { background: #23262e; }
  button.primary { background: #5b8cff; border-color: #5b8cff; color: #fff; font-weight: 600; }
  button.primary:hover { background: #4577f0; }
  button.danger:hover { background: #3a1d22; border-color: #5a2a30; color: #ff8a8a; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th { text-align: left; color: #6b7280; text-transform: uppercase; font-size: 10.5px; letter-spacing: .05em;
       padding: 8px 10px; border-bottom: 1px solid #23262e; }
  td { padding: 9px 10px; border-bottom: 1px solid #1c1f26; vertical-align: top; }
  tr:hover td { background: #14161c; }
  .chip { display: inline-block; background: #1c1f26; border: 1px solid #2c313c; border-radius: 10px;
          padding: 1px 8px; font-size: 10.5px; color: #9aa0ab; margin: 1px 2px 1px 0; }
  .muted { color: #6b7280; }
  .row-actions { display: flex; gap: 6px; }
  .empty { color: #565b66; padding: 30px 4px; font-size: 13px; }
  .split { display: flex; gap: 20px; align-items: flex-start; }
  .split > div { flex: 1; min-width: 0; }
  .msg { border-radius: 8px; padding: 8px 12px; margin-bottom: 8px; font-size: 12.5px; white-space: pre-wrap; }
  .msg.human { background: #14213f; }
  .msg.ai { background: #142d1f; }
  .msg.system { background: #1c1f26; color: #8a8f99; }
  .msg.tool { background: #241c14; color: #d7b98a; }
  .msg .role { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: #6b7280; margin-bottom: 3px; }
  textarea, .editfield input, .editfield select {
    width: 100%; padding: 6px 8px; border-radius: 6px; border: 1px solid #2c313c;
    background: #0f1115; color: #e6e6e6; font-size: 12px; margin-bottom: 6px;
  }
  .editgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 12px; margin-top: 8px; }
  .editgrid label { font-size: 10px; color: #6b7280; text-transform: uppercase; letter-spacing: .04em; display: block; margin-bottom: 2px; }

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
  #gate button { width: 100%; background: #5b8cff; color: #fff; font-weight: 600; }
  #gate button:hover { background: #4577f0; }
  #gate .err { color: #ff8a8a; font-size: 12px; margin-top: 10px; min-height: 14px; }
  .hidden { display: none !important; }
</style>
</head>
<body>
  <div id="gate">
    <div class="box">
      <h2>ARIA Data</h2>
      <p>Enter the access key to view and edit stored data.</p>
      <input id="keyInput" type="password" placeholder="Access key" autofocus>
      <button onclick="submitKey()">Unlock</button>
      <div class="err" id="gateErr"></div>
    </div>
  </div>

  <div id="app" class="hidden">
    <div id="tabs">
      <div class="tab active" data-tab="memory" onclick="showTab('memory')">Memory</div>
      <div class="tab" data-tab="tasks" onclick="showTab('tasks')">Tasks</div>
      <div class="tab" data-tab="songs" onclick="showTab('songs')">Songs</div>
    </div>

    <div id="content">
      <div class="panel active" id="panel-memory">
        <div class="toolbar">
          <button onclick="loadThreads()">Refresh</button>
        </div>
        <div class="split">
          <div>
            <table id="threadsTable">
              <thead><tr><th>Thread</th><th>Checkpoints</th><th></th></tr></thead>
              <tbody></tbody>
            </table>
          </div>
          <div id="threadDetail"><div class="empty">Select a thread to view its conversation.</div></div>
        </div>
      </div>

      <div class="panel" id="panel-tasks">
        <div class="toolbar">
          <input id="taskTitle" placeholder="Title">
          <input id="taskDue" type="datetime-local">
          <select id="taskPriority"><option>low</option><option selected>normal</option><option>high</option></select>
          <button class="primary" onclick="createTask()">Add task</button>
          <button onclick="loadTasks()">Refresh</button>
        </div>
        <table id="tasksTable">
          <thead><tr><th>#</th><th>Title</th><th>Priority</th><th>Due</th><th>Status</th><th></th></tr></thead>
          <tbody></tbody>
        </table>
      </div>

      <div class="panel" id="panel-songs">
        <div class="toolbar">
          <input id="songSearch" placeholder="Filter by title/album..." oninput="renderSongs()">
          <button onclick="loadSongs()">Refresh</button>
        </div>
        <table id="songsTable">
          <thead><tr><th>Title</th><th>Album</th><th>Language</th><th>Moods</th><th></th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  </div>

<script>
const KEY_STORAGE = "ariaDashboardKey";
let allSongs = [];

function esc(s) {
  return (s ?? "").toString().replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
}
function authHeaders(json) {
  const h = { "X-Dashboard-Key": localStorage.getItem(KEY_STORAGE) || "" };
  if (json) h["Content-Type"] = "application/json";
  return h;
}
async function api(path, options) {
  const res = await fetch(path, { ...options, headers: { ...authHeaders(true), ...(options?.headers || {}) } });
  if (res.status === 401) {
    localStorage.removeItem(KEY_STORAGE);
    showGate("Invalid key — try again.");
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

function showGate(message) {
  document.getElementById("gate").classList.remove("hidden");
  document.getElementById("app").classList.add("hidden");
  document.getElementById("gateErr").textContent = message || "";
}
function unlockUI() {
  document.getElementById("gate").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
}
function submitKey() {
  const val = document.getElementById("keyInput").value.trim();
  if (!val) return;
  localStorage.setItem(KEY_STORAGE, val);
  unlockUI();
  loadAll();
}
document.getElementById("keyInput").addEventListener("keydown", e => { if (e.key === "Enter") submitKey(); });

function showTab(name) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach(p => p.classList.toggle("active", p.id === "panel-" + name));
}

function loadAll() { loadThreads(); loadTasks(); loadSongs(); }

// ---- Memory ----
async function loadThreads() {
  const threads = await api("/api/memory/threads").catch(() => []);
  const tbody = document.querySelector("#threadsTable tbody");
  tbody.innerHTML = "";
  if (!threads.length) { tbody.innerHTML = `<tr><td colspan="3" class="empty">No conversations yet.</td></tr>`; return; }
  for (const t of threads) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${esc(t.thread_id.slice(0, 12))}…</code></td>
      <td>${t.checkpoints}</td>
      <td class="row-actions">
        <button onclick="viewThread('${t.thread_id}')">View</button>
        <button class="danger" onclick="deleteThread('${t.thread_id}')">Delete</button>
      </td>`;
    tbody.appendChild(tr);
  }
}
async function viewThread(threadId) {
  const detail = document.getElementById("threadDetail");
  detail.innerHTML = `<div class="empty">Loading…</div>`;
  const data = await api(`/api/memory/threads/${threadId}`).catch(() => null);
  if (!data) { detail.innerHTML = `<div class="empty">Thread not found.</div>`; return; }
  detail.innerHTML = data.messages.map(m => `
    <div class="msg ${esc(m.role)}">
      <div class="role">${esc(m.role)}</div>
      <div>${esc(m.content) || "<em>(empty)</em>"}</div>
    </div>`).join("") || `<div class="empty">No messages.</div>`;
}
async function deleteThread(threadId) {
  if (!confirm("Delete this entire conversation permanently?")) return;
  await api(`/api/memory/threads/${threadId}`, { method: "DELETE" });
  document.getElementById("threadDetail").innerHTML = `<div class="empty">Select a thread to view its conversation.</div>`;
  loadThreads();
}

// ---- Tasks ----
// due_at is stored as "YYYY-MM-DD HH:MM" (time-specific) or "YYYY-MM-DD" (date-only soft
// deadline) - see services/task_tools.py's _parse_due(). <input type="datetime-local"> needs
// "YYYY-MM-DDTHH:MM" and always carries a time, so a date-only value gets midnight filled in
// on the way in; fromLocalDT() swaps the "T" back before it's sent to the API.
function toLocalDT(due) {
  if (!due) return "";
  return (due.length > 10 ? due : due + " 00:00").replace(" ", "T");
}
function fromLocalDT(v) {
  return v ? v.replace("T", " ") : "";
}
async function loadTasks() {
  const tasks = await api("/api/tasks?filter=all").catch(() => []);
  renderTasks(tasks);
}
function renderTasks(tasks) {
  const tbody = document.querySelector("#tasksTable tbody");
  tbody.innerHTML = "";
  if (!tasks.length) { tbody.innerHTML = `<tr><td colspan="6" class="empty">No tasks yet.</td></tr>`; return; }
  for (const t of tasks) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${t.id}</td>
      <td>${esc(t.title)}</td>
      <td>${esc(t.priority)}</td>
      <td class="muted">${esc(t.due_at) || "—"}</td>
      <td>${t.status === "completed" ? "✓ done" : "pending"}</td>
      <td class="row-actions">
        <button onclick="toggleEditTask(${t.id})">Edit</button>
        ${t.status !== "completed" ? `<button onclick="completeTask(${t.id})">Complete</button>` : ""}
        <button class="danger" onclick="deleteTaskRow(${t.id})">Delete</button>
      </td>`;
    tbody.appendChild(tr);
    const editRow = document.createElement("tr");
    editRow.id = `edit-task-${t.id}`;
    editRow.className = "hidden";
    editRow.innerHTML = `<td colspan="6">${taskEditForm(t)}</td>`;
    tbody.appendChild(editRow);
  }
}
function taskEditForm(t) {
  const opt = p => `<option${t.priority === p ? " selected" : ""}>${p}</option>`;
  return `
    <div class="editgrid">
      <div class="editfield"><label>Title</label><input id="tf-title-${t.id}" value="${esc(t.title)}"></div>
      <div class="editfield"><label>Due (date &amp; time)</label>
        <input id="tf-due-${t.id}" type="datetime-local" value="${toLocalDT(t.due_at)}"></div>
      <div class="editfield"><label>Priority</label>
        <select id="tf-priority-${t.id}">${opt("low")}${opt("normal")}${opt("high")}</select></div>
    </div>
    <div class="row-actions" style="margin-top:6px;">
      <button class="primary" onclick="saveTask(${t.id})">Save</button>
      <button onclick="toggleEditTask(${t.id})">Cancel</button>
    </div>`;
}
function toggleEditTask(id) {
  document.getElementById(`edit-task-${id}`).classList.toggle("hidden");
}
async function saveTask(id) {
  const newTitle = document.getElementById(`tf-title-${id}`).value.trim();
  const due = fromLocalDT(document.getElementById(`tf-due-${id}`).value);
  const priority = document.getElementById(`tf-priority-${id}`).value;
  const { tasks } = await api(`/api/tasks/${id}`, { method: "PUT", body: JSON.stringify({ new_title: newTitle, due, priority }) });
  renderTasks(tasks);
}
async function createTask() {
  const title = document.getElementById("taskTitle").value.trim();
  if (!title) return;
  const due = fromLocalDT(document.getElementById("taskDue").value);
  const priority = document.getElementById("taskPriority").value;
  const { tasks } = await api("/api/tasks", { method: "POST", body: JSON.stringify({ title, due, priority }) });
  document.getElementById("taskTitle").value = "";
  document.getElementById("taskDue").value = "";
  renderTasks(tasks);
}
async function completeTask(id) {
  const { tasks } = await api(`/api/tasks/${id}/complete`, { method: "POST" });
  renderTasks(tasks);
}
async function deleteTaskRow(id) {
  if (!confirm("Delete this task permanently?")) return;
  const { tasks } = await api(`/api/tasks/${id}`, { method: "DELETE" });
  renderTasks(tasks);
}

// ---- Songs ----
async function loadSongs() {
  allSongs = await api("/api/songs").catch(() => []);
  renderSongs();
}
function renderSongs() {
  const filter = (document.getElementById("songSearch").value || "").toLowerCase();
  const tbody = document.querySelector("#songsTable tbody");
  tbody.innerHTML = "";
  const filtered = allSongs.filter(s => !filter || s.title.toLowerCase().includes(filter) || s.album.toLowerCase().includes(filter));
  if (!filtered.length) { tbody.innerHTML = `<tr><td colspan="5" class="empty">No songs found.</td></tr>`; return; }
  for (const s of filtered) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(s.title)}</td>
      <td>${esc(s.album)}</td>
      <td class="muted">${esc(s.language)}</td>
      <td>${(s.moods || []).map(m => `<span class="chip">${esc(m)}</span>`).join("")}</td>
      <td class="row-actions">
        <button onclick="toggleEditSong('${s.id}')">Edit</button>
        <button class="danger" onclick="deleteSongRow('${s.id}')">Delete</button>
      </td>`;
    tbody.appendChild(tr);
    const editRow = document.createElement("tr");
    editRow.id = `edit-${s.id}`;
    editRow.className = "hidden";
    editRow.innerHTML = `<td colspan="5">${songEditForm(s)}</td>`;
    tbody.appendChild(editRow);
  }
}
function songEditForm(s) {
  const field = (key, label, isList) => `
    <div class="editfield">
      <label>${label}</label>
      <input id="f-${key}-${s.id}" value="${esc(isList ? (s[key] || []).join(', ') : (s[key] ?? ''))}">
    </div>`;
  return `
    <div class="editgrid">
      ${field("title", "Title")}
      ${field("album", "Album")}
      ${field("path", "Path (relative to SONGS_ROOT)")}
      ${field("language", "Language")}
      ${field("category", "Category")}
      ${field("energy", "Energy")}
      ${field("tempo", "Tempo")}
      ${field("genre", "Genre (comma-separated)", true)}
      ${field("moods", "Moods (comma-separated)", true)}
      ${field("themes", "Themes (comma-separated)", true)}
      ${field("keywords", "Keywords (comma-separated)", true)}
      ${field("voice_aliases", "Voice aliases (comma-separated)", true)}
    </div>
    <div class="editfield"><label>Description</label><input id="f-description-${s.id}" value="${esc(s.description)}"></div>
    <div class="row-actions" style="margin-top:6px;">
      <button class="primary" onclick="saveSong('${s.id}')">Save</button>
      <button onclick="toggleEditSong('${s.id}')">Cancel</button>
    </div>`;
}
function toggleEditSong(id) {
  document.getElementById(`edit-${id}`).classList.toggle("hidden");
}
async function saveSong(id) {
  const listVal = key => document.getElementById(`f-${key}-${id}`).value.split(",").map(s => s.trim()).filter(Boolean);
  const strVal = key => document.getElementById(`f-${key}-${id}`).value;
  const body = {
    title: strVal("title"), album: strVal("album"), path: strVal("path"),
    language: strVal("language"), category: strVal("category"), energy: strVal("energy"),
    tempo: strVal("tempo"), description: strVal("description"),
    genre: listVal("genre"), moods: listVal("moods"), themes: listVal("themes"),
    keywords: listVal("keywords"), voice_aliases: listVal("voice_aliases"),
  };
  await api(`/api/songs/${id}`, { method: "PUT", body: JSON.stringify(body) });
  loadSongs();
}
async function deleteSongRow(id) {
  if (!confirm("Remove this song from the index permanently?")) return;
  await api(`/api/songs/${id}`, { method: "DELETE" });
  loadSongs();
}

if (localStorage.getItem(KEY_STORAGE)) { unlockUI(); loadAll(); } else { showGate(); }
</script>
</body>
</html>
"""


@page_router.get("/data", response_class=HTMLResponse, include_in_schema=False)
async def data_page():
    return HTMLResponse(_PAGE)
