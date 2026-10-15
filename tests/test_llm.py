import pytest
from google.genai import errors

from src import llm


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def generate_content(self, **_kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return type("Resp", (), {"text": outcome})()


@pytest.fixture
def fake_client(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    monkeypatch.setattr(llm.config, "MODEL_FALLBACKS", [])  # tests opt in to fallbacks explicitly
    monkeypatch.setattr(llm, "_model_down_until", {})
    # The rate limiter also sleeps (sub-millisecond here). Windows' coarse clock hid that; Linux CI
    # counted it. Disable it so `sleeps` holds only retry/backoff waits.
    monkeypatch.setattr(llm, "_throttle", lambda: None)

    def install(outcomes):
        models = FakeModels(outcomes)
        monkeypatch.setattr(llm, "_get_client", lambda: type("Client", (), {"models": models})())
        return models, sleeps

    return install


def _client_error(code, details=None):
    return errors.ClientError(code, details or {"error": {"code": code, "message": "x"}})


def test_success_first_try(fake_client):
    models, _sleeps = fake_client(['{"ok": true}'])
    assert llm.call_llm("s", "u", agent="t") == '{"ok": true}'
    assert models.calls == 1


def test_retries_rate_limit_with_server_delay(fake_client):
    details = {"error": {"code": 429, "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                   "retryDelay": "33s"}]}}
    models, sleeps = fake_client([_client_error(429, details), '{"ok": 1}'])
    assert llm.call_llm("s", "u", agent="t") == '{"ok": 1}'
    assert models.calls == 2
    assert any(33 <= s < 34 for s in sleeps)  # honoured Gemini's retryDelay (+ jitter)


def test_rate_limit_waits_at_least_minimum(fake_client):
    _models, sleeps = fake_client([_client_error(429), '{"ok": 1}'])
    llm.call_llm("s", "u", agent="t")
    assert any(s >= llm.config.LLM_429_MIN_WAIT_S for s in sleeps)


def test_retries_server_errors_then_gives_up(fake_client):
    models, _ = fake_client([errors.ServerError(503, {}) for _ in range(5)])
    with pytest.raises(llm.LLMError):
        llm.call_llm("s", "u", agent="t")
    assert models.calls == llm.config.LLM_RETRIES


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_does_not_retry_client_errors(fake_client, code):
    models, _ = fake_client([_client_error(code), '{"ok": 1}'])
    with pytest.raises(llm.LLMError):
        llm.call_llm("s", "u", agent="t")
    assert models.calls == 1


def test_missing_key(monkeypatch):
    monkeypatch.setattr(llm.config, "GEMINI_API_KEY", "")
    with pytest.raises(llm.LLMError):
        llm.ensure_ready()


def test_falls_back_when_model_retired_or_overloaded(fake_client, monkeypatch):
    used = []
    models, sleeps = fake_client([_client_error(404), errors.ServerError(503, {}), '{"ok": 1}'])
    original = models.generate_content

    def spy(**kwargs):
        used.append(kwargs["model"])
        return original(**kwargs)

    models.generate_content = spy
    monkeypatch.setattr(llm.config, "MODEL_NAME", "primary")
    monkeypatch.setattr(llm.config, "MODEL_FALLBACKS", ["backup-1", "backup-2"])
    assert llm.call_llm("s", "u", agent="t") == '{"ok": 1}'
    assert used == ["primary", "backup-1", "backup-2"]
    assert sleeps == []  # switching models does not wait


def test_last_model_overloaded_then_retried(fake_client, monkeypatch):
    monkeypatch.setattr(llm.config, "MODEL_FALLBACKS", [])
    _models, sleeps = fake_client([errors.ServerError(503, {}), '{"ok": 1}'])
    assert llm.call_llm("s", "u", agent="t") == '{"ok": 1}'
    assert len(sleeps) == 1


def test_circuit_breaker_skips_paused_model(fake_client, monkeypatch):
    monkeypatch.setattr(llm.config, "MODEL_NAME", "primary")
    monkeypatch.setattr(llm.config, "MODEL_FALLBACKS", ["backup"])
    used = []
    models, _sleeps = fake_client([errors.ServerError(503, {}), '{"a": 1}', '{"b": 2}'])
    original = models.generate_content

    def spy(**kwargs):
        used.append(kwargs["model"])
        return original(**kwargs)

    models.generate_content = spy
    llm.call_llm("s", "u", agent="t")
    llm.call_llm("s", "u", agent="t")  # second call goes straight to the backup
    assert used == ["primary", "backup", "backup"]
    assert llm._available_models() == ["backup"]
    monkeypatch.setattr(llm, "_model_down_until", {"primary": 0.0, "backup": float("inf")})
    assert llm._available_models() == ["primary"]
    monkeypatch.setattr(llm, "_model_down_until", {"primary": float("inf"), "backup": float("inf")})
    assert llm._available_models() == ["backup"]  # never empty


def _spy(models, used):
    original = models.generate_content

    def spy(**kwargs):
        used.append(kwargs["model"])
        return original(**kwargs)

    models.generate_content = spy


def test_quota_error_switches_model_without_waiting(fake_client, monkeypatch):
    used = []
    models, sleeps = fake_client([_client_error(429), '{"ok": 1}'])
    _spy(models, used)
    monkeypatch.setattr(llm.config, "MODEL_NAME", "primary")
    monkeypatch.setattr(llm.config, "MODEL_FALLBACKS", ["backup"])
    assert llm.call_llm("s", "u", agent="t") == '{"ok": 1}'
    assert used == ["primary", "backup"] and sleeps == []


def test_failure_on_last_model_is_retried_not_dropped(fake_client, monkeypatch):
    """Regression: falling back on the final attempt used to end with 'no attempts made'."""
    used = []
    models, sleeps = fake_client([errors.ServerError(503, {}), _client_error(429), errors.ServerError(503, {}),
                                  '{"ok": 1}'])
    _spy(models, used)
    monkeypatch.setattr(llm.config, "MODEL_NAME", "a")
    monkeypatch.setattr(llm.config, "MODEL_FALLBACKS", ["b"])
    assert llm.call_llm("s", "u", agent="t") == '{"ok": 1}'
    assert used == ["a", "b", "b", "b"]  # both paused: keep retrying the last model with backoff
    assert len(sleeps) == 2
