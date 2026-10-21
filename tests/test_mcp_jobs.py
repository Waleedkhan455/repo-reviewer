"""Background jobs for the MCP server (src/mcp_server/jobs.py), driven by a fake slow service."""

import threading

import pytest

from src import config
from src.mcp_server.jobs import JobManager, JobNotFound, QueueFull


class Gate:
    """A fake review that blocks until released, reporting progress."""

    def __init__(self, result="RESULT", error=None):
        self.release = threading.Event()
        self.started = threading.Event()
        self.result, self.error = result, error

    def __call__(self, progress):
        self.started.set()
        progress("cloning")
        self.release.wait(5)
        progress("done")
        if self.error:
            raise self.error
        return self.result


def _safe(exc):
    return "review failed"


@pytest.fixture
def jm():
    m = JobManager(max_workers=2)
    yield m
    m.shutdown()


def test_lifecycle_queued_running_done(jm):
    gate = Gate()
    job, joined = jm.submit("alice", "repo", ("repo", "a/b"), gate, _safe)
    assert not joined and len(job.id) >= 20
    assert gate.started.wait(2)
    state, position, progress, result, error = jm.snapshot(job)
    assert state == "running" and position == 0 and "cloning" in progress
    gate.release.set()
    assert jm.wait(job, 2)
    state, _, progress, result, error = jm.snapshot(job)
    assert (state, result, error) == ("done", "RESULT", None) and progress[-1] == "done"


def test_failure_path_returns_safe_message(jm):
    gate = Gate(error=RuntimeError("Traceback ... secret path C:/internal"))
    job, _ = jm.submit("alice", "repo", "k", gate, _safe)
    gate.release.set()
    assert jm.wait(job, 2)
    state, _, _, result, error = jm.snapshot(job)
    assert (state, result, error) == ("failed", None, "review failed")


def test_coalescing_same_owner_returns_same_job(jm):
    gate = Gate()
    first, _ = jm.submit("alice", "repo", "same-key", gate, _safe)
    again, joined = jm.submit("alice", "repo", "same-key", Gate(), _safe)
    assert joined and again.id == first.id
    gate.release.set()


def test_coalescing_across_owners_shares_work_but_not_job_ids(jm):
    gate = Gate()
    calls = []

    def counted(progress):
        calls.append(1)
        return gate(progress)

    a, _ = jm.submit("alice", "repo", "same-key", counted, _safe)
    b, joined = jm.submit("bob", "repo", "same-key", counted, _safe)
    assert joined and a.id != b.id and a.work is b.work
    gate.release.set()
    assert jm.wait(b, 2) and calls == [1]  # the review ran once
    with pytest.raises(JobNotFound):
        jm.get(a.id, "bob")  # owner isolation
    assert jm.get(a.id, "alice") is a


def test_queue_full(jm, monkeypatch):
    monkeypatch.setattr(config, "MCP_MAX_QUEUE", 2)
    gates = [Gate(), Gate()]
    jm.submit("a", "repo", 1, gates[0], _safe)
    jm.submit("a", "repo", 2, gates[1], _safe)
    with pytest.raises(QueueFull):
        jm.submit("a", "repo", 3, Gate(), _safe)
    jm.submit("b", "repo", 1, Gate(), _safe)  # joining existing work is still allowed
    for g in gates:
        g.release.set()


def test_queue_positions(monkeypatch):
    jm = JobManager(max_workers=1)
    try:
        g1, g2 = Gate(), Gate()
        first, _ = jm.submit("a", "repo", 1, g1, _safe)
        second, _ = jm.submit("a", "repo", 2, g2, _safe)
        assert g1.started.wait(2)
        assert jm.snapshot(first)[1] == 0 and jm.snapshot(second)[1] == 1
        assert jm.queue_length() == 2
        g1.release.set()
        g2.release.set()
        assert jm.wait(second, 2)
    finally:
        jm.shutdown()


def test_ttl_expiry(monkeypatch):
    now = [1000.0]
    jm = JobManager(max_workers=1, clock=lambda: now[0])
    try:
        gate = Gate()
        gate.release.set()
        job, _ = jm.submit("a", "repo", "k", gate, _safe)
        assert jm.wait(job, 2)
        assert jm.get(job.id, "a")
        now[0] += config.MCP_JOB_TTL_S + 1
        with pytest.raises(JobNotFound):
            jm.get(job.id, "a")
        assert jm.jobs_stored() == 0
    finally:
        jm.shutdown()


def test_unknown_job_id(jm):
    with pytest.raises(JobNotFound):
        jm.get("not-a-real-id", "a")


def test_max_jobs_drops_oldest_finished(monkeypatch):
    monkeypatch.setattr(config, "MCP_MAX_JOBS", 2)
    jm = JobManager(max_workers=1)
    try:
        jobs = []
        for i in range(2):
            g = Gate()
            g.release.set()
            job, _ = jm.submit("a", "repo", i, g, _safe)
            assert jm.wait(job, 2)
            jobs.append(job)
        g = Gate()
        g.release.set()
        jm.submit("a", "repo", 99, g, _safe)
        assert jm.jobs_stored() == 2
        with pytest.raises(JobNotFound):
            jm.get(jobs[0].id, "a")  # the oldest finished job made room
    finally:
        jm.shutdown()


def test_refusal_never_starts_an_orphan_review(monkeypatch):
    """Regression: a full job store used to be detected only after the review had been started."""
    monkeypatch.setattr(config, "MCP_MAX_JOBS", 1)
    jm = JobManager(max_workers=2)
    try:
        running = Gate()
        jm.submit("a", "repo", 1, running, _safe)
        started = []
        with pytest.raises(QueueFull):
            jm.submit("a", "repo", 2, lambda progress: started.append(1), _safe)
        running.release.set()
        assert started == [] and jm.queue_length() <= 1
    finally:
        jm.shutdown()


def test_refused_request_does_not_evict_a_stored_result(monkeypatch):
    """Regression: the store used to make room before the queue check, so a refusal deleted a result."""
    monkeypatch.setattr(config, "MCP_MAX_JOBS", 2)
    monkeypatch.setattr(config, "MCP_MAX_QUEUE", 1)
    jm = JobManager(max_workers=1)
    try:
        done = Gate()
        done.release.set()
        finished, _ = jm.submit("a", "repo", "done", done, _safe)
        assert jm.wait(finished, 2)
        running = Gate()
        jm.submit("a", "repo", "busy", running, _safe)
        with pytest.raises(QueueFull):
            jm.submit("a", "repo", "third", Gate(), _safe)
        assert jm.get(finished.id, "a")  # still there
        running.release.set()
    finally:
        jm.shutdown()


def test_evicted_cache_hits_free_their_results(monkeypatch):
    monkeypatch.setattr(config, "MCP_MAX_JOBS", 3)
    jm = JobManager(max_workers=1)
    try:
        for i in range(50):
            jm.add_finished("a", "repo", f"report {i}")
        assert jm.jobs_stored() == 3 and len(jm._works) == 3
    finally:
        jm.shutdown()


def test_queued_reviews_never_start_after_shutdown():
    jm = JobManager(max_workers=1)
    running, queued = Gate(), Gate()
    jm.submit("a", "repo", 1, running, _safe)
    jm.submit("a", "repo", 2, queued, _safe)
    assert running.started.wait(2)
    jm.shutdown()
    running.release.set()
    assert not queued.started.wait(0.5)
