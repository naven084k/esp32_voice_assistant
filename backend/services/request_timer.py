import time
import uuid
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field, asdict
from datetime import datetime

_W = 68


@dataclass
class _Row:
    layer: str
    duration: float
    detail: str = ""


@dataclass
class ToolCallRecord:
    name: str
    args: str = ""
    output: str = ""
    duration: float = 0.0
    ok: bool = True
    error: str = ""


@dataclass
class RequestTimer:
    label: str = ""
    thread_id: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    _start: float = field(default_factory=time.perf_counter)
    _rows: list[_Row] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    def record(self, layer: str, duration: float, detail: str = ""):
        self._rows.append(_Row(layer, duration, detail))

    def add_tool_call(self, call: ToolCallRecord):
        self.tool_calls.append(call)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "thread_id": self.thread_id,
            "timestamp": self.timestamp,
            "total_duration": round(time.perf_counter() - self._start, 3),
            "tool_calls": [asdict(tc) for tc in self.tool_calls],
        }

    def log_table(self):
        _history.appendleft(self.snapshot())


_current: ContextVar[RequestTimer | None] = ContextVar("request_timer", default=None)
_history: deque[dict] = deque(maxlen=100)


def new_timer(label: str = "", thread_id: str = "") -> RequestTimer:
    t = RequestTimer(label=label, thread_id=thread_id)
    _current.set(t)
    return t


def get_timer() -> RequestTimer | None:
    return _current.get()


def get_history() -> list[dict]:
    return list(_history)


def get_request(request_id: str) -> dict | None:
    return next((r for r in _history if r["id"] == request_id), None)
