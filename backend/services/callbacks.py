import time
from uuid import UUID
from langchain_core.callbacks import AsyncCallbackHandler
from services.request_timer import get_timer, ToolCallRecord


def _fmt_args(inputs, input_str: str) -> str:
    if isinstance(inputs, dict) and inputs:
        return ", ".join(f"{k}={v!r}" for k, v in inputs.items())
    return input_str or ""


class TimingCallbackHandler(AsyncCallbackHandler):

    def __init__(self):
        self._timers: dict[UUID, float] = {}
        self._tool_meta: dict[UUID, tuple[str, str]] = {}
        self._llm_call_count = 0

    async def on_llm_start(self, serialized: dict, prompts: list, *, run_id: UUID, **kwargs):
        self._timers[run_id] = time.perf_counter()

    async def on_llm_end(self, response, *, run_id: UUID, **kwargs):
        elapsed = time.perf_counter() - self._timers.pop(run_id, time.perf_counter())
        usage = (response.llm_output or {}).get("token_usage", {})
        tokens = f"prompt={usage.get('prompt_tokens','?')} comp={usage.get('completion_tokens','?')}" if usage else ""
        self._llm_call_count += 1
        t = get_timer()
        if t:
            t.record(f"LLM #{self._llm_call_count}", elapsed, tokens)

    async def on_llm_error(self, error: Exception, *, run_id: UUID, **kwargs):
        elapsed = time.perf_counter() - self._timers.pop(run_id, time.perf_counter())
        t = get_timer()
        if t:
            t.record("LLM #err", elapsed, str(error)[:40])

    async def on_tool_start(self, serialized: dict, input_str: str, *, run_id: UUID, inputs: dict | None = None, **kwargs):
        self._timers[run_id] = time.perf_counter()
        name = serialized.get("name", "tool")
        self._tool_meta[run_id] = (name, _fmt_args(inputs, input_str))

    async def on_tool_end(self, output, *, run_id: UUID, **kwargs):
        elapsed = time.perf_counter() - self._timers.pop(run_id, time.perf_counter())
        name, args = self._tool_meta.pop(run_id, ("tool", ""))
        out_str = str(output)
        t = get_timer()
        if t:
            preview = out_str[:60].replace("\n", " ")
            t.record("Tool", elapsed, preview[:36])
            t.add_tool_call(ToolCallRecord(name=name, args=args, output=out_str[:500], duration=round(elapsed, 3)))

    async def on_tool_error(self, error: Exception, *, run_id: UUID, **kwargs):
        elapsed = time.perf_counter() - self._timers.pop(run_id, time.perf_counter())
        name, args = self._tool_meta.pop(run_id, ("tool", ""))
        t = get_timer()
        if t:
            t.record("Tool (err)", elapsed, str(error)[:40])
            t.add_tool_call(ToolCallRecord(name=name, args=args, duration=round(elapsed, 3), ok=False, error=str(error)[:500]))
