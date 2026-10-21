"""Validate, clone, list and read a public GitHub repository.

Everything in a cloned repo is attacker-controlled. This module never executes repo
code, never follows symlinks, and never reads a path outside the clone directory.
"""

import contextlib
import http.client
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from src import config
from src.proc import ProcResult, ProcTimeoutError, run_bounded, safe_env


class RepoError(Exception):
    """User-facing error: bad URL, repo too large, clone failed, unsupported repo."""


_GITHUB_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?"
)

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", "site-packages", ".idea", ".vscode",
    "vendor", "bower_components", ".next", ".nuxt", "coverage", ".turbo", ".yarn",
}
SKIP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
    "uv.lock", "Cargo.lock", "composer.lock", "Gemfile.lock",
}
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg", ".pdf", ".zip", ".gz",
    ".tar", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".whl", ".egg", ".exe", ".dll", ".so",
    ".dylib", ".pyc", ".pyo", ".class", ".jar", ".bin", ".dat", ".db", ".sqlite", ".pkl",
    ".npy", ".npz", ".h5", ".pt", ".onnx", ".mp3", ".mp4", ".wav", ".avi", ".mov", ".ttf",
    ".otf", ".woff", ".woff2", ".eot", ".ipynb", ".min.js", ".map",
}


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def url(self) -> str:
        """Canonical clone URL, rebuilt from validated parts (the user's string never reaches git)."""
        return f"https://github.com/{self.owner}/{self.name}.git"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


def canonicalize(url: str) -> RepoRef:
    m = _GITHUB_URL.fullmatch(url.strip())
    if not m or m[2] in {".", ".."}:
        raise RepoError("Only public https://github.com/<owner>/<repo> URLs are accepted")
    return RepoRef(owner=m[1].lower(), name=m[2].lower())  # GitHub is case-insensitive; one cache key


def check_repo_size(ref: RepoRef) -> dict:
    """Ask the GitHub API for repo metadata and refuse private or oversized repos (fails closed)."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{ref.owner}/{ref.name}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "repo-reviewer"},
    )
    if config.GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {config.GITHUB_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=config.GITHUB_API_TIMEOUT_S) as resp:  # noqa: S310 - fixed host
            meta = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RepoError(f"Repository {ref.slug} not found or not public") from exc
        if exc.code in (403, 429):
            raise RepoError("GitHub API rate limit reached; try later or set GITHUB_TOKEN") from exc
        raise RepoError(f"GitHub API error {exc.code}") from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:  # network, truncated body, non-JSON
        raise RepoError("Could not get repository info from the GitHub API") from exc
    if not isinstance(meta, dict) or not isinstance(meta.get("size"), int):
        raise RepoError("Unexpected reply from the GitHub API")
    if meta.get("private"):
        raise RepoError("Private repositories are not supported")
    if meta["size"] > config.MAX_REPO_KB:
        raise RepoError(f"Repository is too large ({meta['size']} KB > {config.MAX_REPO_KB} KB)")
    return meta


def _git_env() -> dict[str, str]:
    return safe_env(GIT_LFS_SKIP_SMUDGE="1", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")


# Applied to every git command: no symlinks, no credential helpers or password prompts.
_GIT_HARDENING = ["-c", "core.symlinks=false", "-c", "credential.helper=", "-c", "core.askPass="]


def _git(args: list[str], cwd: Path, timeout: float) -> ProcResult:
    try:
        return run_bounded(["git", *_GIT_HARDENING, *args], cwd=cwd, timeout=timeout, env=_git_env())
    except ProcTimeoutError as exc:
        raise RepoError(f"git {args[0]} timed out after {timeout:.0f}s") from exc


def checkout_size(dest: Path, rev: str = "HEAD") -> tuple[int, int]:
    """(total blob bytes, file count) of ``rev``, read from git objects without checking anything out."""
    res = _git(["ls-tree", "-r", "-l", rev], dest, config.GIT_CMD_TIMEOUT_S)
    if res.returncode != 0:
        raise RepoError("Could not read the repository tree")
    total = count = 0
    for line in res.stdout.splitlines():
        parts = line.split(None, 4)  # mode type sha size path
        if len(parts) == 5 and parts[1] == "blob" and parts[3].isdigit():
            total += int(parts[3])
            count += 1
    return total, count


_SHA = re.compile(r"[0-9a-f]{40}")


def clone(ref: RepoRef, dest: Path, commit: str | None = None) -> None:
    """Shallow-fetch one commit (default: HEAD), check its unpacked size, then check it out.

    ``commit`` pins a full 40-char SHA (used by the evals for reproducible runs).
    """
    if commit is not None and not _SHA.fullmatch(commit):
        raise RepoError("commit must be a full 40-character SHA")
    started = time.monotonic()
    dest.mkdir(parents=True)

    def step(args: list[str], error: str) -> None:
        remaining = max(config.REPO_CLONE_TIMEOUT_S - (time.monotonic() - started), 5)
        if _git(args, dest, remaining).returncode != 0:
            raise RepoError(error)

    step(["init", "-q"], "git init failed")
    step(["fetch", "-q", "--depth", "1", "--no-tags", "--no-recurse-submodules", "--", ref.url, commit or "HEAD"],
         "git fetch failed (repo or commit missing, private, or network error)")
    total, count = checkout_size(dest, "FETCH_HEAD")
    if total > config.MAX_CHECKOUT_MB * 1_000_000 or count > config.MAX_CHECKOUT_FILES:
        raise RepoError(f"Repository is too large when unpacked ({total // 1_000_000} MB, {count} files)")
    step(["checkout", "-q", "--force", "--detach", "FETCH_HEAD"], "git checkout failed")


def remote_head(ref: RepoRef) -> str:
    """Commit SHA of the remote HEAD, without cloning (used as the cache key)."""
    res = _git(["ls-remote", "--", ref.url, "HEAD"], Path(tempfile.gettempdir()), config.GIT_CMD_TIMEOUT_S)
    sha = res.stdout.split()[0] if res.returncode == 0 and res.stdout.strip() else ""
    if not _SHA.fullmatch(sha):
        raise RepoError("Could not resolve the repository HEAD commit")
    return sha


def head_commit(root: Path) -> str:
    try:
        res = _git(["rev-parse", "HEAD"], root, config.GIT_CMD_TIMEOUT_S)
    except RepoError:
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def _force_remove(func, path, _exc) -> None:
    """rmtree error handler: git marks object files read-only on Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:
        shutil.rmtree(path, onerror=_force_remove)


@contextmanager
def cloned_repo(ref: RepoRef, commit: str | None = None) -> Iterator[Path]:
    """Clone into a fresh per-run temp dir and always delete it afterwards."""
    tmp = Path(tempfile.mkdtemp(prefix="rr_", dir=config.CLONE_TMP_DIR or None))
    try:
        dest = tmp / "repo"
        clone(ref, dest, commit)
        yield dest
    finally:
        with contextlib.suppress(OSError):  # best effort; the OS temp cleaner gets the rest
            remove_tree(tmp)


# --- Listing and reading -----------------------------------------------------


@dataclass(frozen=True)
class RepoFile:
    rel: str  # POSIX-style path relative to the repo root
    path: Path
    size: int

    @property
    def suffix(self) -> str:
        return Path(self.rel).suffix.lower()


def iter_safe_files(root: Path) -> Iterator[Path]:
    """Walk the tree without following symlinks and without leaving ``root``."""
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        dirnames[:] = sorted(n for n in dirnames if n not in SKIP_DIRS and not (d / n).is_symlink())
        for name in sorted(filenames):
            p = d / name
            if p.is_symlink() or not p.resolve().is_relative_to(root):
                continue
            yield p


def _is_binary_name(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(ext) for ext in BINARY_EXTS)


def _looks_binary(path: Path) -> bool:
    with open(path, "rb") as f:
        return b"\x00" in f.read(8192)


# Bundled third-party front-end libraries are not the project's own code: reviewing them wastes
# batches and quota (e.g. public/js/jquery-3.2.1-min.js). To stop a repo from hiding its own code
# this way, a library *name* only counts inside an asset directory, "minified" means the whole file
# looks minified (not one long line), and every skipped file is listed in the report.
_VENDORED_NAME = re.compile(
    r"^(jquery|bootstrap|popper|angular|vue|react|react-dom|lodash|underscore|moment|d3|chart|highcharts"
    r"|three|socket\.io|axios|fontawesome)"
    r"([.-](\d[\w.]*|slim|bundle|esm|umd|min|full|core|production|development))*\.(js|css)$",
    re.IGNORECASE,
)
_MIN_NAME = re.compile(r"[.-]min\.(js|css)$", re.IGNORECASE)
ASSET_DIRS = {"public", "static", "assets", "vendor", "vendors", "third_party", "thirdparty", "external",
              "bower_components", "wwwroot"}
MINIFIED_MAX_LINES = 5  # within the first 16 KB
MINIFIED_MIN_AVG_LINE = 500


def _looks_minified(path: Path) -> bool:
    """The whole sample looks minified: very few, very long lines (one long comment line is not enough)."""
    with open(path, "rb") as f:
        lines = [line for line in f.read(16_384).splitlines() if line.strip()]
    return bool(lines) and len(lines) <= MINIFIED_MAX_LINES and \
        sum(map(len, lines)) / len(lines) >= MINIFIED_MIN_AVG_LINE


def is_vendored_asset(rel: str, path: Path) -> bool:
    if path.suffix.lower() not in {".js", ".css", ".mjs", ".cjs"}:
        return False
    # Two independent signals are required, so neither a name nor a file shape alone can hide code.
    in_asset_dir = any(part.lower() in ASSET_DIRS for part in rel.split("/")[:-1])
    min_name = bool(_MIN_NAME.search(path.name))
    if in_asset_dir:
        return bool(_VENDORED_NAME.search(path.name)) or min_name or _looks_minified(path)
    return min_name and _looks_minified(path)


# Paths go into prompts and Markdown; anything outside this set could break out of the nonce tags.
_SAFE_PATH = re.compile(r"[\w.\- /+@=,()\[\]{}~#!$%&']+")


def list_files(root: Path, vendored: list[str] | None = None) -> list[RepoFile]:
    """Reviewable files. Skipped third-party assets are appended to ``vendored`` (for the report)."""
    root = root.resolve()
    files = []
    for p in iter_safe_files(root):
        rel = p.relative_to(root).as_posix()
        if not _SAFE_PATH.fullmatch(rel) or p.name in SKIP_FILES or _is_binary_name(p.name):
            continue
        size = p.stat().st_size
        if size == 0 or size > config.MAX_FILE_BYTES or _looks_binary(p):
            continue
        if is_vendored_asset(rel, p):
            if vendored is not None:
                vendored.append(rel)
            continue
        files.append(RepoFile(rel=rel, path=p, size=size))
    return files


def read_text(path: Path) -> str:
    text = path.read_bytes().decode("utf-8", errors="replace")
    return text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")


@dataclass
class Snapshot:
    """The filtered files of one cloned repo, with lazily cached contents."""

    root: Path
    files: list[RepoFile]
    vendored: list[str] = field(default_factory=list)  # third-party assets skipped by list_files
    focus: dict[str, str] = field(default_factory=dict)  # PR review: rel -> changed line ranges ("3-7,12")
    by_rel: dict[str, RepoFile] = field(init=False, repr=False)
    _text: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.by_rel = {f.rel: f for f in self.files}

    def has(self, rel: str) -> bool:
        return rel in self.by_rel

    def text(self, rel: str) -> str:
        """Read a file that is in the snapshot. Unknown paths raise KeyError (never touch disk)."""
        if rel not in self._text:
            self._text[rel] = read_text(self.by_rel[rel].path)
        return self._text[rel]

    def lines(self, rel: str) -> list[str]:
        return self.text(rel).split("\n")

    def code_files(self) -> list[RepoFile]:
        """Source files in a supported language (Python, JavaScript, TypeScript, Go)."""
        return [f for f in self.files if language_of(f.rel)]

    def python_files(self) -> list[RepoFile]:
        return [f for f in self.files if f.suffix == ".py"]

    def languages(self) -> set[str]:
        return {lang for f in self.files if (lang := language_of(f.rel))}

    def total_loc(self) -> int:
        return sum(len(self.lines(f.rel)) for f in self.code_files())


# Supported languages: each gets LLM review plus a static analyzer (bandit/ruff for Python,
# Semgrep rule packs for the others).
LANGUAGE_EXTS = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".go": "go",
}
CODE_EXTS = {
    *LANGUAGE_EXTS, ".java", ".kt", ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".rs", ".swift",
    ".scala", ".sh",
}


def language_of(rel: str) -> str | None:
    """Supported language of a file, or None (type declarations like x.d.ts are not code to review)."""
    name = rel.rsplit("/", 1)[-1].lower()
    if name.endswith((".d.ts", ".d.mts", ".d.cts")):
        return None
    return LANGUAGE_EXTS.get(Path(name).suffix)


def language_warnings(snapshot: Snapshot) -> list[str]:
    """Reject repos with no supported code; warn when most of the code is in unsupported languages."""
    supported = len(snapshot.code_files())
    if supported == 0:
        raise RepoError("No supported source files found: Python, JavaScript, TypeScript or Go repositories only")
    other = sum(1 for f in snapshot.files if f.suffix in CODE_EXTS and f.suffix not in LANGUAGE_EXTS)
    share = supported / (supported + other)
    if share < 0.5:
        return [(f"Only {share:.0%} of code files are Python, JavaScript, TypeScript or Go; "
                "other languages get regex checks only.")]
    return []
