from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from services.auth import verify_key
from services.request_timer import get_history, get_request

router = APIRouter(dependencies=[Depends(verify_key)])
page_router = APIRouter()


@router.get("/requests")
async def list_requests():
    """Most recent requests first, newest 100 kept in memory."""
    return get_history()


@router.get("/requests/{request_id}")
async def request_detail(request_id: str):
    req = get_request(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    return req


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ARIA — Tool Calls</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; height: 100vh; display: flex;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0f1115; color: #e6e6e6;
  }
  #list { width: 340px; border-right: 1px solid #23262e; overflow-y: auto; flex-shrink: 0; }
  #list h1 { font-size: 15px; padding: 14px 16px 10px; margin: 0; color: #9aa0ab; letter-spacing: .02em; }
  .row {
    padding: 10px 16px; border-bottom: 1px solid #1c1f26; cursor: pointer;
  }
  .row:hover { background: #171a21; }
  .row.active { background: #1b2130; border-left: 3px solid #5b8cff; }
  .row .msg { font-size: 13.5px; color: #dfe3ea; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .row .meta { font-size: 11px; color: #6b7280; margin-top: 3px; display: flex; gap: 8px; align-items: center; }
  .badge { background: #23262e; padding: 1px 6px; border-radius: 10px; font-size: 10.5px; color: #9aa0ab; }
  .badge.err { background: #3a1d22; color: #ff8a8a; }
  #detail { flex: 1; overflow-y: auto; padding: 24px 32px; }
  #detail h2 { font-size: 16px; margin: 0 0 4px; color: #f1f2f4; }
  #detail .sub { font-size: 12px; color: #6b7280; margin-bottom: 20px; }
  .empty { color: #565b66; padding: 40px; font-size: 14px; }
  .timeline { margin-bottom: 22px; }
  .timeline h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: #6b7280; margin: 0 0 10px; }
  .step-row { margin-bottom: 8px; }
  .step-label {
    display: flex; align-items: baseline; gap: 8px; font-size: 12.5px;
    color: #cfe0ff; margin-bottom: 3px;
  }
  .step-label .dur { color: #7c8595; font-variant-numeric: tabular-nums; }
  .step-label .detail { color: #6b7280; font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .step-track { position: relative; height: 8px; background: #14161c; border-radius: 4px; overflow: hidden; }
  .step-bar { position: absolute; top: 0; height: 100%; border-radius: 4px; background: #5b8cff; }
  .step-bar.tool { background: #3ecf7a; }
  .step-bar.err { background: #ff5c5c; }

  .call {
    background: #14161c; border: 1px solid #23262e; border-radius: 10px;
    margin-bottom: 12px; overflow: hidden;
  }
  .call-head {
    display: flex; align-items: center; gap: 10px; padding: 10px 14px;
    background: #181b22; font-size: 13px;
  }
  .call-head .name { font-weight: 600; color: #cfe0ff; }
  .call-head .dur { margin-left: auto; color: #7c8595; font-variant-numeric: tabular-nums; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #3ecf7a; flex-shrink: 0; }
  .dot.err { background: #ff5c5c; }
  .call-body { padding: 10px 14px; font-size: 12.5px; }
  .field { margin-bottom: 6px; }
  .field .k { color: #6b7280; text-transform: uppercase; font-size: 10px; letter-spacing: .05em; margin-bottom: 2px; }
  .field .v { color: #d7dae0; white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .field .v.error { color: #ff8a8a; }
  #refresh { position: absolute; top: 10px; right: 16px; background: none; border: 1px solid #2c313c; color: #9aa0ab; border-radius: 6px; padding: 4px 10px; font-size: 11px; cursor: pointer; }
  #refresh:hover { background: #1c1f26; }

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
  .hidden { display: none !important; }
</style>
</head>
<body>
  <div id="gate">
    <div class="box">
      <h2>ARIA Dashboard</h2>
      <p>Enter the access key to view tool call activity.</p>
      <input id="keyInput" type="password" placeholder="Access key" autofocus>
      <button onclick="submitKey()">Unlock</button>
      <div class="err" id="gateErr"></div>
    </div>
  </div>

  <div id="list" class="hidden"><h1>Requests</h1></div>
  <div id="detail" class="hidden"><div class="empty">Select a request to see the tools it called.</div></div>
  <button id="refresh" class="hidden" onclick="loadList()">Refresh</button>

<script>
let selected = null;
let pollTimer = null;
const KEY_STORAGE = "ariaDashboardKey";

function esc(s) {
  return (s ?? "").toString().replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
}

function authHeaders() {
  return { "X-Dashboard-Key": localStorage.getItem(KEY_STORAGE) || "" };
}

function showGate(message) {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  document.getElementById("gate").classList.remove("hidden");
  document.getElementById("list").classList.add("hidden");
  document.getElementById("detail").classList.add("hidden");
  document.getElementById("refresh").classList.add("hidden");
  document.getElementById("gateErr").textContent = message || "";
}

function unlockUI() {
  document.getElementById("gate").classList.add("hidden");
  document.getElementById("list").classList.remove("hidden");
  document.getElementById("detail").classList.remove("hidden");
  document.getElementById("refresh").classList.remove("hidden");
}

function submitKey() {
  const val = document.getElementById("keyInput").value.trim();
  if (!val) return;
  localStorage.setItem(KEY_STORAGE, val);
  unlockUI();
  startPolling();
}

document.getElementById("keyInput").addEventListener("keydown", e => {
  if (e.key === "Enter") submitKey();
});

async function loadList() {
  const res = await fetch("/api/requests", { headers: authHeaders() });
  if (res.status === 401) {
    localStorage.removeItem(KEY_STORAGE);
    showGate("Invalid key — try again.");
    return;
  }
  const items = await res.json();
  const list = document.getElementById("list");
  list.innerHTML = "<h1>Requests</h1>";
  if (!items.length) {
    list.innerHTML += '<div class="empty">No requests yet.</div>';
    return;
  }
  for (const r of items) {
    const row = document.createElement("div");
    row.className = "row" + (r.id === selected ? " active" : "");
    const errCount = r.tool_calls.filter(c => !c.ok).length;
    row.innerHTML = `
      <div class="msg">${esc(r.label) || "(no label)"}</div>
      <div class="meta">
        <span>${new Date(r.timestamp).toLocaleTimeString()}</span>
        <span class="badge">${r.tool_calls.length} tool${r.tool_calls.length===1?"":"s"}</span>
        ${errCount ? `<span class="badge err">${errCount} error</span>` : ""}
        <span>${r.total_duration}s</span>
      </div>`;
    row.onclick = () => selectRequest(r.id);
    list.appendChild(row);
  }
}

async function selectRequest(id) {
  selected = id;
  const res = await fetch(`/api/requests/${id}`, { headers: authHeaders() });
  if (res.status === 401) {
    localStorage.removeItem(KEY_STORAGE);
    showGate("Invalid key — try again.");
    return;
  }
  if (!res.ok) return;
  const r = await res.json();
  document.querySelectorAll(".row").forEach(el => el.classList.remove("active"));
  loadList();

  const detail = document.getElementById("detail");
  let html = `<h2>${esc(r.label)}</h2><div class="sub">thread ${esc(r.thread_id)} · ${r.total_duration}s total · ${r.tool_calls.length} tool call(s)</div>`;

  const steps = r.steps || [];
  if (steps.length) {
    html += `<div class="timeline"><h3>Timeline</h3>`;
    const total = r.total_duration || 1;
    for (const s of steps) {
      const left = Math.max(0, (s.start_offset / total) * 100);
      const width = Math.max((s.duration / total) * 100, 0.5);
      const barClass = s.type === "tool" ? (s.ok === false ? "tool err" : "tool") : (s.ok === false ? "err" : "");
      html += `
        <div class="step-row">
          <div class="step-label">
            <span>${esc(s.layer)}</span>
            <span class="dur">${s.duration}s</span>
            <span class="detail">${esc(s.detail)}</span>
          </div>
          <div class="step-track">
            <div class="step-bar ${barClass}" style="left:${left}%; width:${width}%;"></div>
          </div>
        </div>`;
    }
    html += `</div>`;
  }

  if (!r.tool_calls.length) {
    detail.innerHTML = html;
    return;
  }
  for (const c of r.tool_calls) {
    html += `
      <div class="call">
        <div class="call-head">
          <span class="dot ${c.ok ? "" : "err"}"></span>
          <span class="name">${esc(c.name)}</span>
          <span class="dur">${c.duration}s</span>
        </div>
        <div class="call-body">
          <div class="field"><div class="k">Args</div><div class="v">${esc(c.args) || "—"}</div></div>
          ${c.ok
            ? `<div class="field"><div class="k">Output</div><div class="v">${esc(c.output) || "—"}</div></div>`
            : `<div class="field"><div class="k">Error</div><div class="v error">${esc(c.error)}</div></div>`}
        </div>
      </div>`;
  }
  detail.innerHTML = html;
}

function startPolling() {
  loadList();
  if (!pollTimer) {
    pollTimer = setInterval(loadList, 4000);
  }
}

if (localStorage.getItem(KEY_STORAGE)) {
  unlockUI();
  startPolling();
} else {
  showGate();
}
</script>
</body>
</html>
"""


@page_router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page():
    return HTMLResponse(_PAGE)
