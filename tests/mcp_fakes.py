"""Shared fakes for the MCP server tests: a temp git repo, a fake service layer, an in-memory client."""

import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import Client
from mcp.types import ListRootsResult, Root

from src import repo, service
from src.changes import PRReview
from src.mcp_server.jobs import JobManager
from src.mcp_server.server import build_server
from src.orchestrator import CacheLookup, ReviewResult
from src.schemas import ArchitectOut, Finding, Plan, ReviewData, RunStatsOut
from src.scoring import score

COMMIT = "a" * 40


def git_repo(root: Path, files: dict[str, str]) -> Path:
    """A git repo with one commit of ``files``."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"],
                ["-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=root, check=True, capture_output=True)
    return root


def finding(fid="S1", severity="high", file="app.py", line=2, issue="SQL injection", source="security",
            evidence="cur.execute(q)", fix="use parameters") -> Finding:
    return Finding(id=fid, file=file, line=line, severity=severity, issue=issue, evidence=evidence, fix=fix,
                   source=source)


def pr_review(in_diff=(), outside=()) -> PRReview:
    return PRReview(list(in_diff), list(outside), [], score(list(in_diff), 2), ["app.py"], 2, 3)


def review_result(findings: list[Finding], cached=False, report="# Report\n") -> ReviewResult:
    data = ReviewData(
        run_id="r", repo="octo/demo", commit=COMMIT, pipeline_version="9", model="m", verified=True,
        plan=Plan(languages=["python"], assignments={"security": [], "quality": []}), tool_hits=0,
        duplicates_dropped=[], invalid_items_dropped=0, raw_findings=findings,
        confirmed_ids=[f.id for f in findings], rejected=[], architecture=ArchitectOut(summary="s"),
        score=score(findings, 1000), stats=RunStatsOut(seconds=1, llm_calls=1, llm_errors=0, llm_seconds=1),
        warnings=[])
    return ReviewResult(report_md=report, data=data, cached=cached)


class FakeService:
    """Replaces the service calls the MCP tools make. ``calls`` records what was asked."""

    def __init__(self, monkeypatch, *, changes=None, repo=None, cached=None, error=None):
        self.calls: list[tuple] = []
        self.changes = changes if changes is not None else pr_review()
        self.repo = repo if repo is not None else review_result([])
        self.cached, self.error = cached, error
        monkeypatch.setattr(service, "review_worktree", self.review_worktree)
        monkeypatch.setattr(service, "review_changes", self.review_changes)
        monkeypatch.setattr(service, "find_cached_review", self.find_cached_review)
        monkeypatch.setattr(service, "review_repo", self.review_repo)

    def _maybe_fail(self):
        if self.error:
            raise self.error

    def review_worktree(self, root, base="HEAD", *, include_untracked=True, bug_hunter=False, progress=None):
        self.calls.append(("worktree", root, base, include_untracked, bug_hunter))
        if progress:
            progress("Running agents: security, quality")
        self._maybe_fail()
        return self.changes

    def review_changes(self, root, base, head, *, bug_hunter=False, progress=None):
        self.calls.append(("changes", root, base, head))
        self._maybe_fail()
        return self.changes

    def find_cached_review(self, url, options=None):
        self.calls.append(("lookup", url, options))
        repo.canonicalize(url)  # the real validation: raises RepoError for bad URLs
        commit = (options.commit if options else None) or COMMIT
        return CacheLookup(commit=commit, key=f"key-{url}-{commit}", hit=self.cached)

    def review_repo(self, url, options=None, progress=None):
        self.calls.append(("review", url, options))
        self._maybe_fail()
        return self.repo


@asynccontextmanager
async def client(mode="local", roots: list[Path] | None = None, cwd: Path | None = None):
    """An in-memory MCP client; ``roots`` are announced to the server as the client's roots."""
    jobs = JobManager(max_workers=2)
    server = build_server(mode, jobs=jobs, cwd=cwd)

    async def list_roots(_context):
        return ListRootsResult(roots=[Root(uri=p.resolve().as_uri()) for p in roots or []])

    kwargs = {"list_roots_callback": list_roots, "mode": "legacy"} if roots else {}
    try:
        async with Client(server, **kwargs) as c:
            yield c
    finally:
        jobs.shutdown()
