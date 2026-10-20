"""JSONL trace log plus per-run statistics.

Each review run sets a RunStats object in a ContextVar. Worker threads are started
with ``submit_in_context`` so they share the same RunStats instance.
"""

import contextlib
import contextvars
import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Executor, Future
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TypeVar

from src import config

T = TypeVar("T")

_write_lock = threading.Lock()


@dataclass
class RunStats:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    llm_calls: int = 0
    llm_errors: int = 0
    llm_seconds: float = 0.0
    calls_by_agent: dict[str, int] = field(default_factory=dict)
    models_used: dict[str, int] = field(default_factory=dict)  # successful calls per model
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_call(self, latency_s: float, ok: bool, agent: str = "", model: str = "") -> None:
        with self._lock:
            self.llm_calls += 1
            self.llm_seconds += latency_s
            self.calls_by_agent[agent] = self.calls_by_agent.get(agent, 0) + 1
            if not ok:
                self.llm_errors += 1
            elif model:
                self.models_used[model] = self.models_used.get(model, 0) + 1


current_run: ContextVar[RunStats | None] = ContextVar("current_run", default=None)


def log_event(event: str, **fields) -> None:
    """Append one JSON line to logs/trace.jsonl. Never log secrets or prompt text."""
    run = current_run.get()
    record = {"ts": round(time.time(), 3), "run_id": run.run_id if run else None, "event": event, **fields}
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _write_lock:
        try:
            config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
            if config.TRACE_FILE.exists() and config.TRACE_FILE.stat().st_size > config.TRACE_MAX_BYTES:
                with contextlib.suppress(OSError):  # e.g. a reader holds the file open on Windows
                    config.TRACE_FILE.replace(config.TRACE_FILE.with_suffix(".jsonl.1"))  # keep one old file
            with open(config.TRACE_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # logging must never fail a review


def submit_in_context(pool: Executor, fn: Callable[..., T], *args: object) -> Future[T]:
    """Submit to a thread pool so the task sees this thread's ContextVars (the current RunStats)."""
    return pool.submit(contextvars.copy_context().run, fn, *args)


def read_trace(run_id: str, limit: int | None = None) -> list[dict]:
    """Trace events of one run (for the UI trace viewer). Missing or unreadable lines are skipped."""
    events = []
    try:
        with open(config.TRACE_FILE, encoding="utf-8") as f:
            for line in f:
                if run_id not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("run_id") == run_id:
                    events.append(event)
    except OSError:
        return []
    return events[-(limit or config.TRACE_VIEW_MAX_EVENTS):]
