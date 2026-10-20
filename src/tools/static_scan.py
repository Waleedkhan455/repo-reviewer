"""Run bandit and ruff (Python) and Semgrep (JavaScript, TypeScript, Go) and normalize their output.

All tools are hardened against the scanned repo:
- Python runs in isolated mode (-I) from a neutral directory, so modules planted in the repo
  (e.g. ``bandit/__main__.py``) can never shadow the real tool.
- Our own config only (the repo's config files are ignored), secrets stripped from the environment,
  hard timeouts that kill the whole process tree. Security scanners (bandit, Semgrep) also ignore
  suppression comments; ruff, which only feeds Quality/Bug Hunter hints, respects inline `# noqa`.
No tool executes the code it scans.
"""

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from ruff.__main__ import find_ruff_bin

from src import config
from src.logger import log_event
from src.proc import ProcTimeoutError, run_bounded


class ScanError(Exception):
    """A scanner crashed, timed out or produced unreadable output."""


@dataclass(frozen=True)
class ToolFinding:
    tool: str
    rule: str
    file: str  # POSIX path relative to the repo root
    line: int
    severity: str  # high / medium / low
    message: str

    def as_line(self) -> str:
        return f"- [{self.tool} {self.rule} {self.severity}] {self.file}:{self.line} {self.message}"


def _rel(root: Path, cwd: Path, filename: str) -> str:
    p = Path(filename)
    if not p.is_absolute():
        p = cwd / p
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return Path(filename).as_posix()


def _run_json(tool: str, cmd: list[str], cwd: Path) -> object:
    try:
        res = run_bounded(cmd, cwd=cwd, timeout=config.SCAN_TIMEOUT_S)
    except (ProcTimeoutError, OSError) as exc:
        log_event("scan_error", tool=tool, error=str(exc))
        raise ScanError(f"{tool}: {exc}") from exc
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        log_event("scan_error", tool=tool, error=f"exit {res.returncode}, unreadable output",
                  stderr=res.stderr[-300:])
        raise ScanError(f"{tool} failed (exit {res.returncode})") from exc


def run_bandit(root: Path, targets: list[str] | None = None) -> list[ToolFinding]:
    """Scan the whole repo, or only ``targets`` (repo-relative paths, e.g. the files a PR changed)."""
    cwd = root.parent  # neutral directory: never run from inside the attacker's repo
    paths = [str(root / t) for t in targets] if targets is not None else [str(root)]
    if not paths:
        return []
    cmd = [
        sys.executable, "-I", "-m", "bandit", "-r", *paths, "-f", "json", "-q",
        "--ignore-nosec", "--ini", str(config.BANDIT_INI), "--skip", config.BANDIT_SKIP,
    ]
    data = _run_json("bandit", cmd, cwd)
    if not isinstance(data, dict):
        raise ScanError("bandit returned unexpected JSON")
    return [
        ToolFinding(
            tool="bandit",
            rule=r.get("test_id", "?"),
            file=_rel(root, cwd, r.get("filename", "")),
            line=int(r.get("line_number") or 0),
            severity=str(r.get("issue_severity", "low")).lower(),
            message=r.get("issue_text", ""),
        )
        for r in data.get("results", [])
    ]


def _ruff_severity(code: str) -> str:
    if code.startswith(("F", "E9", "PLE")):
        return "medium"
    return "low"


def run_ruff(root: Path, targets: list[str] | None = None) -> list[ToolFinding]:
    """Scan the whole repo, or only ``targets`` (repo-relative paths, e.g. the files a PR changed)."""
    cwd = root.parent
    paths = [str(root / t) for t in targets] if targets is not None else [str(root)]
    if not paths:
        return []
    cmd = [
        find_ruff_bin(), "check", *paths, "--isolated", "--no-cache",
        "--output-format", "json", "--select", config.RUFF_RULES, "--exit-zero",
    ]
    data = _run_json("ruff", cmd, cwd)
    if not isinstance(data, list):
        raise ScanError("ruff returned unexpected JSON")
    findings = []
    for r in data:
        code = r.get("code") or "syntax-error"
        findings.append(ToolFinding(
            tool="ruff",
            rule=code,
            file=_rel(root, cwd, r.get("filename", "")),
            line=int((r.get("location") or {}).get("row") or 0),
            severity=_ruff_severity(code),
            message=r.get("message", ""),
        ))
    return findings


# --- Semgrep (JavaScript, TypeScript, Go) -------------------------------------------


def _semgrep_bin() -> str:
    """The semgrep executable installed next to this interpreter (never one from the scanned repo)."""
    exe = Path(sys.executable).parent / ("semgrep.exe" if sys.platform == "win32" else "semgrep")
    if exe.exists():
        return str(exe)
    found = shutil.which("semgrep")
    if not found:
        raise ScanError("semgrep is not installed")
    return found


_SEMGREP_SEVERITY = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}


def semgrep_configs(languages: set[str]) -> list[str]:
    """Free registry rule packs for the languages present, without duplicates."""
    return list(dict.fromkeys(pack for lang in sorted(languages) for pack in config.SEMGREP_PACKS.get(lang, [])))


def _target_batches(paths: list[str]) -> list[list[str]]:
    """Split explicit target paths so no command line exceeds SEMGREP_MAX_CMD_CHARS (Windows limit ~32k)."""
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for p in paths:
        if current and size + len(p) + 1 > config.SEMGREP_MAX_CMD_CHARS:
            batches.append(current)
            current, size = [], 0
        current.append(p)
        size += len(p) + 1
    if current:
        batches.append(current)
    return batches


def _rule_label(result: dict) -> str:
    """Static text from the rule's registry metadata (its CWE). Never the match message: Semgrep
    interpolates matched repo code into messages, which would let the repo write into the prompt."""
    cwe = ((result.get("extra") or {}).get("metadata") or {}).get("cwe")
    first = cwe[0] if isinstance(cwe, list) and cwe else cwe
    return str(first or "")[: config.SEMGREP_MESSAGE_CHARS]


def run_semgrep(root: Path, targets: list[str], languages: set[str]) -> list[ToolFinding]:
    """Scan only ``targets`` (repo-relative paths of first-party code) with Semgrep's free rule packs.

    The repo cannot silence it (``nosemgrep`` comments and ``.semgrepignore`` files are ignored) and
    cannot add rules (explicit ``--config`` disables auto-discovery of a planted ``.semgrep.yml``).
    Memory is bounded by scanning only first-party files, a per-file size cap, one job and Semgrep's --max-memory.
    """
    configs = semgrep_configs(languages)
    if not configs or not targets:
        return []
    base = [
        _semgrep_bin(), "scan", *(arg for c in configs for arg in ("--config", c)),
        "--json", "--quiet", "--metrics", "off", "--disable-version-check",
        "--disable-nosem", "--x-ignore-semgrepignore-files",
        "--max-target-bytes", str(config.SEMGREP_MAX_TARGET_BYTES), "--timeout", str(config.SEMGREP_RULE_TIMEOUT_S),
        "--max-memory", str(config.SEMGREP_MAX_MEMORY_MB), "--jobs", str(config.SEMGREP_JOBS),
    ]
    findings = []
    for batch in _target_batches([str(root / rel) for rel in targets]):
        data = _run_json("semgrep", [*base, *batch], root.parent)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ScanError("semgrep returned unexpected JSON")
        for r in data["results"]:
            findings.append(ToolFinding(
                tool="semgrep",
                rule=str(r.get("check_id", "?")).rsplit(".", 1)[-1],
                file=_rel(root, root.parent, r.get("path", "")),
                line=int((r.get("start") or {}).get("line") or 0),
                severity=_SEMGREP_SEVERITY.get(str((r.get("extra") or {}).get("severity", "")).upper(), "low"),
                message=_rule_label(r),
            ))
    return findings
