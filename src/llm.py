"""The single wrapper around the LLM API. No other module imports the SDK."""

import json
import random
import re
import threading
import time

import httpx
from google import genai
from google.genai import errors, types

from src import config
from src.logger import current_run, log_event


class LLMError(Exception):
    """The LLM call failed after all retries, or failed with a non-retryable error."""


_client: genai.Client | None = None
_client_lock = threading.Lock()
_rate_lock = threading.Lock()
_last_call = 0.0


def ensure_ready() -> None:
    """Fail fast before cloning if no API key is configured."""
    if not config.GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY is not set (copy .env.example to .env and add your key)")


def _get_client() -> genai.Client:
    global _client
    with _client_lock:
        if _client is None:
            ensure_ready()
            _client = genai.Client(
                api_key=config.GEMINI_API_KEY,
                http_options=types.HttpOptions(timeout=config.LLM_TIMEOUT_S * 1000),
            )
        return _client


def _throttle() -> None:
    """Keep at least 60/LLM_RPM seconds between calls across all threads (free-tier limit)."""
    global _last_call
    with _rate_lock:
        wait = _last_call + 60 / config.LLM_RPM - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


_RETRY_DELAY = re.compile(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"')


def _retry_wait(exc: Exception, attempt: int) -> float:
    """Seconds to wait before the next attempt. Rate limits honour Gemini's retryDelay (min 20s)."""
    if isinstance(exc, errors.ClientError) and exc.code == 429:
        m = _RETRY_DELAY.search(json.dumps(exc.details, default=str))
        wait = max(float(m[1]) if m else 0.0, config.LLM_429_MIN_WAIT_S)
    else:
        wait = 2.0**attempt
    return min(wait, config.LLM_MAX_WAIT_S) + random.uniform(0, 1)  # noqa: S311 - jitter, not crypto


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, errors.ClientError):
        return exc.code == 429  # rate limit; other 4xx (bad request, auth) will not fix themselves
    return isinstance(exc, (errors.ServerError, httpx.HTTPError))


def _should_fall_back(exc: Exception) -> bool:
    """Retired (404), out of quota (429, quotas are per model) or overloaded (503): try another model."""
    return isinstance(exc, errors.APIError) and exc.code in (404, 429, 503)


# Circuit breaker: a model that just failed that way is skipped by every thread for a while,
# so only one call pays for discovering it, not every call in the run.
_model_down_until: dict[str, float] = {}
_breaker_lock = threading.Lock()


def _pause_seconds(exc: errors.APIError) -> float:
    if exc.code == 404:
        return config.MODEL_RETIRED_PAUSE_S
    if exc.code == 429:
        m = _RETRY_DELAY.search(json.dumps(exc.details, default=str))
        return max(float(m[1]) if m else 0.0, config.MODEL_QUOTA_PAUSE_S)
    return config.MODEL_OVERLOADED_PAUSE_S


def _mark_down(model: str, exc: errors.APIError) -> None:
    pause = _pause_seconds(exc)
    with _breaker_lock:
        _model_down_until[model] = time.monotonic() + pause
    log_event("model_paused", model=model, code=exc.code, seconds=round(pause))


def _available_models() -> list[str]:
    """Configured models in order, minus paused ones (the last model is always kept as a final try)."""
    models = [config.MODEL_NAME, *config.MODEL_FALLBACKS]
    now = time.monotonic()
    with _breaker_lock:
        up = [m for m in models if _model_down_until.get(m, 0.0) <= now]
    return up or models[-1:]


def call_llm(system: str, user: str, *, agent: str) -> str:
    """Send one prompt and return the raw text reply (expected to be JSON).

    Uses the first model that is not paused. A retired, out-of-quota or overloaded model is paused
    for every thread and the call moves to the next model at once; switching models never uses up
    the retry budget. When no other model is left, transient errors are retried with backoff.
    """
    client = _get_client()
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=config.LLM_TEMPERATURE,
        response_mime_type="application/json",
    )
    run = current_run.get()
    tried: set[str] = set()
    retries = attempt = 0
    while True:
        attempt += 1
        untried = [m for m in _available_models() if m not in tried]
        model = untried[0] if untried else _available_models()[-1]
        _throttle()
        start = time.monotonic()
        try:
            resp = client.models.generate_content(model=model, contents=user, config=cfg)
            text = resp.text or ""
        except Exception as exc:  # classified below; anything unexpected is re-raised
            latency = time.monotonic() - start
            if run:
                run.record_call(latency, ok=False, agent=agent)
            log_event("llm_error", agent=agent, model=model, attempt=attempt, latency_s=round(latency, 2),
                      error=type(exc).__name__, code=getattr(exc, "code", None),
                      message=str(getattr(exc, "message", "") or "")[: config.ERROR_LOG_CHARS])
            if not isinstance(exc, (errors.APIError, httpx.HTTPError)):
                raise
            if _should_fall_back(exc):
                _mark_down(model, exc)
                tried.add(model)
                if any(m not in tried for m in _available_models()):
                    continue  # another model can take it right now
            retries += 1
            if not _is_retryable(exc) or retries >= config.LLM_RETRIES:
                raise LLMError(f"{agent}: LLM call failed ({type(exc).__name__})") from exc
            time.sleep(_retry_wait(exc, retries))
            continue
        latency = time.monotonic() - start
        if run:
            run.record_call(latency, ok=True, agent=agent, model=model)
        log_event("llm_call", agent=agent, model=model, attempt=attempt, prompt_chars=len(system) + len(user),
                  reply_chars=len(text), latency_s=round(latency, 2))
        return text
