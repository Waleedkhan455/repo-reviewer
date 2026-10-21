"""MCP tools through an in-memory client, with the service layer faked (no network, no LLM)."""

import anyio
import pytest

from src import config
from src.diffs import DiffError
from src.llm import LLMError
from tests.mcp_fakes import COMMIT, FakeService, client, finding, git_repo, pr_review, review_result


@pytest.fixture
def anyio_backend():
    return "asyncio"


def out(result):
    """Structured output; union return types (review or handle) are wrapped as {"result": ...}."""
    assert not result.is_error, result.content
    data = result.structured_content
    return data.get("result", data) if set(data) == {"result"} else data


def error_text(result):
    assert result.is_error
    return " ".join(getattr(c, "text", "") for c in result.content)


@pytest.fixture
def repo(tmp_path):
    root = git_repo(tmp_path / "proj", {"app.py": "import os\n\n\ndef f(x):\n    return x\n"})
    (root / "app.py").write_text("import os\nq = 'SELECT ' + x\n\n\ndef f(x):\n    return x\n", encoding="utf-8")
    return root


# --- review_changes -------------------------------------------------------------------


@pytest.mark.anyio
async def test_review_changes_happy_path(repo, monkeypatch):
    fake = FakeService(monkeypatch, changes=pr_review(in_diff=[finding()], outside=[finding("Q1", "low", line=5)]))
    progress = []
    async with client(roots=[repo]) as c:
        r = out(await c.call_tool("review_changes", {"path": "."},
                                  progress_callback=lambda p, t, m: progress.append(m)))
    assert r["job_id"] and r["head"] is None and r["base"] == "HEAD"
    assert [f["id"] for f in r["in_diff"]] == ["S1"] and [f["id"] for f in r["outside"]] == ["Q1"]
    assert r["in_diff"][0]["file"] == "app.py" and "in_diff" in r["untrusted_fields"]
    kind, root, base, include_untracked, _bh = fake.calls[0]
    assert kind == "worktree" and root == repo.resolve() and len(base) == 40 and include_untracked
    assert any("security" in m for m in progress)


@pytest.mark.anyio
async def test_review_changes_uses_configured_roots_without_client_roots(repo, monkeypatch):
    FakeService(monkeypatch)
    monkeypatch.setattr(config, "MCP_ROOTS", [str(repo)])
    async with client() as c:  # the default client does not answer roots/list
        r = out(await c.call_tool("review_changes", {"path": str(repo)}))
    assert r["complete"] is True


@pytest.mark.anyio
async def test_review_changes_path_outside_roots(repo, tmp_path, monkeypatch):
    FakeService(monkeypatch)
    other = git_repo(tmp_path / "other", {"b.py": "x = 1\n"})
    async with client(roots=[repo]) as c:
        msg = error_text(await c.call_tool("review_changes", {"path": str(other)}))
        assert "outside the allowed folders" in msg
        msg = error_text(await c.call_tool("review_changes", {"path": "../other"}))
        assert "outside the allowed folders" in msg


@pytest.mark.anyio
async def test_review_changes_not_a_git_folder(tmp_path, monkeypatch):
    FakeService(monkeypatch)
    plain = tmp_path / "plain"
    plain.mkdir()
    async with client(roots=[plain]) as c:
        assert "not a git repository" in error_text(await c.call_tool("review_changes", {}))


@pytest.mark.anyio
async def test_review_changes_with_head(repo, monkeypatch):
    import subprocess
    fake = FakeService(monkeypatch)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qam", "two"], cwd=repo,
                   check=True, capture_output=True)
    async with client(roots=[repo]) as c:
        r = out(await c.call_tool("review_changes", {"base": "HEAD~1", "head": "HEAD"}))
        assert r["head"] == "HEAD" and fake.calls[0][0] == "changes"
        # Regression: head with the default base compared HEAD with itself and reported "clean".
        assert "same commit" in error_text(await c.call_tool("review_changes", {"head": "HEAD"}))
        assert "unknown git ref" in error_text(await c.call_tool("review_changes", {"base": "nope-branch"}))
        assert "not a valid git ref" in error_text(await c.call_tool("review_changes", {"base": "--output=x"}))


@pytest.mark.anyio
async def test_review_changes_head_must_be_checked_out(repo, monkeypatch):
    import subprocess
    FakeService(monkeypatch)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qam", "two"], cwd=repo,
                   check=True, capture_output=True)
    async with client(roots=[repo]) as c:
        msg = error_text(await c.call_tool("review_changes", {"base": "HEAD~1", "head": "HEAD~1"}))
    assert "check out the head commit" in msg


@pytest.mark.anyio
async def test_review_changes_failure_is_a_clean_error(repo, monkeypatch):
    FakeService(monkeypatch, error=LLMError("401 key C:/secret/path invalid"))
    async with client(roots=[repo]) as c:
        msg = error_text(await c.call_tool("review_changes", {}))
    assert "model unavailable, try later" in msg and "secret" not in msg


@pytest.mark.anyio
async def test_diff_error_message_is_passed_on(repo, monkeypatch):
    FakeService(monkeypatch, error=DiffError("change too large to review"))
    async with client(roots=[repo]) as c:
        assert "change too large to review" in error_text(await c.call_tool("review_changes", {}))


@pytest.mark.anyio
async def test_handle_then_poll(repo, monkeypatch):
    import threading
    gate = threading.Event()
    fake = FakeService(monkeypatch, changes=pr_review(in_diff=[finding()]))
    real = fake.review_worktree

    def slow(*args, **kwargs):
        gate.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr("src.service.review_worktree", slow)
    async with client(roots=[repo]) as c:
        handle = out(await c.call_tool("review_changes", {"wait_seconds": 0}))
        assert handle["state"] in ("queued", "running") and handle["poll_after_seconds"] > 0
        status = out(await c.call_tool("get_review_status", {"job_id": handle["job_id"]}))
        assert status["state"] in ("queued", "running") and status["result"] is None
        assert "still" in error_text(await c.call_tool("list_findings", {"job_id": handle["job_id"]}))
        # Regression: a re-run after editing files used to join the old run and return stale findings.
        again = out(await c.call_tool("review_changes", {"wait_seconds": 0}))
        assert again["job_id"] != handle["job_id"]
        gate.set()
        for _ in range(50):
            states = [out(await c.call_tool("get_review_status", {"job_id": j}))
                      for j in (handle["job_id"], again["job_id"])]
            status = states[0]
            if all(s["state"] == "done" for s in states):
                break
            await anyio.sleep(0.1)
    assert status["state"] == "done" and status["result"]["in_diff"][0]["id"] == "S1"
    assert sum(call[0] == "worktree" for call in fake.calls) == 2  # both runs reviewed the tree


# --- review_repo ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_review_repo_cache_hit_needs_no_review(monkeypatch):
    fake = FakeService(monkeypatch, cached=review_result([finding()], cached=True))
    async with client(mode="remote") as c:
        r = out(await c.call_tool("review_repo", {"url": "https://github.com/octo/demo"}))
        page = out(await c.call_tool("list_findings", {"job_id": r["job_id"]}))
    assert r["cached"] is True and r["commit"] == COMMIT and r["top_findings"][0]["id"] == "S1"
    assert page["total"] == 1 and [c[0] for c in fake.calls] == ["lookup"]


@pytest.mark.anyio
async def test_review_repo_miss_runs_pinned_review(monkeypatch):
    fake = FakeService(monkeypatch, repo=review_result([finding(), finding("Q1", "low")]))
    async with client(mode="remote") as c:
        r = out(await c.call_tool("review_repo", {"url": "https://github.com/octo/demo", "wait_seconds": 5}))
    assert r["cached"] is False and r["total_findings"] == 2 and r["counts"]["high"] == 1
    _, _, options = fake.calls[-1]
    assert fake.calls[-1][0] == "review" and options.commit == COMMIT


@pytest.mark.anyio
async def test_review_repo_errors(monkeypatch):
    FakeService(monkeypatch, error=LLMError("boom"))
    async with client(mode="remote") as c:
        assert "invalid or unsupported repo" in error_text(
            await c.call_tool("review_repo", {"url": "https://evil.example/x/y"}))
        assert "40-character" in error_text(
            await c.call_tool("review_repo", {"url": "https://github.com/octo/demo", "commit": "abc"}))
        assert "model unavailable" in error_text(
            await c.call_tool("review_repo", {"url": "https://github.com/octo/demo", "wait_seconds": 5}))
        long = "https://github.com/" + "a" * config.MCP_INPUT_MAX_CHARS
        assert (await c.call_tool("review_repo", {"url": long})).is_error


# --- reading results --------------------------------------------------------------------


@pytest.fixture
def many(monkeypatch):
    findings = [finding(f"S{i}", sev, file=f"pkg/m{i}.py", source=src)
                for i, (sev, src) in enumerate([("high", "security"), ("medium", "quality"), ("low", "quality"),
                                                ("low", "bug_hunter")])]
    return FakeService(monkeypatch, cached=review_result(findings, cached=True, report="# R\n" + "x" * 50_000))


@pytest.mark.anyio
async def test_list_findings_filters_and_pages(many):
    async with client(mode="remote") as c:
        job = out(await c.call_tool("review_repo", {"url": "https://github.com/octo/demo"}))["job_id"]

        async def page(**kw):
            return out(await c.call_tool("list_findings", {"job_id": job, **kw}))

        assert (await page())["total"] == 4
        assert [f["id"] for f in (await page(min_severity="medium"))["items"]] == ["S0", "S1"]
        assert [f["id"] for f in (await page(agent="quality"))["items"]] == ["S1", "S2"]
        assert [f["id"] for f in (await page(file="pkg/m3.py"))["items"]] == ["S3"]
        assert (await page(file="pkg"))["total"] == 4
        p = await page(limit=2, offset=2)
        assert [f["id"] for f in p["items"]] == ["S2", "S3"] and p["total"] == 4
        assert (await c.call_tool("list_findings", {"job_id": job, "limit": config.MCP_PAGE_MAX + 1})).is_error


@pytest.mark.anyio
async def test_get_report_formats(many):
    async with client(mode="remote") as c:
        job = out(await c.call_tool("review_repo", {"url": "https://github.com/octo/demo"}))["job_id"]
        summary = out(await c.call_tool("get_report", {"job_id": job}))
        md = out(await c.call_tool("get_report", {"job_id": job, "format": "markdown"}))
    assert "score" in summary["content"] and summary["truncated"] is False
    assert md["truncated"] is True and len(md["content"]) <= config.MCP_REPORT_MAX_CHARS
    assert md["untrusted_fields"] == ["content"]


@pytest.mark.anyio
async def test_get_finding_with_local_context(repo, monkeypatch):
    FakeService(monkeypatch, changes=pr_review(in_diff=[finding(line=2)]))
    async with client(roots=[repo]) as c:
        job = out(await c.call_tool("review_changes", {}))["job_id"]
        detail = out(await c.call_tool("get_finding", {"job_id": job, "finding_id": "S1", "context_lines": 1}))
        missing = await c.call_tool("get_finding", {"job_id": job, "finding_id": "NOPE"})
    assert detail["finding"]["id"] == "S1"
    assert detail["context"] == ["    1: import os", "    2: q = 'SELECT ' + x", "    3: "]
    assert "unknown finding id" in error_text(missing)


@pytest.mark.anyio
async def test_get_finding_context_never_leaves_the_project(repo, tmp_path, monkeypatch):
    (tmp_path / "outside.txt").write_text("TOP SECRET\n", encoding="utf-8")
    FakeService(monkeypatch, changes=pr_review(in_diff=[finding(file="../outside.txt", line=1)]))
    async with client(roots=[repo]) as c:
        job = out(await c.call_tool("review_changes", {}))["job_id"]
        detail = out(await c.call_tool("get_finding", {"job_id": job, "finding_id": "S1"}))
    assert detail["context"] is None


@pytest.mark.anyio
async def test_unknown_job_id(monkeypatch):
    FakeService(monkeypatch)
    async with client(mode="remote") as c:
        for tool in ("get_review_status", "get_report", "list_findings"):
            assert "unknown or expired job id" in error_text(await c.call_tool(tool, {"job_id": "nope"}))


# --- resources and prompts ---------------------------------------------------------------


@pytest.mark.anyio
async def test_cached_report_resource(monkeypatch):
    fake = FakeService(monkeypatch, cached=review_result([], cached=True, report="# Cached report\n"))
    async with client(mode="remote") as c:
        hit = await c.read_resource(f"review://octo/demo/{COMMIT}")
        fake.cached = None
        miss = await c.read_resource(f"review://octo/demo/{COMMIT}")
    assert "Cached report" in hit.contents[0].text
    assert "No cached review" in miss.contents[0].text
    assert all(call[0] == "lookup" for call in fake.calls)  # a resource never starts a review


@pytest.mark.anyio
async def test_job_resource(monkeypatch):
    FakeService(monkeypatch, cached=review_result([finding()], cached=True))
    async with client(mode="remote") as c:
        job = out(await c.call_tool("review_repo", {"url": "https://github.com/octo/demo"}))["job_id"]
        res = await c.read_resource(f"review://jobs/{job}")
    assert '"state":"done"' in res.contents[0].text


@pytest.mark.anyio
async def test_prompts(monkeypatch):
    async with client() as c:
        names = {p.name for p in (await c.list_prompts()).prompts}
        audit = await c.get_prompt("security_audit", {"url": "https://github.com/octo/demo"})
    assert names == {"review_my_changes", "security_audit", "explain_score"}
    assert "review_repo" in audit.messages[0].content.text


# --- regressions from the Phase 1 review ----------------------------------------------------


@pytest.mark.anyio
async def test_agent_filter_matches_bug_hunter(many):
    """Regression: agent names were Markdown-escaped (an escaped underscore), so the filter never matched."""
    async with client(mode="remote") as c:
        job = out(await c.call_tool("review_repo", {"url": "https://github.com/octo/demo"}))["job_id"]
        page = out(await c.call_tool("list_findings", {"job_id": job, "agent": "bug_hunter"}))
    assert [(f["id"], f["agent"]) for f in page["items"]] == [("S3", "bug_hunter")]


@pytest.mark.anyio
async def test_changes_findings_are_paged_by_severity(repo, monkeypatch):
    lows = [finding(f"L{i}", "low") for i in range(3)]
    FakeService(monkeypatch, changes=pr_review(in_diff=lows, outside=[finding("H1", "high", line=9)]))
    async with client(roots=[repo]) as c:
        job = out(await c.call_tool("review_changes", {}))["job_id"]
        page = out(await c.call_tool("list_findings", {"job_id": job, "limit": 1}))
    assert page["items"][0]["id"] == "H1"


@pytest.mark.anyio
async def test_quota_is_charged_for_fresh_reviews_only(monkeypatch):
    from mcp import Client

    from src.mcp_server.jobs import JobManager
    from src.mcp_server.server import build_server
    from src.mcp_server.tools import QuotaExceeded

    fake = FakeService(monkeypatch, cached=review_result([], cached=True))
    charged = []

    def charge(owner):
        charged.append(owner)
        raise QuotaExceeded

    jobs = JobManager()
    try:
        async with Client(build_server("remote", jobs=jobs, charge=charge)) as c:
            assert not (await c.call_tool("review_repo", {"url": "https://github.com/octo/demo"})).is_error
            assert charged == []  # cache hits are free
            fake.cached = None
            msg = error_text(await c.call_tool("review_repo", {"url": "https://github.com/octo/demo"}))
    finally:
        jobs.shutdown()
    assert "daily limit reached, resets at 00:00 UTC" in msg and len(charged) == 1
    assert not any(call[0] == "review" for call in fake.calls)


@pytest.mark.anyio
async def test_given_commit_is_pinned(monkeypatch):
    fake = FakeService(monkeypatch)
    sha = "b" * 40
    async with client(mode="remote") as c:
        out(await c.call_tool("review_repo", {"url": "https://github.com/octo/demo", "commit": sha,
                                              "wait_seconds": 5}))
    assert fake.calls[-1][0] == "review" and fake.calls[-1][2].commit == sha


@pytest.mark.anyio
async def test_resource_errors_keep_their_message(monkeypatch):
    from mcp.shared.exceptions import MCPError
    FakeService(monkeypatch)
    async with client(mode="remote") as c:
        with pytest.raises(MCPError, match="unknown or expired job id"):
            await c.read_resource("review://jobs/expired123")
        with pytest.raises(MCPError, match="40-character"):
            await c.read_resource("review://octo/demo/main")


@pytest.mark.anyio
async def test_repo_with_a_git_filter_is_refused_by_the_tool(repo, monkeypatch):
    import subprocess
    fake = FakeService(monkeypatch)
    subprocess.run(["git", "config", "filter.evil.clean", "sh -c 'touch PWNED; cat'"], cwd=repo, check=True)
    async with client(roots=[repo]) as c:
        msg = error_text(await c.call_tool("review_changes", {}))
    assert "refusing to review" in msg and fake.calls == [] and not (repo / "PWNED").exists()


def test_cli_rejects_bad_and_unfinished_modes(monkeypatch, capsys):
    from src.mcp_server.__main__ import main
    monkeypatch.setattr(config, "MCP_MODE", "Admin")
    for argv in ([], ["--mode", "remote"], ["--transport", "http"]):
        with pytest.raises(SystemExit) as exc:
            main(argv if argv else None)
        assert exc.value.code == 2
    assert "must be one of" in capsys.readouterr().err
