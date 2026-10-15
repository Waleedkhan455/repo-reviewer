"""Which lines did a pull request add or change? Parsed from `git diff --unified=0`.

The diff text is attacker-influenced (it contains the PR's own lines), so parsing is stateful:
file headers are only recognised before a file's first hunk, and each hunk body is consumed by its
exact line count, so an added line such as "++ /dev/null" can never be mistaken for a header.
"""

import re
from pathlib import Path

from src import config
from src.proc import ProcTimeoutError, run_bounded, safe_env

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SHA = re.compile(r"[0-9a-f]{7,40}")


class DiffError(Exception):
    """The diff could not be computed (missing history, bad commit, git failure, too large)."""


def parse_unified_diff(text: str) -> dict[str, set[int]]:
    """Map each changed file (new path) to the line numbers it added or modified.

    Deleted files and pure deletions produce no lines. Quoted paths (unusual characters) are skipped;
    such files are also rejected by the file listing.
    """
    changed: dict[str, set[int]] = {}
    current: str | None = None
    in_header = False
    pending = 0  # hunk body lines still to consume
    for line in text.splitlines():
        if pending:
            if not line.startswith("\\"):  # "\ No newline at end of file" is not a counted line
                pending -= 1
            continue
        if line.startswith("diff --git "):
            current, in_header = None, True
            continue
        if in_header and line.startswith("+++ "):
            target = line[4:].strip()
            current = target[2:] if target.startswith("b/") else None  # "/dev/null" or quoted -> skip
            continue
        m = _HUNK.match(line)
        if m:
            in_header = False
            old_count = int(m[1]) if m[1] is not None else 1
            start, new_count = int(m[2]), int(m[3]) if m[3] is not None else 1
            pending = old_count + new_count
            if current is not None and new_count:
                changed.setdefault(current, set()).update(range(start, start + new_count))
    return changed


def _git(root: Path, args: list[str]) -> str:
    cmd = ["git", "-c", "core.quotePath=false", *args]
    try:
        res = run_bounded(cmd, cwd=root, timeout=config.GIT_CMD_TIMEOUT_S, env=safe_env(GIT_TERMINAL_PROMPT="0"))
    except ProcTimeoutError as exc:
        raise DiffError("git diff timed out") from exc
    if res.returncode != 0:
        raise DiffError("git diff failed: is the full history checked out (actions/checkout fetch-depth: 0)?")
    return res.stdout


def changed_lines(root: Path, base: str, head: str) -> dict[str, set[int]]:
    """Lines changed between the merge base of ``base`` and ``head`` (what a PR shows) and ``head``.

    ``--text`` stops a PR's own .gitattributes (``*.py -diff`` / ``binary``) from hiding its changes,
    and the size is checked with ``--numstat`` before the full diff is read into memory.
    """
    for sha in (base, head):
        if not _SHA.fullmatch(sha):
            raise DiffError(f"not a commit SHA: {sha!r}")
    common = ["--no-color", "--no-ext-diff", "--no-textconv", "--text", "-M", f"{base}...{head}", "--"]
    added = 0
    for row in _git(root, ["diff", "--numstat", *common]).splitlines():
        first = row.split("\t", 1)[0]
        added += int(first) if first.isdigit() else 0
    if added > config.PR_MAX_CHANGED_LINES:
        raise DiffError(f"pull request too large to review ({added} added lines > {config.PR_MAX_CHANGED_LINES})")
    return parse_unified_diff(_git(root, ["diff", "--unified=0", *common]))


def format_ranges(lines: set[int]) -> str:
    """{3, 4, 5, 9} -> "3-5,9" (compact, for the agents' changed_lines attribute)."""
    out: list[str] = []
    for n in sorted(lines):
        if out and out[-1].split("-")[-1] == str(n - 1):
            out[-1] = f"{out[-1].split('-')[0]}-{n}"
        else:
            out.append(str(n))
    return ",".join(out)
