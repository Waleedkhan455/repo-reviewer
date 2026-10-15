import json
from pathlib import Path

import pytest

from src import config, llm


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No test may reach the real LLM; trace logs go to a temp dir."""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "TRACE_FILE", tmp_path / "logs" / "trace.jsonl")
    monkeypatch.setattr(config, "LLM_RPM", 100_000)

    def _blocked():
        raise AssertionError("real LLM client used in a test")

    monkeypatch.setattr(llm, "_get_client", _blocked)


@pytest.fixture
def fake_llm(monkeypatch):
    """Route call_llm by agent name: fake_llm({"security": fn(system, user) -> str | dict})."""
    calls = []

    def install(handlers):
        def call_llm(system, user, *, agent):
            calls.append((agent, user))
            reply = handlers[agent](system, user)
            return reply if isinstance(reply, str) else json.dumps(reply)

        monkeypatch.setattr(llm, "call_llm", call_llm)
        return calls

    return install


@pytest.fixture
def make_repo(tmp_path):
    """Create files in a temp dir: make_repo({"src/a.py": "print(1)"}) -> Path."""
    def create(files: dict[str, str | bytes]) -> Path:
        root = tmp_path / "repo"
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content, encoding="utf-8")
        return root

    return create
