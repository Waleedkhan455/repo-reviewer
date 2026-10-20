"""The public service API used by the CLI, web app, PR bot, evals and a future MCP server."""

from src import changes, diffs, orchestrator, service
from src.orchestrator import CacheLookup, ReviewOptions


def test_review_repo_delegates_to_the_pipeline(monkeypatch):
    calls = []
    monkeypatch.setattr(orchestrator, "run_review",
                        lambda url, opts, progress: calls.append((url, opts, progress)) or "R")
    opts = ReviewOptions(verify=False)
    assert service.review_repo("https://github.com/a/b", opts, print) == "R"
    assert calls == [("https://github.com/a/b", opts, print)]


def test_cached_report_helpers(monkeypatch):
    lookup = CacheLookup("c" * 40, "key", hit="cached")
    monkeypatch.setattr(orchestrator, "lookup_cache", lambda url, opts=None: lookup)
    assert service.find_cached_review("https://github.com/a/b") is lookup
    assert service.load_cached_report("https://github.com/a/b") == "cached"
    monkeypatch.setattr(orchestrator, "lookup_cache", lambda url, opts=None: CacheLookup("c" * 40, "k", None))
    assert service.load_cached_report("https://github.com/a/b") is None


def test_review_changes_diffs_then_reviews(monkeypatch, tmp_path):
    monkeypatch.setattr(diffs, "changed_lines", lambda root, base, head: {"a.py": {3}})
    seen = {}

    def fake_review(root, changed, *, bug_hunter, progress):
        seen.update(root=root, changed=changed, bug_hunter=bug_hunter, progress=progress)
        return "PR"

    def callback(msg):
        return None

    monkeypatch.setattr(changes, "review_changed_lines", fake_review)
    assert service.review_changes(tmp_path, "a" * 40, "b" * 40, bug_hunter=True, progress=callback) == "PR"
    assert seen == {"root": tmp_path, "changed": {"a.py": {3}}, "bug_hunter": True, "progress": callback}


def test_errors_are_exported_for_callers():
    from src.diffs import DiffError
    from src.llm import LLMError
    from src.orchestrator import ReviewError
    from src.repo import RepoError

    assert (service.DiffError, service.LLMError, service.ReviewError, service.RepoError) == (
        DiffError, LLMError, ReviewError, RepoError)
