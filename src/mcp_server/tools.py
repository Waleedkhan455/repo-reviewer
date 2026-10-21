"""Tool logic for the MCP server: validate input, call the service layer through the job manager, and turn
results into sanitized output models.

No pipeline logic lives here (see ``src/service.py``). Results are converted and sanitized once, in the
worker thread, and stored with the job; the paging tools only slice what is stored.
"""

import asyncio
import logging
import re
import secrets
import warnings
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from src import config, service
from src.mcp_server import roots
from src.mcp_server.jobs import Job, JobManager, JobNotFound, Kind, Progress, QueueFull
from src.mcp_server.models import (
    FINDING_UNTRUSTED,
    ChangesReview,
    FindingDetail,
    FindingOut,
    FindingsPage,
    JobStatus,
    RepoReview,
    ReportOut,
    ReviewHandle,
    ServerStatus,
    sanitize,
    sanitize_code,
    sanitize_error,
    sanitize_path,
    sanitize_report,
)
from src.schemas import SEVERITY_ORDER, Finding

log = logging.getLogger("repo_reviewer.mcp")

# roots/list is deprecated in the 2026-07-28 spec but still what Claude Code, Cursor and Claude Desktop send;
# when a client lacks it we fall back to RR_MCP_ROOTS and the start folder.
warnings.filterwarnings("ignore", message="The roots capability is deprecated")

Mode = Literal["local", "remote"]
LOCAL_OWNER = "local"  # local mode has one user: the person who started the server
AGENTS = {"security", "quality", "bug_hunter"}
_SHA = re.compile(r"[0-9a-f]{40}")
_PLAIN_ID = re.compile(r"[A-Za-z0-9_-]+")


class QuotaExceeded(Exception):  # noqa: N818 - a condition, like QueueFull
    """The caller (or the server) has used up its fresh reviews for today."""


Charge = Callable[[str], None]  # charge(owner): raises QuotaExceeded when no fresh review is left


@dataclass
class Stored:
    """A finished review in output form."""

    summary: RepoReview | ChangesReview
    findings: list[FindingOut]  # every confirmed finding, highest severity first
    report_md: str  # sanitized and capped
    report_truncated: bool = False
    paths: dict[str, str] = field(default_factory=dict)  # finding id -> real repo-relative path
    root: Path | None = None  # the local checkout (local mode only), for code context


# --- conversions (run in the worker thread) ---------------------------------------


def finding_out(f: Finding) -> FindingOut:
    """Our own fields (id, agent, severity, line) are validated; text from the model or the code is sanitized."""
    fid = f.id if _PLAIN_ID.fullmatch(f.id) and len(f.id) <= config.MCP_ID_MAX_CHARS else "invalid-id"
    return FindingOut(id=fid, agent=f.source if f.source in AGENTS else "unknown", severity=f.severity,
                      file=sanitize_path(f.file), line=f.line,
                      issue=sanitize(f.issue, config.MCP_TEXT_MAX_CHARS),
                      evidence=sanitize(f.evidence, config.MCP_EVIDENCE_MAX_CHARS),
                      fix=sanitize(f.fix, config.MCP_TEXT_MAX_CHARS))


def _by_severity(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.file, f.line))


def _texts(items: list[str]) -> list[str]:
    return [sanitize(w, config.MCP_TEXT_MAX_CHARS) for w in items]


def _untrusted(*fields: str) -> list[str]:
    """Names of output fields that carry reviewed-code text (finding lists expand to their text fields)."""
    return [*fields, *(f"<finding>.{name}" for name in FINDING_UNTRUSTED)]


def repo_stored(result: service.ReviewResult) -> Stored:
    data = result.data
    confirmed_ids = set(data.confirmed_ids)
    confirmed = _by_severity([f for f in data.raw_findings if f.id in confirmed_ids])
    out = [finding_out(f) for f in confirmed]
    summary = RepoReview(
        repo=sanitize_path(data.repo), commit=sanitize(data.commit, config.MCP_ID_MAX_CHARS), cached=result.cached,
        degraded=data.degraded, score=data.score.score, findings_per_kloc=data.score.per_kloc,
        counts=dict(data.score.counts), top_findings=out[: config.MCP_TOP_FINDINGS], total_findings=len(out),
        rejected_by_verifier=len(data.rejected), warnings=_texts(data.warnings),
        untrusted_fields=_untrusted("top_findings", "warnings"))
    report, truncated = sanitize_report(result.report_md, config.MCP_REPORT_MAX_CHARS)
    return Stored(summary, out, report, truncated, {f.id: f.file for f in confirmed})


def changes_stored(review: service.PRReview, root: Path, base: str, head: str | None, mode: Mode) -> Stored:
    in_diff = [finding_out(f) for f in review.in_diff]
    outside = [finding_out(f) for f in review.outside]  # both lists arrive sorted by severity
    summary = ChangesReview(
        path=sanitize_path(root.as_posix(), config.MCP_INPUT_MAX_CHARS),
        base=sanitize(base, config.MCP_REF_MAX_CHARS), head=sanitize(head, config.MCP_REF_MAX_CHARS) if head else None,
        complete=review.complete, degraded=review.degraded, changed_line_count=review.changed_line_count,
        in_diff=in_diff, outside=outside, not_reviewed=_texts(review.not_reviewed),
        scanner_flags=[sanitize(h.as_line(), config.MCP_TEXT_MAX_CHARS) for h in review.scanner_flags],
        score=review.result_score.score, warnings=_texts(review.warnings),
        untrusted_fields=_untrusted("in_diff", "outside", "not_reviewed", "scanner_flags", "warnings"))
    # One list for paging, highest severity first; on equal severity, findings on changed lines come first.
    everything = sorted(in_diff + outside, key=lambda f: SEVERITY_ORDER[f.severity])
    report, truncated = sanitize_report(_changes_markdown(summary), config.MCP_REPORT_MAX_CHARS)
    paths = {f.id: f.file for f in [*review.in_diff, *review.outside]}
    return Stored(summary, everything, report, truncated, paths, root if mode == "local" else None)


def _changes_markdown(s: ChangesReview) -> str:
    lines = [f"# Review of changes (base {s.base}, head {s.head or 'working tree'})", "",
             (f"Score {s.score}/100, {s.changed_line_count} changed lines, "
              f"{'complete' if s.complete else 'INCOMPLETE'}{', degraded' if s.degraded else ''}."), ""]
    for title, items in (("Findings on changed lines", s.in_diff), ("Other confirmed findings", s.outside)):
        lines += [f"## {title}", ""] + ([f"- **{f.severity}** {f.file}:{f.line} {f.issue} Fix: {f.fix}"
                                         for f in items] or ["None."]) + [""]
    for title, items in (("Not reviewed", s.not_reviewed), ("Unconfirmed scanner flags", s.scanner_flags),
                         ("Warnings", s.warnings)):
        if items:
            lines += [f"## {title}", "", *(f"- {i}" for i in items), ""]
    return "\n".join(lines)


def safe_error(exc: Exception) -> str:
    """A user-facing message for a failed review; never a stack trace or an internal path."""
    def detail() -> str:
        return sanitize_error(exc, config.MCP_ERROR_MAX_CHARS)

    if isinstance(exc, service.RepoError):
        return "invalid or unsupported repo: " + detail()
    if isinstance(exc, QuotaExceeded):
        return "daily limit reached, resets at 00:00 UTC"
    if isinstance(exc, service.LLMError):
        return "model unavailable, try later"
    if isinstance(exc, (service.DiffError, roots.PathNotAllowed)):
        return detail()
    if isinstance(exc, service.ReviewError):
        return "the review failed: every model call failed, try later"
    log.error("review failed: %s", type(exc).__name__, exc_info=exc)
    return "the review failed unexpectedly"


# --- the tools ----------------------------------------------------------------------


class ReviewTools:
    def __init__(self, mode: Mode, jobs: JobManager, cwd: Path, charge: Charge | None = None) -> None:
        self.mode = mode
        self.jobs = jobs
        self.cwd = cwd
        self.charge = charge  # quota for fresh repo reviews; None = the user's own key (local mode)

    def owner(self, ctx: Context | None) -> str:
        return LOCAL_OWNER  # remote mode binds jobs to a caller id (Phase 2)

    async def allowed_roots(self, ctx: Context) -> list[Path]:
        client: list[Path] = []
        try:
            listed = await asyncio.wait_for(ctx.session.list_roots(), config.MCP_ROOTS_TIMEOUT_S)
            client = [p for p in (roots.file_uri_to_path(str(r.uri)) for r in listed.roots) if p]
        except Exception as exc:  # the client does not support roots: fall back to configured folders
            log.debug("roots/list unavailable: %s", type(exc).__name__)
        return await asyncio.to_thread(roots.pick_roots, client, config.MCP_ROOTS, self.cwd)

    async def review_changes(self, ctx: Context, path: str, base: str, head: str | None, include_untracked: bool,
                             bug_hunter: bool, wait_seconds: int) -> ChangesReview | ReviewHandle:
        allowed = await self.allowed_roots(ctx)
        try:
            root = await asyncio.to_thread(roots.resolve_project, path, allowed, self.cwd)
            await asyncio.to_thread(service.check_checkout, root)
            base_sha = await asyncio.to_thread(service.resolve_ref, root, base)
            head_sha = None
            if head:
                head_sha = await asyncio.to_thread(service.resolve_ref, root, head)
                current = await asyncio.to_thread(service.resolve_ref, root, "HEAD")
        except (roots.PathNotAllowed, service.DiffError) as exc:
            raise ToolError(safe_error(exc)) from None
        if head_sha and head_sha != current:
            raise ToolError("check out the head commit first: the review reads files from the working tree")
        if head_sha and head_sha == base_sha:
            raise ToolError("base and head are the same commit; pass the branch to compare with as base, e.g. main")

        def run(progress: Progress) -> Stored:
            if head_sha:
                review = service.review_changes(root, base_sha, head_sha, bug_hunter=bug_hunter, progress=progress)
            else:
                review = service.review_worktree(root, base_sha, include_untracked=include_untracked,
                                                 bug_hunter=bug_hunter, progress=progress)
            return changes_stored(review, root, base, head, self.mode)

        # Committed changes are fixed, so identical requests share one run. The working tree is not: a call
        # after the user edited files must review the new contents, so working-tree reviews never coalesce.
        fresh = None if head_sha else secrets.token_hex(8)
        key = ("changes", str(root), base_sha, head_sha, include_untracked, bug_hunter, fresh)
        job = self._submit(ctx, "changes", key, run)
        return await self._result_or_handle(ctx, job, wait_seconds)

    async def review_repo(self, ctx: Context, url: str, commit: str | None, bug_hunter: bool,
                          wait_seconds: int) -> RepoReview | ReviewHandle:
        if commit is not None and not _SHA.fullmatch(commit):
            raise ToolError("commit must be a full 40-character lowercase SHA")
        options = service.ReviewOptions(bug_hunter=bug_hunter, commit=commit)
        try:
            lookup = await asyncio.to_thread(service.find_cached_review, url, options)
        except Exception as exc:
            raise ToolError(safe_error(exc)) from None
        if lookup.hit:
            stored = await asyncio.to_thread(repo_stored, lookup.hit)
            job = self._finished(ctx, "repo", stored)
            return stored.summary.model_copy(update={"job_id": job.id})

        pinned = service.ReviewOptions(bug_hunter=bug_hunter, commit=lookup.commit)

        def run(progress: Progress) -> Stored:
            return repo_stored(service.review_repo(url, pinned, progress))

        key = ("repo", lookup.key)
        if self.charge is not None and not self.jobs.has_active(self.owner(ctx), key):  # joining is free
            try:
                self.charge(self.owner(ctx))
            except QuotaExceeded as exc:
                raise ToolError(safe_error(exc)) from None
        job = self._submit(ctx, "repo", key, run)
        return await self._result_or_handle(ctx, job, wait_seconds)

    async def get_review_status(self, ctx: Context | None, job_id: str) -> JobStatus:
        job = self._job(ctx, job_id)
        view = self.jobs.snapshot(job)
        summary = view.result.summary.model_copy(update={"job_id": job.id}) if isinstance(view.result, Stored) else None
        return JobStatus(job_id=job.id, kind=job.work.kind, state=view.state, position=view.position,
                         progress=_texts(view.progress[-config.MCP_STATUS_PROGRESS:]), error=view.error,
                         result=summary)

    async def get_report(self, ctx: Context, job_id: str, format: Literal["summary", "markdown"]) -> ReportOut:
        job, stored = self._done(ctx, job_id)
        if format == "markdown":
            content, truncated = stored.report_md, stored.report_truncated
        else:
            content, truncated = sanitize_report(_summary_text(stored.summary), config.MCP_REPORT_MAX_CHARS)
        return ReportOut(job_id=job.id, format=format, content=content, truncated=truncated,
                         untrusted_fields=["content"])

    async def list_findings(self, ctx: Context, job_id: str, min_severity: Literal["high", "medium", "low"],
                            agent: str | None, file: str | None, limit: int, offset: int) -> FindingsPage:
        job, stored = self._done(ctx, job_id)
        floor = SEVERITY_ORDER[min_severity]
        prefix = file.strip("/") if file else None

        def keep(f: FindingOut) -> bool:
            real = stored.paths.get(f.id, f.file)
            return (SEVERITY_ORDER[f.severity] <= floor and (agent is None or f.agent == agent)
                    and (prefix is None or real == prefix or real.startswith(prefix + "/")))

        matching = [f for f in stored.findings if keep(f)]
        limit = max(1, min(limit, config.MCP_PAGE_MAX))
        offset = max(0, offset)
        return FindingsPage(job_id=job.id, items=matching[offset: offset + limit], total=len(matching),
                            offset=offset, limit=limit, untrusted_fields=_untrusted("items"))

    async def get_finding(self, ctx: Context, job_id: str, finding_id: str, context_lines: int) -> FindingDetail:
        job, stored = self._done(ctx, job_id)
        finding = next((f for f in stored.findings if f.id == finding_id), None)
        if finding is None:
            raise ToolError("unknown finding id for this job")
        context = None
        if self.mode == "local" and stored.root is not None and finding.id in stored.paths:
            n = max(0, min(context_lines, config.MCP_CONTEXT_LINES_MAX))
            context = await asyncio.to_thread(_code_context, stored.root, stored.paths[finding.id], finding.line, n)
        return FindingDetail(job_id=job.id, finding=finding, context=context,
                             untrusted_fields=_untrusted("finding", "context"))

    async def server_status(self, ctx: Context) -> ServerStatus:
        return ServerStatus(mode=self.mode, model=config.MODEL_NAME, fallback_models=list(config.MODEL_FALLBACKS),
                            pipeline_version=config.PIPELINE_VERSION, queue_length=self.jobs.queue_length(),
                            jobs_stored=self.jobs.jobs_stored(), daily_reviews_left=None,
                            max_wait_seconds=config.MCP_MAX_WAIT_S)

    async def job_json(self, ctx: Context | None, job_id: str) -> str:
        """Resource body: a job's status as JSON."""
        try:
            return (await self.get_review_status(ctx, job_id)).model_dump_json()
        except ToolError as exc:
            raise ResourceError(str(exc)) from None

    async def cached_report(self, owner: str, repo_name: str, commit: str) -> str:
        """Resource body: the cached Markdown report for one commit. Never starts a review."""
        if not _SHA.fullmatch(commit):
            raise ResourceError("commit must be a full 40-character lowercase SHA")
        url = f"https://github.com/{owner}/{repo_name}"
        try:
            lookup = await asyncio.to_thread(service.find_cached_review, url, service.ReviewOptions(commit=commit))
        except service.RepoError as exc:
            raise ResourceError(safe_error(exc)) from None
        if lookup.hit is None:
            return "No cached review for this commit. Start one with the review_repo tool."
        return sanitize_report(lookup.hit.report_md, config.MCP_REPORT_MAX_CHARS)[0]

    # --- helpers --------------------------------------------------------------

    def _submit(self, ctx: Context, kind: Kind, key: Hashable, run: Callable[[Progress], Stored]) -> Job:
        try:
            job, _joined = self.jobs.submit(self.owner(ctx), kind, key, run, safe_error)
        except QueueFull as exc:
            raise ToolError(str(exc)) from None
        return job

    def _finished(self, ctx: Context, kind: Kind, stored: Stored) -> Job:
        try:
            return self.jobs.add_finished(self.owner(ctx), kind, stored)
        except QueueFull as exc:
            raise ToolError(str(exc)) from None

    def _job(self, ctx: Context | None, job_id: str) -> Job:
        try:
            return self.jobs.get(job_id, self.owner(ctx))
        except JobNotFound as exc:
            raise ToolError(str(exc)) from None

    def _done(self, ctx: Context, job_id: str) -> tuple[Job, Stored]:
        job = self._job(ctx, job_id)
        view = self.jobs.snapshot(job)
        if view.state == "failed":
            raise ToolError(view.error or "the review failed")
        if not isinstance(view.result, Stored):
            raise ToolError(f"the review is still {view.state}; poll get_review_status")
        return job, view.result

    async def _result_or_handle(self, ctx: Context, job: Job,
                                wait_seconds: int) -> RepoReview | ChangesReview | ReviewHandle:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, min(wait_seconds, config.MCP_MAX_WAIT_S))
        last, step = None, 0
        while True:
            remaining = deadline - loop.time()
            finished = await asyncio.to_thread(self.jobs.wait, job, min(1.0, max(0.0, remaining)))
            view = self.jobs.snapshot(job)
            if view.progress and view.progress[-1] != last:
                last, step = view.progress[-1], step + 1
                try:
                    await ctx.report_progress(step, None, sanitize(last, config.MCP_ERROR_MAX_CHARS))
                except Exception as exc:  # progress is best effort
                    log.debug("progress not sent: %s", type(exc).__name__)
            if finished:
                if view.state == "failed" or not isinstance(view.result, Stored):
                    raise ToolError(view.error or "the review failed")
                return view.result.summary.model_copy(update={"job_id": job.id})
            if remaining <= 0:
                return ReviewHandle(job_id=job.id, state="queued" if view.state == "queued" else "running",
                                    position=view.position, poll_after_seconds=config.MCP_POLL_AFTER_S)


def _summary_text(s: RepoReview | ChangesReview) -> str:
    if isinstance(s, RepoReview):
        head = (f"{s.repo} at {s.commit[:12]}: score {s.score}/100, {s.total_findings} confirmed findings "
                f"({s.counts.get('high', 0)} high, {s.counts.get('medium', 0)} medium, {s.counts.get('low', 0)} low), "
                f"{s.rejected_by_verifier} rejected by the Verifier.")
        items = s.top_findings
    else:
        head = (f"Changes vs {s.base}: score {s.score}/100, {len(s.in_diff)} findings on changed lines, "
                f"{len(s.outside)} elsewhere; {'complete' if s.complete else 'incomplete'}.")
        items = s.in_diff
    lines = [head + (" Some steps failed (degraded)." if s.degraded else "")]
    lines += [f"- [{f.severity}] {f.file}:{f.line} {f.issue}" for f in items[: config.MCP_TOP_FINDINGS]]
    return "\n".join(lines)


def _code_context(root: Path, rel: str, line: int, n: int) -> list[str] | None:
    """Numbered lines around ``line``, split exactly as the agents saw the file; None if it cannot be read
    safely (outside the project, a symlink escape, or too large)."""
    text = service.source_lines(root, rel)
    if text is None:
        return None
    start, end = max(1, line - n), min(len(text), line + n)
    return [f"{i:>5}: {sanitize_code(text[i - 1], config.MCP_TEXT_MAX_CHARS)}" for i in range(start, end + 1)]
