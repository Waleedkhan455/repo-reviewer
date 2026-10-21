"""Background review jobs for the MCP server: queueing, progress, coalescing, expiry and owner binding.

Two layers keep coalescing and isolation compatible:
- a *work item* is one actual review run; identical requests (same key) that arrive while one is queued
  or running share it, so the review runs once;
- a *job* is one caller's handle on a work item, with an unguessable id bound to that caller. Another
  caller asking for the same review gets its own job id; looking up someone else's id says "not found".
"""

import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Literal, NamedTuple

from src import config
from src.logger import submit_in_context

Kind = Literal["repo", "changes"]
State = Literal["queued", "running", "done", "failed"]
Progress = Callable[[str], None]


class QueueFull(Exception):  # noqa: N818 - a condition, not an error type name
    """Too many reviews are queued or running; try again later."""


class JobNotFound(Exception):  # noqa: N818
    """Unknown, expired, or someone else's job id."""


@dataclass
class Work:
    key: Hashable
    kind: Kind
    created: float
    state: State = "queued"
    progress: deque[str] = field(default_factory=lambda: deque(maxlen=config.MCP_PROGRESS_KEEP))
    result: object = None
    error: str | None = None
    finished: float | None = None
    done: threading.Event = field(default_factory=threading.Event)


class JobView(NamedTuple):
    """A consistent snapshot of a job's work item."""

    state: State
    position: int  # 0 = running (or finished); n = n work items ahead in the queue
    progress: list[str]
    result: object
    error: str | None


@dataclass
class Job:
    id: str
    owner: str
    work: Work
    created: float


class JobManager:
    def __init__(self, max_workers: int = config.APP_MAX_CONCURRENT,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rr-job")
        self._lock = threading.Lock()
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._works: list[Work] = []
        self._closed = False

    # --- submitting -------------------------------------------------------------

    def submit(self, owner: str, kind: Kind, key: Hashable, fn: Callable[[Progress], object],
               on_error: Callable[[Exception], str]) -> tuple[Job, bool]:
        """Start (or join) the review for ``key``. Returns (job, joined_existing_work).

        ``fn(progress)`` does the blocking work; ``on_error`` turns an exception into a safe, user-facing
        message (never a stack trace).
        """
        with self._lock:
            self._expire()
            work = next((w for w in self._works if w.key == key and w.state in ("queued", "running")), None)
            if work is not None:
                mine = next((j for j in self._jobs.values() if j.owner == owner and j.work is work), None)
                if mine is not None:
                    return mine, True
            # Every check that can refuse happens before anything changes, so a refusal neither leaves an
            # orphaned run behind nor evicts someone's stored result.
            joined = work is not None
            if not joined and sum(w.state in ("queued", "running") for w in self._works) >= config.MCP_MAX_QUEUE:
                raise QueueFull("too many reviews are queued; try again in a few minutes")
            self._make_room()
            if work is None:
                work = Work(key=key, kind=kind, created=self._clock())
                self._works.append(work)
                submit_in_context(self._pool, self._run, work, fn, on_error)
            mine = Job(id=secrets.token_urlsafe(16), owner=owner, work=work, created=self._clock())
            self._jobs[mine.id] = mine
            return mine, joined

    def has_active(self, owner: str, key: Hashable) -> bool:
        """True when a review for ``key`` is already queued or running (so a new request would only join it).
        ``owner`` is accepted for symmetry: joining anyone's run costs no extra model calls."""
        with self._lock:
            return any(w.key == key and w.state in ("queued", "running") for w in self._works)

    def add_finished(self, owner: str, kind: Kind, result: object) -> Job:
        """A job that is already done (a cache hit), so the paging tools work on it like on any other."""
        with self._lock:
            self._expire()
            self._make_room()
            now = self._clock()
            work = Work(key=("finished", secrets.token_hex(8)), kind=kind, created=now, state="done",
                        result=result, finished=now)
            work.done.set()
            self._works.append(work)
            job = Job(id=secrets.token_urlsafe(16), owner=owner, work=work, created=now)
            self._jobs[job.id] = job
            return job

    def _run(self, work: Work, fn: Callable[[Progress], object], on_error: Callable[[Exception], str]) -> None:
        with self._lock:
            if self._closed:  # the client went away: queued reviews never start
                work.state, work.error, work.finished = "failed", "the server is shutting down", self._clock()
                work.done.set()
                return
            work.state = "running"

        def progress(message: str) -> None:
            with self._lock:
                work.progress.append(message)

        try:
            result = fn(progress)
        except Exception as exc:  # reported to the caller as a safe message, never re-raised in the pool
            message = on_error(exc)
            with self._lock:
                work.state, work.error, work.finished = "failed", message, self._clock()
        else:
            with self._lock:
                work.state, work.result, work.finished = "done", result, self._clock()
        finally:
            work.done.set()

    # --- reading ----------------------------------------------------------------

    def get(self, job_id: str, owner: str) -> Job:
        with self._lock:
            self._expire()
            job = self._jobs.get(job_id)
            if job is None or job.owner != owner:
                raise JobNotFound("unknown or expired job id")
            return job

    def snapshot(self, job: Job) -> JobView:
        with self._lock:
            work = job.work
            return JobView(work.state, self._position(work), list(work.progress), work.result, work.error)

    def wait(self, job: Job, timeout: float) -> bool:
        """Block up to ``timeout`` seconds; True once the job has finished (done or failed)."""
        return job.work.done.wait(max(0.0, timeout))

    def queue_length(self) -> int:
        with self._lock:
            return sum(w.state in ("queued", "running") for w in self._works)

    def jobs_stored(self) -> int:
        with self._lock:
            return len(self._jobs)

    # --- housekeeping (call with the lock held) -----------------------------------

    def _position(self, work: Work) -> int:
        if work.state != "queued":
            return 0
        queued = [w for w in self._works if w.state == "queued"]
        return queued.index(work) + 1

    def _expire(self) -> None:
        cutoff = self._clock() - config.MCP_JOB_TTL_S
        self._works = [w for w in self._works if w.finished is None or w.finished > cutoff]
        live = {id(w) for w in self._works}
        self._jobs = {k: j for k, j in self._jobs.items() if id(j.work) in live}

    def _make_room(self) -> None:
        """Evict the oldest finished job when the store is full, and free its result if no job uses it."""
        if len(self._jobs) < config.MCP_MAX_JOBS:
            return
        finished = sorted((j for j in self._jobs.values() if j.work.finished is not None), key=lambda j: j.created)
        if not finished:
            raise QueueFull("too many reviews are stored; try again later")
        evicted = self._jobs.pop(finished[0].id)
        if not any(j.work is evicted.work for j in self._jobs.values()):
            self._works = [w for w in self._works if w is not evicted.work]

    def shutdown(self) -> None:
        """Stop accepting work: queued reviews are cancelled; running ones are abandoned (not waited for)."""
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
