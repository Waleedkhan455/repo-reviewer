"""Minimal GitHub REST client for the pull-request bot (standard library only)."""

import json
import re
import urllib.error
import urllib.request

from src import config

_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
API = "https://api.github.com"


class GitHubError(Exception):
    """A GitHub API call failed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubClient:
    def __init__(self, token: str, repo: str) -> None:
        if not _REPO.fullmatch(repo):
            raise GitHubError(f"invalid repository name: {repo!r}")
        self._token = token
        self.repo = repo

    def _request(self, method: str, path: str, body: dict | None = None) -> object:
        req = urllib.request.Request(  # noqa: S310 - fixed https host, validated repo name
            f"{API}/repos/{self.repo}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "repo-reviewer",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=config.GITHUB_API_TIMEOUT_S) as resp:  # noqa: S310 - fixed host
                raw = resp.read()
        except urllib.error.HTTPError as exc:  # never include the token in the message
            raise GitHubError(f"GitHub API {method} {path} failed with {exc.code}", exc.code) from exc
        except OSError as exc:
            raise GitHubError(f"GitHub API {method} {path} unreachable") from exc
        return json.loads(raw) if raw else None

    def _paged(self, path: str, max_pages: int = 10) -> list[dict]:
        items: list[dict] = []
        for page in range(1, max_pages + 1):
            batch = self._request("GET", f"{path}?per_page=100&page={page}")
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    def review_comments(self, pr: int) -> list[dict]:
        """Existing inline (diff) comments on the PR."""
        return self._paged(f"/pulls/{pr}/comments")

    def issue_comments(self, pr: int) -> list[dict]:
        """Existing conversation comments on the PR."""
        return self._paged(f"/issues/{pr}/comments")

    def create_review(self, pr: int, commit: str, body: str, comments: list[dict]) -> None:
        """One review holding all inline comments (a single notification for the PR author)."""
        self._request("POST", f"/pulls/{pr}/reviews",
                      {"commit_id": commit, "event": "COMMENT", "body": body, "comments": comments})

    def create_issue_comment(self, pr: int, body: str) -> None:
        self._request("POST", f"/issues/{pr}/comments", {"body": body})

    def update_issue_comment(self, comment_id: int, body: str) -> None:
        self._request("PATCH", f"/issues/comments/{comment_id}", {"body": body})
