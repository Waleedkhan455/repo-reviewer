"""Bounded subprocess execution for every command that touches attacker-controlled content.

- The child gets an environment with secrets removed.
- On timeout the whole process tree is killed. Plain ``subprocess.run(timeout=...)`` does not
  enforce the timeout on Windows when grandchildren (git-remote-https, ruff.exe) hold the pipes.
"""

import contextlib
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_SECRET_NAME = re.compile(r"(?i)key|token|secret|passw|credential|auth|^RR_")


class ProcTimeoutError(Exception):
    """The command exceeded its timeout and its process tree was killed."""


@dataclass(frozen=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


def safe_env(**extra: str) -> dict[str, str]:
    """The current environment without anything that looks like a secret, plus ``extra``."""
    env = {k: v for k, v in os.environ.items() if not _SECRET_NAME.search(k)}
    env.update(extra)
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, check=False, timeout=30)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def run_bounded(cmd: list[str], *, cwd: Path, timeout: float, env: dict[str, str] | None = None) -> ProcResult:
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
             else {"start_new_session": True})
    proc = subprocess.Popen(cmd, cwd=cwd, env=env if env is not None else safe_env(),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **group)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        raise ProcTimeoutError(f"{Path(cmd[0]).name} timed out after {timeout}s") from None
    return ProcResult(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
