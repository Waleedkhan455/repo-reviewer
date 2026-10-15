import sys
import time

import pytest

from src.proc import ProcTimeoutError, run_bounded, safe_env

# A child that starts a grandchild holding the output pipes, then waits for it.
SPAWNS_GRANDCHILD = (
    "import subprocess, sys; "
    "subprocess.run([sys.executable, '-c', 'import time; time.sleep(20)'])"
)


def test_timeout_kills_whole_tree(tmp_path):
    start = time.monotonic()
    with pytest.raises(ProcTimeoutError):
        run_bounded([sys.executable, "-c", SPAWNS_GRANDCHILD], cwd=tmp_path, timeout=1)
    assert time.monotonic() - start < 10  # plain subprocess.run would block ~20s on Windows


def test_output_and_returncode(tmp_path):
    res = run_bounded([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"], cwd=tmp_path, timeout=30)
    assert res.returncode == 3
    assert res.stdout.strip() == "hi"


def test_safe_env_strips_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-1")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-2")
    monkeypatch.setenv("RR_MODEL", "x")
    monkeypatch.setenv("HARMLESS_VAR", "ok")
    env = safe_env(EXTRA="1")
    assert "GEMINI_API_KEY" not in env and "GITHUB_TOKEN" not in env and "RR_MODEL" not in env
    assert env["HARMLESS_VAR"] == "ok" and env["EXTRA"] == "1"
    res = run_bounded([sys.executable, "-c", "import os; print(os.environ.get('GEMINI_API_KEY', 'absent'))"],
                      cwd=tmp_path, timeout=30)
    assert res.stdout.strip() == "absent"
