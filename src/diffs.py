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


# A repository's own .git/config can name commands git runs during a diff (core.fsmonitor, diff drivers,
# textconv). Local mode may review a downloaded repo, so those are always switched off.
GIT_SAFE = ["git", "-c", "core.quotePath=false", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=",
            "-c", "protocol.allow=never"]


def _git_env() -> dict[str, str]:
    return safe_env(GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")


# Repository-level settings that make git run a program. Comparing the working tree with a commit passes
# files through clean filters, which `-c` cannot switch off without knowing their names, so a repository
# that defines any of these is refused (the user's global settings, such as git-lfs, are their own).
_RUNS_COMMAND = re.compile(
    r"filter\..*|diff\..*\.(command|textconv)|diff\.external|core\.(fsmonitor|worktree|sshcommand|pager|askpass"
    r"|gitproxy|alternaterefscommand|editor)|remote\..*\.(uploadpack|receivepack)|protocol\..*"
    r"|extensions\.partialclone|uploadpack\..*|credential\..*", re.IGNORECASE)


def check_repo_config(root: Path) -> None:
    """Refuse a checkout whose own git config (local, worktree or included files) can run commands."""
    try:
        res = run_bounded([*GIT_SAFE, "config", "--list", "--show-scope", "--name-only", "--includes"],
                          cwd=root, timeout=config.GIT_CMD_TIMEOUT_S, env=_git_env())
    except ProcTimeoutError as exc:
        raise DiffError("git config timed out") from exc
    if res.returncode != 0:
        raise DiffError("could not read the repository's git config")
    for row in res.stdout.splitlines():
        scope, _, key = row.partition("\t")
        if scope in ("local", "worktree") and _RUNS_COMMAND.fullmatch(key.strip()):
            raise DiffError(f"refusing to review: the repository's git config sets {key.strip()[:80]!r}, which "
                            "can make git run a program; remove it from .git/config first")


def _git(root: Path, args: list[str]) -> str:
    cmd = [*GIT_SAFE, *args]
    try:
        res = run_bounded(cmd, cwd=root, timeout=config.GIT_CMD_TIMEOUT_S, env=_git_env())
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


# --- Working tree (uncommitted changes, used by the local MCP server) ------------------------------

_SAFE_REF = re.compile(rf"[A-Za-z0-9][A-Za-z0-9._/~^@{{}}-]{{0,{config.MCP_REF_MAX_CHARS - 1}}}")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's well-known empty tree: "no commits yet"


def resolve_ref(root: Path, base: str) -> str:
    """Turn a ref like HEAD, main or HEAD~2 into a commit SHA (refs are validated, never passed as options).

    In a repository with no commits yet, ``HEAD`` resolves to git's empty tree (everything is new).
    """
    if not _SAFE_REF.fullmatch(base) or ".." in base:
        raise DiffError(f"not a valid git ref: {base!r}")
    try:
        res = run_bounded([*GIT_SAFE, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{base}^{{commit}}"],
                          cwd=root, timeout=config.GIT_CMD_TIMEOUT_S, env=_git_env())
    except ProcTimeoutError as exc:
        raise DiffError("git rev-parse timed out") from exc
    sha = res.stdout.strip()
    if res.returncode == 0 and _SHA.fullmatch(sha):
        return sha
    if base == "HEAD":  # a repository without commits yet: everything in it is new
        return EMPTY_TREE
    raise DiffError(f"unknown git ref: {base!r}")


def _is_binary(path: Path) -> bool:
    with open(path, "rb") as f:
        return b"\x00" in f.read(8192)


def _inside_file(root: Path, rel: str) -> Path | None:
    """``root/rel`` if it is a regular file that really lies inside ``root`` (no symlink or junction escape)."""
    path = root / rel
    try:
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root.resolve()) or not path.is_file():
            return None
    except (OSError, RuntimeError):
        return None
    return path


def _too_large(added: int) -> DiffError:
    return DiffError(f"change too large to review ({added} changed lines > {config.PR_MAX_CHANGED_LINES})")


def _untracked_lines(root: Path, budget: int) -> dict[str, set[int]]:
    """Every line of each new, untracked text file (honouring .gitignore). Symlinks and binaries are skipped;
    a file over the size limit is kept as line 1 so the review lists it as not reviewed. Stops with
    ``DiffError`` as soon as more than ``budget`` lines are found."""
    changed: dict[str, set[int]] = {}
    total = 0
    listing = _git(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    for rel in filter(None, listing.split("\0")):
        path = _inside_file(root, rel)
        if path is None:
            continue
        key = rel.replace("\\", "/")
        if path.stat().st_size > config.MAX_FILE_BYTES:
            changed[key] = {1}
            continue
        if _is_binary(path):
            continue
        with open(path, "rb") as f:
            count = len(f.read().splitlines())
        total += count
        if total > budget:
            raise _too_large(config.PR_MAX_CHANGED_LINES - budget + total)
        if count:
            changed[key] = set(range(1, count + 1))
    return changed


def worktree_changed_lines(root: Path, base: str = "HEAD", include_untracked: bool = True) -> dict[str, set[int]]:
    """Lines changed in the working tree (staged and unstaged) relative to ``base``, plus untracked files.

    Deleted files contribute nothing and binary files are skipped. Like ``changed_lines``, the diff is
    forced to text and its size is checked before the full diff is read. A repository whose own git config
    could make git run a program is refused first (see ``check_repo_config``).
    """
    check_repo_config(root)
    sha = resolve_ref(root, base)
    common = ["--no-color", "--no-ext-diff", "--no-textconv", "--text", "--ignore-submodules=all", "-M", sha, "--"]
    added = 0
    for row in _git(root, ["diff", "--numstat", *common]).splitlines():
        first = row.split("\t", 1)[0]
        added += int(first) if first.isdigit() else 0
    if added > config.PR_MAX_CHANGED_LINES:
        raise _too_large(added)
    untracked = _untracked_lines(root, config.PR_MAX_CHANGED_LINES - added) if include_untracked else {}
    changed = parse_unified_diff(_git(root, ["diff", "--unified=0", *common]))
    changed = {rel: lines for rel, lines in changed.items() if not _is_binary_on_disk(root, rel)}
    for rel, lines in untracked.items():
        changed.setdefault(rel, set()).update(lines)
    return changed


def _is_binary_on_disk(root: Path, rel: str) -> bool:
    path = _inside_file(root, rel)
    return path is not None and _is_binary(path)
