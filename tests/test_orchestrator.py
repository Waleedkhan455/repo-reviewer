"""End-to-end pipeline with a local repo and a fake LLM (no network, no clone)."""

import json
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import config, repo
from src.orchestrator import ReviewError, ReviewOptions, dedupe, pack_chunks, rank_files, run_review
from src.repo import RepoFile, Snapshot, list_files
from src.schemas import Finding

FILES = {
    "README.md": "# Demo\nA tiny demo app.\n",
    "app.py": "from db import get_user\n\nprint(get_user(None, 'bob'))\n",
    "db.py": (
        "def get_user(conn, name):\n"
        "    query = \"SELECT * FROM users WHERE name = '\" + name + \"'\"\n"
        "    return conn.execute(query)\n"
    ),
    "tests/test_db.py": "def test_x():\n    assert True\n",
}


SHA = "a" * 40


@pytest.fixture
def local_repo(make_repo, monkeypatch, tmp_path):
    root = make_repo(FILES)
    clones = []

    @contextmanager
    def fake_clone(ref, commit=None):
        clones.append(commit)
        yield root

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(repo, "remote_head", lambda ref: SHA)

    monkeypatch.setattr(repo, "check_repo_size", lambda ref: {})
    monkeypatch.setattr(repo, "cloned_repo", fake_clone)
    monkeypatch.setattr(repo, "head_commit", lambda root: "abc123def4567890")
    return SimpleNamespace(root=root, clones=clones)


def _handlers(security_reply=None):
    def security(system, user):
        if security_reply is not None:
            return security_reply
        return {"findings": [
            {"file": "db.py", "line": 2, "severity": "high", "issue": "SQL injection via string concatenation",
             "evidence": "query = \"SELECT * FROM users WHERE name = '\" + name + \"'\"",
             "fix": "Use a parameterized query"},
            {"file": "auth.py", "line": 10, "severity": "high", "issue": "hallucinated file",
             "evidence": "password = 'admin'", "fix": "n/a"},
        ]}

    def quality(system, user):
        return {"findings": [
            {"file": "./db.py", "line": 3, "severity": "low", "issue": "No error handling around execute",
             "evidence": "return conn.execute(query)", "fix": "Handle sqlite3.Error"},
        ]}

    def verifier(system, user):
        ids = re.findall(r"CLAIM (\S+)", user)
        return {"verdicts": [{"id": i, "verdict": "confirmed" if i.startswith("SEC") else "rejected",
                              "reason": "checked"} for i in ids]}

    return {
        "architect": lambda s, u: {"summary": "A demo app with a SQL helper.",
                                   "modules": [{"name": "db | data", "purpose": "queries"}],
                                   "entry_points": ["app.py"], "dependencies": []},
        "security": security,
        "quality": quality,
        "verifier": verifier,
        "writer": lambda s, u: {"overview": "Small app with one serious SQL injection.",
                                "top_fixes": [{"id": "SEC-1", "why": "Exploitable."},
                                              {"id": "SEC-404", "why": "unknown id is dropped"}]},
    }


def test_full_review(local_repo, fake_llm):
    calls = fake_llm(_handlers())
    result = run_review("https://github.com/demo/app")
    d = result.data.model_dump()
    assert d["repo"] == "demo/app"
    assert len(d["raw_findings"]) == 3
    assert d["confirmed_ids"] == ["SEC-1"]
    stages = {r["id"]: r["stage"] for r in d["rejected"]}
    assert stages == {"SEC-2": "precheck", "QUAL-1": "llm"}
    assert d["score"]["score"] == 90
    assert "tests/test_db.py" not in d["plan"]["assignments"]["quality"]
    # the hallucinated finding never reached the LLM verifier
    verifier_inputs = " ".join(u for a, u in calls if a == "verifier")
    assert "SEC-2" not in verifier_inputs
    md = result.report_md
    assert "## Score: 90 / 100" in md
    assert "SEC-1" in md and "SEC-404" not in md
    assert r"db \| data" in md
    assert "3 raw findings → **1 confirmed**, 2 rejected (1 by plain-Python pre-check, 1 by the LLM verifier" in md
    json.dumps(d)  # findings.json must be serializable


def test_no_verify_keeps_raw(local_repo, fake_llm):
    calls = fake_llm(_handlers())
    d = run_review("https://github.com/demo/app", ReviewOptions(verify=False)).data.model_dump()
    assert len(d["confirmed_ids"]) == 3
    assert not any(a == "verifier" for a, _ in calls)


def test_all_agent_calls_failing_raises(local_repo, fake_llm):
    handlers = _handlers(security_reply="garbage")
    handlers["quality"] = lambda s, u: "garbage"
    fake_llm(handlers)
    with pytest.raises(ReviewError):
        run_review("https://github.com/demo/app")


def test_missing_key_fails_before_clone(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(repo, "check_repo_size", lambda ref: pytest.fail("should not reach GitHub"))
    from src.llm import LLMError
    with pytest.raises(LLMError):
        run_review("https://github.com/demo/app", ReviewOptions(use_cache=False))


def test_rank_prefers_entry_and_sensitive_files():
    files = [RepoFile(r, Path(r), 100) for r in ["utils/strings.py", "tests/test_auth.py", "auth/login.py", "main.py"]]
    ranked = [f.rel for f in rank_files(files, [])]
    assert ranked[0] == "main.py"
    assert ranked[1] == "auth/login.py"
    assert ranked[-1] == "tests/test_auth.py"


def test_pack_chunks_respects_limits(make_repo, monkeypatch):
    monkeypatch.setattr(config, "MAX_PROMPT_CHARS", 6_000)  # 1_000 usable per chunk after overhead
    monkeypatch.setattr(config, "MAX_CHUNKS_PER_AGENT", 2)
    root = make_repo({f"m{i}.py": "x = 1\n" * 60 for i in range(5)})
    snap = Snapshot(root, list_files(root))
    chunks, skipped = pack_chunks([f.rel for f in snap.files], snap)
    assert len(chunks) == 2
    assert sum(len(c) for c in chunks) + len(skipped) == 5
    assert skipped


def test_huge_single_file_is_truncated_not_lost(make_repo, monkeypatch):
    monkeypatch.setattr(config, "MAX_PROMPT_CHARS", 6_000)
    root = make_repo({"big.py": "y = 2\n" * 5_000})
    snap = Snapshot(root, list_files(root))
    chunks, skipped = pack_chunks(["big.py"], snap)
    assert chunks == [["big.py"]] and skipped == []


def _f(fid, source, line, evidence="x = eval(data)", issue="eval"):
    return Finding(id=fid, file="a.py", line=line, severity="high", issue=issue, evidence=evidence,
                   fix="f", source=source)


def test_dedupe_cross_agent_only():
    kept, dropped = dedupe([
        _f("SEC-1", "security", 10),
        _f("SEC-2", "security", 10, issue="hard-coded secret"),   # same agent, same line: kept
        _f("QUAL-1", "quality", 11, evidence="x  =  eval(data)"),  # other agent, ±1 line, same code: dropped
        _f("QUAL-2", "quality", 10, evidence="return conn.execute(q)"),  # different code: kept
    ])
    assert [f.id for f in kept] == ["SEC-1", "SEC-2", "QUAL-2"]
    assert dropped == ["QUAL-1"]


def test_truncated_and_masked_output(local_repo, fake_llm, make_repo):
    make_repo({"big.py": "".join(f"v{i} = {i}\n" for i in range(4000)),
               "keys.py": "AWS = 'AKIAABCDEFGHIJKLMNOP'\n"})
    fake_llm(_handlers(security_reply={"findings": [
        {"file": "keys.py", "line": 1, "severity": "high", "issue": "Hard-coded AWS key AKIAABCDEFGHIJKLMNOP",
         "evidence": "AWS = 'AKIAABCDEFGHIJKLMNOP'", "fix": "Use env vars"}]}))
    result = run_review("https://github.com/demo/app")
    assert "ABCDEFGHIJKLMNOP" not in result.data.model_dump_json()
    assert "ABCDEFGHIJKLMNOP" not in result.report_md
    assert "big.py" in result.data.plan.skipped["truncated"]
    assert any("partly reviewed (truncated): big.py" in w for w in result.data.warnings)
    assert "big.py" in result.report_md


def test_scanner_failure_is_reported(local_repo, fake_llm, monkeypatch):
    from src.tools.static_scan import ScanError

    def broken(root, targets=None):
        raise ScanError("bandit failed (exit 2)")

    monkeypatch.setattr("src.orchestrator.run_bandit", broken)
    fake_llm(_handlers())
    result = run_review("https://github.com/demo/app")
    assert "Static analysis degraded: bandit failed (exit 2)" in result.data.warnings
    assert result.data.degraded


def test_cache_hit_skips_llm_and_pins_commit(local_repo, fake_llm):
    calls = fake_llm(_handlers())
    first = run_review("https://github.com/demo/app")
    n_calls = len(calls)
    second = run_review("https://github.com/demo/app")
    assert not first.cached and second.cached
    assert len(calls) == n_calls  # no LLM calls on a hit
    assert second.data == first.data and second.report_md == first.report_md
    assert local_repo.clones == [SHA]  # cloned once, pinned to the resolved HEAD
    third = run_review("https://github.com/demo/app", ReviewOptions(verify=False))
    assert not third.cached  # different options, different key


def test_bug_hunter_optional(local_repo, fake_llm):
    handlers = _handlers()
    handlers["bug_hunter"] = lambda s, u: {"findings": [
        {"file": "db.py", "line": 3, "severity": "medium", "issue": "conn may be None (app.py passes None)",
         "evidence": "return conn.execute(query)", "fix": "Require a connection"}]}
    calls = fake_llm(handlers)
    d = run_review("https://github.com/demo/app", ReviewOptions(bug_hunter=False, use_cache=False)).data
    assert "bug_hunter" not in d.plan.assignments and not any(a == "bug_hunter" for a, _ in calls)
    d = run_review("https://github.com/demo/app", ReviewOptions(bug_hunter=True, use_cache=False)).data
    assert d.bug_hunter and any(f.id == "BUG-1" for f in d.raw_findings)


def test_degraded_review_is_not_cached(local_repo, fake_llm):
    handlers = _handlers()
    handlers["quality"] = lambda s, u: "garbage"  # quality fails, security succeeds
    fake_llm(handlers)
    first = run_review("https://github.com/demo/app")
    assert first.data.degraded
    fake_llm(_handlers())
    second = run_review("https://github.com/demo/app")
    assert not second.cached and not second.data.degraded  # retried, not served from cache


def test_cache_write_failure_keeps_the_review(local_repo, fake_llm, monkeypatch):
    def broken(*args):
        raise PermissionError("locked by another reader")

    monkeypatch.setattr("src.cache.save", broken)
    fake_llm(_handlers())
    assert run_review("https://github.com/demo/app").data.confirmed_ids == ["SEC-1"]


def test_lookup_cache_reuses_commit(local_repo, fake_llm, monkeypatch):
    from src.orchestrator import lookup_cache

    fake_llm(_handlers())
    lookup = lookup_cache("https://github.com/demo/app")
    assert lookup.commit == SHA and lookup.hit is None
    monkeypatch.setattr(repo, "remote_head", lambda ref: pytest.fail("second ls-remote"))
    result = run_review("https://github.com/demo/app", ReviewOptions(commit=lookup.commit))
    assert lookup_cache("https://github.com/demo/app", ReviewOptions(commit=lookup.commit)).hit is not None
    assert not result.cached


def test_injected_markdown_is_inert(local_repo, fake_llm):
    payload = "See ![t](https://evil.example/t.png) and [Apply fix](https://evil.example/phish) <img src=x>"
    handlers = _handlers(security_reply={"findings": [
        {"file": "db.py", "line": 2, "severity": "high", "issue": payload,
         "evidence": "query = \"SELECT * FROM users WHERE name = '\" + name + \"'\"", "fix": payload}]})
    handlers["writer"] = lambda s, u: {"overview": payload, "top_fixes": [{"id": "SEC-1", "why": payload}]}
    fake_llm(handlers)
    md = run_review("https://github.com/demo/app").report_md
    assert "](https://" not in md and "![" not in md
    assert re.search(r"(?<!\\)<img", md) is None  # only the escaped form \<img remains


def test_batch_progress_reaches_notify(local_repo, fake_llm, monkeypatch):
    monkeypatch.setattr(config, "MAX_PROMPT_CHARS", 5_400)  # 400 chars per batch -> one file per batch
    fake_llm(_handlers())
    messages = []
    result = run_review("https://github.com/demo/app", ReviewOptions(use_cache=False), progress=messages.append)
    batch_msgs = [m for m in messages if m.startswith("Security: batch")]
    assert batch_msgs and batch_msgs[-1].startswith(f"Security: batch {len(batch_msgs)}/{len(batch_msgs)}")
    assert "Security agent finished" in messages
    assert "2 of 2 files reviewed" in result.report_md


def test_javascript_repo_end_to_end(make_repo, monkeypatch, fake_llm, tmp_path):
    """A JS-only repo is accepted, Semgrep evidence reaches the Security agent, all agents review JS."""
    from src.tools.static_scan import ToolFinding

    root = make_repo({
        "package.json": '{"name": "demo"}',
        "server.js": "const app = require('express')();\napp.get('/e', (req, res) => res.send(eval(req.query.c)));\n",
        "lib/util.js": "module.exports = (a) => a + 1;\n",
        "types/index.d.ts": "export declare const x: number;\n",
        "server.test.js": "test('x', () => {});\n",
    })

    @contextmanager
    def fake_clone(ref, commit=None):
        yield root

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(repo, "check_repo_size", lambda ref: {})
    monkeypatch.setattr(repo, "cloned_repo", fake_clone)
    monkeypatch.setattr(repo, "head_commit", lambda root: "f" * 40)
    semgrep_calls = []

    def fake_semgrep(root, targets, languages):
        semgrep_calls.append(languages)
        assert sorted(targets) == ["lib/util.js", "server.js", "server.test.js"]
        return [ToolFinding("semgrep", "eval_nodejs", "server.js", 2, "high", "eval of user input")]

    monkeypatch.setattr("src.orchestrator.run_semgrep", fake_semgrep)
    monkeypatch.setattr("src.orchestrator.run_bandit", lambda root: pytest.fail("bandit on a JS-only repo"))
    seen = {}

    def security(system, user):
        seen["security"] = user
        return {"findings": [{"file": "server.js", "line": 2, "severity": "high", "issue": "eval of request data",
                              "evidence": "app.get('/e', (req, res) => res.send(eval(req.query.c)));",
                              "fix": "Never eval user input"}]}

    def quality(system, user):
        seen["quality"] = user
        return {"findings": []}

    handlers = _handlers()
    handlers.update(security=security, quality=quality)
    handlers["verifier"] = lambda s, u: {"verdicts": [{"id": "SEC-1", "verdict": "confirmed", "reason": "ok"}]}
    fake_llm(handlers)
    d = run_review("https://github.com/demo/js", ReviewOptions(use_cache=False)).data
    assert semgrep_calls == [{"javascript"}]
    assert "[semgrep eval_nodejs high] server.js:2" in seen["security"]
    assert 'path="lib/util.js"' in seen["quality"]
    assert "index.d.ts" not in seen["quality"] and "server.test.js" not in seen["quality"]
    assert d.plan.languages == ["javascript"]
    assert "package.json" in d.plan.assignments["architect"]
    assert d.confirmed_ids == ["SEC-1"]


@pytest.mark.parametrize("rel, is_test", [
    ("tests/test_x.py", True), ("app/x_test.py", True), ("src/__tests__/a.js", True), ("a.spec.ts", True),
    ("web/a.test.tsx", True), ("pkg/db_test.go", True), ("src/app.ts", False), ("main.go", False),
    ("contest.py", False),
    # production code must not hide behind test-like names or folders
    ("spec/handlers.js", False), ("src/testing/auth.js", False), ("src/auth.spec.impl.js", False),
    ("src/test_utils_login.js", False), ("e2e/server.ts", False),
])
def test_is_test_across_languages(rel, is_test):
    from src.orchestrator import _is_test

    assert _is_test(rel) is is_test


def test_test_files_with_scanner_hits_reach_security(make_repo):
    from src.orchestrator import make_plan, plan_warnings
    from src.tools.static_scan import ToolFinding

    root = make_repo({
        "app.py": "x = 1\n",
        "tests/test_app.py": "def test_x():\n    assert True\n",
        "tests/handlers.py": "import os\nos.system(cmd)\n",  # real code hidden in a test folder
    })
    vendored = ["public/js/jquery.min.js"]
    snap = Snapshot(root, list_files(root), vendored)
    hits = [ToolFinding("regex", "UNSAFE-OSSYSTEM", "tests/handlers.py", 2, "medium", "os.system() call")]
    plan, _chunks = make_plan(snap, hits)
    assert "tests/handlers.py" in plan.assignments["security"]
    assert "tests/test_app.py" not in plan.assignments["security"]
    assert plan.skipped["tests"] == ["tests/test_app.py"]
    notes = " ".join(plan_warnings(plan))
    assert "public/js/jquery.min.js" in notes and "tests/test_app.py" in notes
