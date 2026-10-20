"""Central configuration: every limit, model name and path lives here.

Values can be overridden with environment variables (or a local .env file).
"""

import getpass
import os
import stat
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
# Read .env without copying it into os.environ, so secrets never reach child processes.
_DOTENV = dotenv_values(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or _DOTENV.get(name) or default


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


# --- LLM -------------------------------------------------------------------
GEMINI_API_KEY = _env("GEMINI_API_KEY")
MODEL_NAME = _env("RR_MODEL", "gemini-3.8-flash")
# Used in order when a model is retired (404) or overloaded (503). Comma-separated in RR_MODEL_FALLBACKS.
MODEL_OVERLOADED_PAUSE_S = 300  # skip a model that returned 503 for this long
MODEL_RETIRED_PAUSE_S = 86_400  # skip a model that returned 404 (retired) for a day
MODEL_QUOTA_PAUSE_S = 60  # skip a model that returned 429 (quota) at least this long, or its retryDelay
MODEL_FALLBACKS = [m.strip() for m in _env("RR_MODEL_FALLBACKS", "gemini-3.5-flash,gemini-3.5-flash-lite").split(",")
                   if m.strip()]
LLM_TEMPERATURE = 0.1
LLM_TIMEOUT_S = _int("RR_LLM_TIMEOUT_S", 60)
LLM_RETRIES = _int("RR_LLM_RETRIES", 3)
LLM_RPM = _int("RR_LLM_RPM", 10)  # free-tier requests per minute
LLM_429_MIN_WAIT_S = 20  # rate-limit retries wait at least this long (or Gemini's retryDelay)
LLM_MAX_WAIT_S = 60
PIPELINE_VERSION = "8"  # bump when prompts or models change (used for caching)

# --- Repo access -------------------------------------------------------------
GITHUB_TOKEN = _env("GITHUB_TOKEN")
GITHUB_API_TIMEOUT_S = 15
GIT_CMD_TIMEOUT_S = 30
REPO_CLONE_TIMEOUT_S = _int("RR_CLONE_TIMEOUT_S", 60)
MAX_REPO_KB = _int("RR_MAX_REPO_KB", 50_000)  # packed size reported by the GitHub API
MAX_CHECKOUT_MB = _int("RR_MAX_CHECKOUT_MB", 200)  # unpacked size, checked before checkout
MAX_CHECKOUT_FILES = _int("RR_MAX_CHECKOUT_FILES", 20_000)
MAX_FILES = _int("RR_MAX_FILES", 200)  # safety cap; every selected file is reviewed
MAX_FILE_BYTES = _int("RR_MAX_FILE_BYTES", 100_000)

# --- Prompt sizing -------------------------------------------------------------
MAX_PROMPT_CHARS = _int("RR_MAX_PROMPT_CHARS", 30_000)  # small batches: free-tier models miss issues in big ones
PROMPT_OVERHEAD_CHARS = 5_000  # room for the system prompt and tool evidence in each chunk
# Agents review ALL selected files in successive batches; this cap only stops a huge repo from
# using up the free daily quota in one review (files beyond it are listed in the report).
MAX_CHUNKS_PER_AGENT = _int("RR_MAX_CHUNKS_PER_AGENT", 20)
AGENT_WORKERS = 4  # architect, security, quality, bug hunter
ENABLE_BUG_HUNTER = _env("RR_BUG_HUNTER") == "1"
MAX_ARCHITECT_FILES = 12
ARCHITECT_FILE_CHARS = 3_000  # the architect sees only the head of each file
MAX_TREE_LINES = 300
MAX_TOOL_HITS_PER_CHUNK = 60
MAX_FINDINGS_PER_CHUNK = _int("RR_MAX_FINDINGS_PER_CHUNK", 15)  # asked of the model per batch
MAX_FINDINGS_PER_AGENT = _int("RR_MAX_FINDINGS_PER_AGENT", 60)  # kept per agent after merging batches
TOP_FIXES = 5

# --- Static analysis ---------------------------------------------------------
SCAN_TIMEOUT_S = _int("RR_SCAN_TIMEOUT_S", 120)
BANDIT_INI = ROOT / "src" / "tools" / "bandit.ini"  # ours, never the repo's .bandit
BANDIT_SKIP = "B101"  # assert_used: noise in test suites
RUFF_RULES = "F,E9,E722,B,C901,SIM,N,PLR0911,PLR0912,PLR0913,PLR0915"
MAX_PATTERN_LINE_CHARS = 1_000  # skip minified lines in the regex scan
# Free Semgrep registry rule packs per language (fetched from semgrep.dev at scan time).
SEMGREP_PACKS = {
    "javascript": ["p/javascript", "p/nodejsscan"],
    "typescript": ["p/typescript", "p/nodejsscan"],
    "go": ["p/golang", "p/gosec"],
}
SEMGREP_RULE_TIMEOUT_S = 10  # per rule per file
SEMGREP_JOBS = 1
SEMGREP_MAX_MEMORY_MB = 300  # Semgrep's own per-file memory cap
SEMGREP_MAX_TARGET_BYTES = 50_000  # Semgrep skips bigger files (the LLM agents still read them)
SEMGREP_MAX_CMD_CHARS = 20_000  # explicit target lists are split to stay under the OS command-line limit
SEMGREP_MESSAGE_CHARS = 120

# --- Verifier ----------------------------------------------------------------
VERIFY_CONTEXT_LINES = 15
EVIDENCE_SLACK_LINES = 3
MIN_EVIDENCE_CHARS = 8
VERIFY_WORKERS = 3

# --- Paths -------------------------------------------------------------------
PROMPTS_DIR = ROOT / "prompts"


def _is_writable(directory: Path) -> bool:
    """Probe with a uniquely named temp file (O_EXCL, random name), so concurrent processes never
    collide and a pre-planted file or symlink is never opened."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".rr-write-test-"):
            pass
        return True
    except OSError:
        return False


def _private_temp_dir() -> Path | None:
    """A per-user directory under the system temp folder that only this user can use.

    On a shared host another user could pre-create a predictable /tmp path to plant cache entries or
    symlinks, so on POSIX it must be a real directory owned by us with no group/other permissions.
    """
    user = os.getuid() if hasattr(os, "getuid") else getpass.getuser()
    path = Path(tempfile.gettempdir()) / f"repo-reviewer-{user}"
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        if hasattr(os, "getuid"):
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                return None
    except OSError:
        return None
    return path if _is_writable(path) else None


def _data_dir() -> Path:
    """Where logs, the review cache and CLI reports are written.

    RR_DATA_DIR if set (relative paths are relative to the project), else the project folder when it is
    writable, else a private per-user temp folder (hosted platforms may mount the code read-only).
    """
    configured = _env("RR_DATA_DIR")
    if configured:
        path = Path(configured)
        path = path if path.is_absolute() else ROOT / path
        if _is_writable(path):
            return path
        print(f"repo-reviewer: RR_DATA_DIR={configured!r} is not writable; using a fallback location",
              file=sys.stderr)
    if _is_writable(ROOT):
        return ROOT
    return _private_temp_dir() or Path(tempfile.mkdtemp(prefix="repo-reviewer-"))


DATA_DIR = _data_dir()
LOGS_DIR = DATA_DIR / "logs"
TRACE_FILE = LOGS_DIR / "trace.jsonl"
TRACE_MAX_BYTES = 5_000_000  # rotate the trace log above this size
TRACE_VIEW_MAX_EVENTS = 500
ERROR_LOG_CHARS = 300
REPORTS_DIR = DATA_DIR / "reports"  # CLI default output (--out overrides)
CACHE_DIR = DATA_DIR / ".cache" / "reviews"
CACHE_MAX_ENTRIES = 200
EVALS_DIR = ROOT / "evals"

# --- Web app (Phase 2) ----------------------------------------------------------
APP_DAILY_RUN_CAP = _int("RR_APP_DAILY_RUN_CAP", 40)  # fresh (non-cached) reviews per day, all users
APP_COOLDOWN_S = _int("RR_APP_COOLDOWN_S", 120)  # per browser session, between fresh reviews
APP_MAX_CONCURRENT = 2  # simultaneous reviews (free-tier RPM is shared)
APP_MAX_CONCURRENT_LOOKUPS = 4  # simultaneous `git ls-remote` cache lookups
# --- Pull-request bot (Phase 3) -----------------------------------------------------
PR_MAX_INLINE_COMMENTS = 25  # per run; the summary comment always lists everything
PR_NEARBY_LINES = 2  # a finding cited this close to a changed line is attached to that line
PR_MAX_CHANGED_LINES = 5_000  # bigger pull requests are refused (reported, not silently half-reviewed)
PR_MAX_TOTAL_INLINE_COMMENTS = 60  # per pull request, across all runs
PR_BOT_LOGIN = _env("RR_BOT_LOGIN", "github-actions[bot]")  # only this author's comments are trusted

APP_TRACE_COLUMNS = ["ts", "event", "agent", "model", "attempt", "latency_s", "prompt_chars", "reply_chars", "code"]
