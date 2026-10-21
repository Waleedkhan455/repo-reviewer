"""Uncommitted changes in a local checkout (diffs.worktree_changed_lines), on real temporary git repos."""

import os
import subprocess

import pytest

from src import config, service
from src.diffs import DiffError, worktree_changed_lines


def _git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("a = 1\nb = 2\nc = 3\n")
    (root / "old.py").write_text("x = 1\n")
    (root / "keep.py").write_text("k = 1\n")
    (root / ".gitignore").write_text("ignored.log\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def test_empty_diff(repo):
    assert worktree_changed_lines(repo) == {}


def test_modified_staged_and_unstaged(repo):
    (repo / "app.py").write_text("a = 1\nb = 20\nc = 3\nd = 4\n")  # unstaged
    (repo / "keep.py").write_text("k = 2\n")
    _git(repo, "add", "keep.py")  # staged
    assert worktree_changed_lines(repo) == {"app.py": {2, 4}, "keep.py": {1}}


def test_untracked_files_count_fully_and_respect_gitignore(repo):
    (repo / "new.py").write_text("print(1)\nprint(2)\n")
    (repo / "ignored.log").write_text("noise\n")
    assert worktree_changed_lines(repo) == {"new.py": {1, 2}}
    assert worktree_changed_lines(repo, include_untracked=False) == {}


def test_deleted_and_binary_files_are_skipped(repo):
    (repo / "old.py").unlink()
    (repo / "data.bin").write_bytes(b"\x00\x01\x02binary")
    assert worktree_changed_lines(repo) == {}


def test_rename_reports_new_path(repo):
    body = "".join(f"v{i} = {i}\n" for i in range(10))
    (repo / "old.py").write_text(body)
    _git(repo, "commit", "-q", "-am", "longer file, so git can detect the rename (>= 50% similar)")
    _git(repo, "mv", "old.py", "renamed.py")
    (repo / "renamed.py").write_text(body + "extra = 1\n")
    assert worktree_changed_lines(repo) == {"renamed.py": {11}}


def test_too_large_change_is_refused(repo, monkeypatch):
    monkeypatch.setattr(config, "PR_MAX_CHANGED_LINES", 5)
    (repo / "big.py").write_text("z = 0\n" * 10)
    with pytest.raises(DiffError, match="too large"):
        worktree_changed_lines(repo)


def test_other_base_and_repo_without_commits(repo, tmp_path):
    (repo / "app.py").write_text("a = 1\nb = 2\nc = 3\ne = 5\n")
    _git(repo, "commit", "-q", "-am", "second")
    assert worktree_changed_lines(repo, base="HEAD~1") == {"app.py": {4}}
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _git(fresh, "init", "-q")
    (fresh / "first.py").write_text("one = 1\n")
    assert worktree_changed_lines(fresh) == {"first.py": {1}}  # no commits yet: everything is new


@pytest.mark.parametrize("base", ["--output=/tmp/x", "-p", "HEAD..main", "a b", "", "x" * 200, "main;rm"])
def test_unsafe_or_bad_refs_are_refused(repo, base):
    with pytest.raises(DiffError):
        worktree_changed_lines(repo, base=base)


def test_unknown_ref(repo):
    with pytest.raises(DiffError, match="unknown git ref"):
        worktree_changed_lines(repo, base="no-such-branch")


def test_service_review_worktree_wires_diff_into_review(repo, monkeypatch):
    (repo / "new.py").write_text("print(1)\n")
    seen = {}

    def fake(root, changed, *, bug_hunter, progress):
        seen.update(root=root, changed=changed, bug_hunter=bug_hunter)
        return "REVIEW"

    monkeypatch.setattr(service.changes, "review_changed_lines", fake)
    assert service.review_worktree(repo, bug_hunter=True) == "REVIEW"
    assert seen == {"root": repo, "changed": {"new.py": {1}}, "bug_hunter": True}


def test_repo_config_cannot_run_commands_during_the_diff(repo):
    """Security regression: a repo's own .git/config can set core.fsmonitor to a command that a plain
    `git diff` executes (a downloaded repo with its .git folder). Our git calls switch it off."""
    marker = repo / "PWNED"
    hook = repo / "hook.sh"
    hook.write_text(f'#!/bin/sh\necho pwned > "{marker.as_posix()}"\n')
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", hook.as_posix())
    (repo / "app.py").write_text("a = 1\nb = 22\nc = 3\n")
    subprocess.run(["git", "diff", "--stat"], cwd=repo, capture_output=True)  # unprotected, for comparison
    if not marker.exists():
        pytest.skip("this git/OS does not run fsmonitor hooks, so the attack cannot be demonstrated here")
    marker.unlink()
    with pytest.raises(DiffError, match=r"core.fsmonitor"):  # refused up front ...
        worktree_changed_lines(repo)
    from src.diffs import _git as safe_git
    safe_git(repo, ["diff", "--stat"])  # ... and our git calls switch it off anyway
    assert not marker.exists()


def test_repo_clean_filter_cannot_run_during_the_diff(repo):
    """Security regression: comparing the working tree with a commit runs the repo's clean filter
    (.gitattributes `filter=x` + .git/config `filter.x.clean`), which `--no-textconv` does not stop."""
    marker = repo / "PWNED"
    (repo / ".gitattributes").write_text("*.py filter=evil\n")
    _git(repo, "config", "filter.evil.clean", f"sh -c 'echo pwned > \"{marker.as_posix()}\"; cat'")
    (repo / "app.py").write_text("a = 1\nb = 22\nc = 3\n")
    subprocess.run(["git", "diff", "--stat"], cwd=repo, capture_output=True)  # unprotected, for comparison
    if not marker.exists():
        pytest.skip("this git/OS did not run the filter, so the attack cannot be demonstrated here")
    marker.unlink()
    with pytest.raises(DiffError, match=r"filter.evil.clean"):
        worktree_changed_lines(repo)
    assert not marker.exists()


@pytest.mark.parametrize("key, value", [("diff.x.textconv", "sh"), ("core.sshCommand", "sh"),
                                        ("include.path", "extra.cfg")])
def test_repo_config_that_can_run_programs_is_refused(repo, key, value):
    if key == "include.path":  # settings pulled in through an include count too
        (repo / ".git" / "extra.cfg").write_text("[filter \"x\"]\n\tclean = sh\n")
    _git(repo, "config", key, value)
    (repo / "app.py").write_text("a = 1\nb = 22\nc = 3\n")
    with pytest.raises(DiffError, match="refusing to review"):
        worktree_changed_lines(repo)


def test_tracked_file_turned_binary_is_skipped(repo):
    (repo / "app.py").write_bytes(b"a = 1\x00\x01\nb = 2\n")
    assert "app.py" not in worktree_changed_lines(repo)


def test_untracked_symlink_is_skipped(repo, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n")
    try:
        os.symlink(outside, repo / "link.py")
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs extra privileges on this OS")
    assert "link.py" not in worktree_changed_lines(repo)


def test_oversized_untracked_file_is_listed_not_dropped(repo, monkeypatch):
    monkeypatch.setattr(config, "MAX_FILE_BYTES", 50)
    (repo / "big.py").write_text("x = 1\n" * 20)
    assert worktree_changed_lines(repo)["big.py"] == {1}  # kept, so the review reports it as not reviewed


def test_untracked_files_count_toward_the_size_limit(repo, monkeypatch):
    monkeypatch.setattr(config, "PR_MAX_CHANGED_LINES", 10)
    (repo / "new.py").write_text("x = 1\n" * 11)
    with pytest.raises(DiffError, match="too large"):
        worktree_changed_lines(repo)
