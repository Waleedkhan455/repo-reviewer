"""Abuse limits for the public web app.

- a global daily cap on fresh (non-cached) reviews, refunded when a run fails before any LLM call
- a cap on concurrent reviews and on concurrent cache lookups (`git ls-remote`)
- a per-client cooldown keyed by client address, so reloading the page does not reset it

Pure Python (no Streamlit) so it can be unit-tested. The app keeps one shared instance per process.
"""

import threading
import time
from collections.abc import Callable
from datetime import date

MAX_TRACKED_CLIENTS = 10_000


class Quota:
    def __init__(self, daily_cap: int, max_concurrent: int, max_lookups: int, cooldown_s: int,
                 today: Callable[[], date] = date.today, clock: Callable[[], float] = time.time) -> None:
        self.daily_cap = daily_cap
        self.max_concurrent = max_concurrent
        self.cooldown_s = cooldown_s
        self._today = today
        self._clock = clock
        self._lock = threading.Lock()
        self._lookups = threading.BoundedSemaphore(max_lookups)
        self._day = today()
        self._runs = 0
        self._active = 0
        self._last_start: dict[str, float] = {}

    # --- cache lookups ---------------------------------------------------------

    def try_lookup(self) -> bool:
        """Reserve one cache-lookup slot without waiting. Call end_lookup() afterwards."""
        return self._lookups.acquire(blocking=False)

    def end_lookup(self) -> None:
        self._lookups.release()

    # --- fresh reviews ---------------------------------------------------------

    def cooldown_left(self, client: str) -> int:
        with self._lock:
            last = self._last_start.get(client)
        return 0 if last is None else max(0, int(last + self.cooldown_s - self._clock()))

    def acquire(self, client: str) -> str | None:
        """Reserve one fresh review. Returns a refusal message, or None if the review may start."""
        with self._lock:
            if self._today() != self._day:
                self._day, self._runs = self._today(), 0
            last = self._last_start.get(client)
            if last is not None and self._clock() - last < self.cooldown_s:
                return f"Please wait {int(last + self.cooldown_s - self._clock())}s before starting another review."
            if self._active >= self.max_concurrent:
                return "The server is busy with other reviews. Please try again in a minute."
            if self._runs >= self.daily_cap:
                return "The free daily review limit has been reached. Please try again tomorrow."
            self._runs += 1
            self._active += 1
            if len(self._last_start) >= MAX_TRACKED_CLIENTS:
                self._last_start.clear()  # bounded memory; worst case one extra review per client
            self._last_start[client] = self._clock()
            return None

    def release(self, *, refund: bool = False) -> None:
        """End a review. ``refund`` returns the daily slot (the run failed before any LLM call)."""
        with self._lock:
            self._active = max(0, self._active - 1)
            if refund:
                self._runs = max(0, self._runs - 1)

    @property
    def runs_today(self) -> int:
        return self._runs
