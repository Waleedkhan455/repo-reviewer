"""Public entry points of Repo Reviewer.

Every front end calls these functions: the CLI (``src/main.py``), the Streamlit app (``app.py``), the
pull-request bot (``src/pr_review.py``), the evals and a future MCP server. Nothing here adds behaviour;
it gives the pipeline one stable, documented surface so callers never reach into private helpers.

Errors: ``RepoError`` (bad URL, repo too large or unsupported), ``LLMError`` (no key or the model is
unavailable), ``ReviewError`` (every agent call failed) and ``DiffError`` (the change could not be diffed).
"""

from pathlib import Path

from src import changes, diffs, orchestrator
from src.changes import PRReview
from src.diffs import DiffError
from src.llm import LLMError
from src.orchestrator import CacheLookup, Progress, ReviewError, ReviewOptions, ReviewResult
from src.repo import RepoError

__all__ = [
    "CacheLookup", "DiffError", "LLMError", "PRReview", "RepoError", "ReviewError", "ReviewOptions",
    "ReviewResult", "find_cached_review", "load_cached_report", "review_changes", "review_repo",
]


def review_repo(url: str, options: ReviewOptions | None = None, progress: Progress | None = None) -> ReviewResult:
    """Review a public GitHub repository (``https://github.com/<owner>/<repo>``).

    Served from the cache when this exact commit was already reviewed with the same options.
    ``progress`` receives short status messages, always from the calling thread.
    """
    return orchestrator.run_review(url, options, progress)


def find_cached_review(url: str, options: ReviewOptions | None = None) -> CacheLookup:
    """Resolve the repository's current commit and look it up in the cache. Uses no LLM quota.

    Pass ``ReviewOptions(commit=lookup.commit)`` to ``review_repo`` afterwards so the commit is not
    resolved twice and the review lands under the same cache key.
    """
    return orchestrator.lookup_cache(url, options)


def load_cached_report(url: str, options: ReviewOptions | None = None) -> ReviewResult | None:
    """The cached review of the repository's current commit, or None if it has not been reviewed yet."""
    return find_cached_review(url, options).hit


def review_changes(root: Path, base: str, head: str, *, bug_hunter: bool = False,
                   progress: Progress | None = None) -> PRReview:
    """Review only the lines changed between ``base`` and ``head`` in a local git checkout at ``root``.

    ``base``/``head`` are commit SHAs; lines are taken from ``git diff base...head`` (what a pull
    request shows). The working tree should be checked out at ``head``.
    """
    changed = diffs.changed_lines(root, base, head)
    return changes.review_changed_lines(root, changed, bug_hunter=bug_hunter, progress=progress)
