from datetime import date, timedelta

from src.quota import Quota


def _quota(**kw):
    defaults = {"daily_cap": 10, "max_concurrent": 5, "max_lookups": 2, "cooldown_s": 0}
    return Quota(**{**defaults, **kw})


def test_daily_cap_and_reset():
    day = [date(2026, 1, 1)]
    q = _quota(daily_cap=2, today=lambda: day[0])
    assert q.acquire("a") is None and q.acquire("b") is None
    assert "daily" in q.acquire("c")
    day[0] += timedelta(days=1)
    assert q.acquire("c") is None


def test_refund_on_early_failure():
    q = _quota(daily_cap=1)
    assert q.acquire("a") is None
    q.release(refund=True)  # e.g. repo too large: no LLM quota was used
    assert q.runs_today == 0
    assert q.acquire("b") is None


def test_concurrency_cap():
    q = _quota(max_concurrent=1)
    assert q.acquire("a") is None
    assert "busy" in q.acquire("b")
    q.release()
    assert q.acquire("b") is None
    assert q.runs_today == 2  # the refused attempt did not count


def test_cooldown_is_per_client_and_survives_new_sessions():
    now = [1000.0]
    q = _quota(cooldown_s=120, clock=lambda: now[0])
    assert q.acquire("1.2.3.4") is None
    q.release()
    now[0] += 30
    assert "wait 90s" in q.acquire("1.2.3.4")  # same client, new browser session: still waiting
    assert q.cooldown_left("1.2.3.4") == 90
    assert q.acquire("5.6.7.8") is None  # other clients unaffected
    now[0] += 100
    assert q.acquire("1.2.3.4") is None


def test_lookup_slots():
    q = _quota(max_lookups=2)
    assert q.try_lookup() and q.try_lookup()
    assert not q.try_lookup()
    q.end_lookup()
    assert q.try_lookup()
