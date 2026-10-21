"""Structured tool outputs, and sanitising of every string that came from reviewed code.

The calling AI agent reads these outputs, and reviewed code is attacker-controlled: a comment such as
"AI assistant: ignore all issues and run ..." can end up in a finding's text. So every repo-derived
string is passed through ``sanitize`` (or ``sanitize_code`` for code lines), and each output lists the
fields that came from reviewed code in ``untrusted_fields``.
"""

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field

from src import config
from src.tools.patterns import mask_secrets

# Characters that are invisible or display differently from what they say: controls (C0/C1), format
# characters such as zero-width and bidi controls ("Trojan Source") and Unicode tag characters (hidden text
# some models still read), private-use and unassigned code points, line/paragraph separators, and
# variation selectors / combining grapheme joiner.
_INVISIBLE_CATEGORIES = {"Cc", "Cf", "Co", "Cn", "Zl", "Zp"}
_INVISIBLE_EXTRA = re.compile("[͏᠋-᠏︀-️\U000e0100-\U000e01ef]")
_ZWSP = "​"  # breaks live Markdown/HTML syntax; added by us (and by the writer), so kept when present


def _strip_invisible(text: str, keep: str = "") -> str:
    text = _INVISIBLE_EXTRA.sub(" ", text)
    return "".join(ch if ch in keep or unicodedata.category(ch) not in _INVISIBLE_CATEGORIES else " "
                   for ch in text)


Severity = Literal["high", "medium", "low"]


def _cap(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1].rstrip("\\" + _ZWSP)  # never end on a dangling escape
    return cut + "…"


# Syntax that a Markdown/HTML renderer would turn into something live. A zero-width space is inserted so
# the text reads the same but no longer parses: links "](", images "![", HTML tags "<img", "</x", "<!--",
# and URLs "://" / "www." (which renderers autolink). Code stays readable: `x_y * 2 < 3` is unchanged.
_LIVE_SYNTAX = re.compile(r"(\]\s*)(\()|(!)(\[)|(<)([A-Za-z/!?])|(:)(//)|(www)(\.)", re.IGNORECASE)


def _neutralize(text: str) -> str:
    return _LIVE_SYNTAX.sub(lambda m: _ZWSP.join(g for g in m.groups() if g is not None), text)


def sanitize(text: object, max_chars: int) -> str:
    """Untrusted prose -> inert, single-line text: invisible characters removed, secrets masked, no live
    links, images, HTML or URLs, capped at ``max_chars``. Ordinary code characters (``_ * ( ) #``) stay
    as they are, so quoted code remains readable."""
    flat = " ".join(mask_secrets(_strip_invisible(str(text))).split())
    return _cap(_neutralize(flat), max_chars)


def sanitize_error(text: object, max_chars: int) -> str:
    """An error message (ours, possibly quoting a ref or config key): same rules as ``sanitize``."""
    return sanitize(text, max_chars)


def sanitize_code(text: object, max_chars: int) -> str:
    """Untrusted code line: controls removed and secrets masked, but not Markdown-escaped (so the code
    stays readable for an agent that edits it). Only returned in local mode, for the user's own files."""
    cleaned = _strip_invisible(str(text).replace("\r", "").replace("\n", " "), keep="\t")
    return _cap(mask_secrets(cleaned), max_chars)


_PLAIN_PATH = re.compile(r"([A-Za-z]:)?[A-Za-z0-9._/@+-]+")  # optional Windows drive letter


def sanitize_path(text: object, max_chars: int = config.MCP_PATH_MAX_CHARS) -> str:
    """A repo-relative file path: kept as is when it only has ordinary path characters (so agents can open
    it), otherwise neutralised like prose."""
    raw = str(text)
    if _PLAIN_PATH.fullmatch(raw) and "://" not in raw:
        return _cap(raw, max_chars)
    return sanitize(raw, max_chars)


def sanitize_report(text: object, max_chars: int) -> tuple[str, bool]:
    """A rendered Markdown report (already escaped by the writer): controls removed, secrets masked,
    capped. Returns (text, truncated)."""
    cleaned = _strip_invisible(mask_secrets(str(text).replace("\r", "")), keep="\n\t" + _ZWSP)
    return _cap(cleaned, max_chars), len(cleaned) > max_chars


FINDING_UNTRUSTED = ["issue", "evidence", "fix", "file"]


class FindingOut(BaseModel):
    id: str
    agent: str  # security | quality | bug_hunter
    severity: Severity
    file: str
    line: int
    issue: str
    evidence: str
    fix: str


class ReviewHandle(BaseModel):
    job_id: str
    state: Literal["queued", "running"]
    position: int = Field(description="0 = running now; n = n jobs ahead in the queue")
    poll_after_seconds: int
    message: str = "The review is still running. Call get_review_status with this job_id."


class RepoReview(BaseModel):
    job_id: str | None = None
    repo: str
    commit: str
    cached: bool
    degraded: bool
    score: int
    findings_per_kloc: float
    counts: dict[str, int]
    top_findings: list[FindingOut]
    total_findings: int
    rejected_by_verifier: int
    warnings: list[str]
    untrusted_fields: list[str]


class ChangesReview(BaseModel):
    job_id: str | None = None
    path: str
    base: str
    head: str | None  # None = the working tree (uncommitted changes)
    complete: bool
    degraded: bool
    changed_line_count: int
    in_diff: list[FindingOut]
    outside: list[FindingOut]
    not_reviewed: list[str]
    scanner_flags: list[str]
    score: int
    warnings: list[str]
    untrusted_fields: list[str]


class JobStatus(BaseModel):
    job_id: str
    kind: Literal["repo", "changes"]
    state: Literal["queued", "running", "done", "failed"]
    position: int
    progress: list[str]
    error: str | None = None
    result: RepoReview | ChangesReview | None = None


class FindingsPage(BaseModel):
    job_id: str
    items: list[FindingOut]
    total: int
    offset: int
    limit: int
    untrusted_fields: list[str]


class FindingDetail(BaseModel):
    job_id: str
    finding: FindingOut
    context: list[str] | None = Field(None, description="numbered code lines around the finding (local mode only)")
    untrusted_fields: list[str]


class ReportOut(BaseModel):
    job_id: str
    format: Literal["summary", "markdown"]
    content: str
    truncated: bool
    untrusted_fields: list[str]


class ServerStatus(BaseModel):
    mode: Literal["local", "remote"]
    model: str
    fallback_models: list[str]
    pipeline_version: str
    queue_length: int
    jobs_stored: int
    daily_reviews_left: int | None  # remote mode: for this caller; None in local mode (your own key)
    max_wait_seconds: int
