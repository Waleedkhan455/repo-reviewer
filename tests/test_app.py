"""Streamlit UI smoke tests with AppTest (no browser, no network, no LLM)."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src import orchestrator, service
from src.orchestrator import ReviewResult
from src.quota import Quota
from src.schemas import Finding, RejectedOut
from tests.factories import review_data as _data

APP = Path(__file__).resolve().parent.parent / "app.py"


def _app():
    at = AppTest.from_file(str(APP), default_timeout=30)
    return at.run()


def test_initial_render():
    at = _app()
    assert not at.exception
    assert "Repo Reviewer" in at.title[0].value
    assert len(at.text_input) == 1


def test_bad_url_shows_error():
    at = _app()
    at.text_input[0].input("https://evil.example.com/a/b")
    at.button[0].click().run()
    assert not at.exception
    assert any("github.com" in e.value for e in at.error)


@pytest.fixture
def cached_result(monkeypatch):
    f = Finding(id="SEC-1", file="db.py", line=2, severity="high", issue="SQL injection",
                evidence="query = 'SELECT' + name", fix="parameterize", source="security")
    data = _data("demo/app", [f], confirmed=["SEC-1"]).model_copy(update={
        "rejected": [RejectedOut(id="QUAL-1", stage="precheck", reason="file not found")]})
    result = ReviewResult(report_md="# Code Review: demo/app\n\n## Score: 90 / 100\n", data=data, cached=True)
    monkeypatch.setattr(service, "find_cached_review",
                        lambda url, opts=None: orchestrator.CacheLookup("c" * 40, "k", result))
    return result


def test_cached_result_renders_without_quota(cached_result):
    at = _app()
    at.text_input[0].input("https://github.com/demo/app")
    at.button[0].click().run()
    assert not at.exception
    assert [m.value for m in at.metric][:3] == ["100 / 100", "1", "1"]
    assert any("Served from cache" in c.value for c in at.caption)
    assert any("Code Review: demo/app" in m.value for m in at.markdown)



def _submit(at, url="https://github.com/demo/app"):
    at.text_input[0].input(url)
    at.button[0].click().run()
    return at


@pytest.fixture
def cache_miss(monkeypatch):
    monkeypatch.setattr(service, "find_cached_review",
                        lambda url, opts=None: orchestrator.CacheLookup("c" * 40, "k", None))


def test_ai_failure_shows_generic_message(cache_miss, monkeypatch):
    from src.llm import LLMError

    def fail(*a, **k):
        raise LLMError("quota exhausted; key AIzaSECRET at C:/internal/path")

    monkeypatch.setattr(service, "review_repo", fail)
    at = _submit(_app())
    assert not at.exception
    errors = " ".join(e.value for e in at.error)
    assert "unavailable or rate-limited" in errors
    assert "AIza" not in errors and "internal" not in errors


def test_unexpected_error_is_generic(cache_miss, monkeypatch):
    monkeypatch.setattr(service, "review_repo", lambda *a, **k: 1 / 0)
    at = _submit(_app())
    assert not at.exception
    assert any("Something went wrong" in e.value for e in at.error)


def test_repo_error_refunds_daily_quota(cache_miss, monkeypatch):
    from src.repo import RepoError

    def too_big(*a, **k):
        raise RepoError("Repository is too large")

    refunds = []
    original = Quota.release

    def spy(self, *, refund=False):
        refunds.append(refund)
        original(self, refund=refund)

    monkeypatch.setattr(service, "review_repo", too_big)
    monkeypatch.setattr(Quota, "release", spy)
    at = _submit(_app())
    assert any("too large" in e.value for e in at.error)
    assert refunds == [True]
