"""Local mode path confinement (src/mcp_server/roots.py)."""

import os
import subprocess
from pathlib import Path

import pytest

from src.mcp_server.roots import PathNotAllowed, file_uri_to_path, pick_roots, resolve_project


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    return path


@pytest.fixture
def layout(tmp_path):
    allowed = tmp_path / "allowed"
    project = _git_init(allowed / "project")
    (project / "src").mkdir()
    outside = _git_init(tmp_path / "outside")
    plain = allowed / "plain-folder"
    plain.mkdir()
    return {"root": allowed.resolve(), "project": project.resolve(), "outside": outside.resolve(), "plain": plain}


def test_valid_project_and_subfolder_resolve_to_the_work_tree(layout):
    roots = [layout["root"]]
    assert resolve_project(str(layout["project"]), roots) == layout["project"]
    assert resolve_project(str(layout["project"] / "src"), roots) == layout["project"]
    assert resolve_project("project", roots) == layout["project"]  # relative to the first root


@pytest.mark.parametrize("path", ["../outside", "project/../../outside"])
def test_dotdot_escapes_are_rejected(layout, path):
    with pytest.raises(PathNotAllowed, match="outside"):
        resolve_project(path, [layout["root"]])


def test_absolute_path_outside_roots_is_rejected(layout):
    with pytest.raises(PathNotAllowed, match="outside"):
        resolve_project(str(layout["outside"]), [layout["root"]])


def test_symlink_pointing_outside_is_rejected(layout):
    link = layout["root"] / "sneaky"
    try:
        os.symlink(layout["outside"], link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs extra privileges on this OS")
    with pytest.raises(PathNotAllowed, match="outside"):
        resolve_project(str(link), [layout["root"]])


def test_non_git_folder_is_rejected(layout):
    with pytest.raises(PathNotAllowed, match="not a git repository"):
        resolve_project(str(layout["plain"]), [layout["root"]])


def test_allowed_subfolder_of_a_larger_repo_is_rejected(tmp_path):
    big = _git_init(tmp_path / "big")
    allowed = big / "only-this"
    allowed.mkdir()
    with pytest.raises(PathNotAllowed, match="extends outside"):
        resolve_project(str(allowed), [allowed.resolve()])


@pytest.mark.parametrize("path", ["", "a\0b", "x" * 600])
def test_invalid_inputs(layout, path):
    with pytest.raises(PathNotAllowed, match="invalid"):
        resolve_project(path, [layout["root"]])


def test_missing_path_and_no_roots(layout):
    with pytest.raises(PathNotAllowed, match="does not exist"):
        resolve_project(str(layout["root"] / "nope"), [layout["root"]])
    with pytest.raises(PathNotAllowed, match="no allowed folders"):
        resolve_project(".", [])


def test_root_sources_in_priority_order(tmp_path):
    a, b, c = (tmp_path / n for n in "abc")
    for p in (a, b, c):
        p.mkdir()
    assert pick_roots([a], [], c) == [a.resolve()]  # client roots win
    assert pick_roots([], [str(b)], c) == [b.resolve()]  # then RR_MCP_ROOTS
    assert pick_roots([], [], c) == [c.resolve()]  # then the start folder
    assert pick_roots([tmp_path / "missing"], [], c) == []  # a missing root allows nothing


def test_file_uris(tmp_path):
    assert file_uri_to_path(tmp_path.resolve().as_uri()) == tmp_path.resolve()
    assert file_uri_to_path("https://example.com/x") is None
    assert file_uri_to_path("file://evil-host/share") is None


@pytest.mark.parametrize("path", [r"\\attacker.example\share", "//attacker.example/share", r"\\?\C:\x",
                                  r"\\.\pipe\x"])
def test_network_and_device_paths_are_refused_before_any_filesystem_call(layout, path, monkeypatch):
    """Security regression: resolving a UNC path makes Windows contact the host with the user's credentials."""
    def no_fs(*args, **kwargs):
        raise AssertionError("filesystem touched for a network path")

    monkeypatch.setattr(Path, "resolve", no_fs)
    with pytest.raises(PathNotAllowed, match="network"):
        resolve_project(path, [layout["root"]])


def test_configured_roots_are_a_ceiling_for_client_roots(layout, tmp_path):
    inner = layout["project"]
    roots = pick_roots([inner, layout["outside"]], [str(layout["root"])], tmp_path)
    assert roots == [inner]  # the client root outside RR_MCP_ROOTS is ignored
    assert pick_roots([layout["outside"]], [str(layout["root"])], tmp_path) == []  # nothing left: nothing allowed


def test_relative_paths_start_from_the_server_folder_when_allowed(layout, tmp_path):
    assert resolve_project(".", [layout["root"]], cwd=layout["project"] / "src") == layout["project"]
    with pytest.raises(PathNotAllowed):  # cwd outside the roots: relative to the first root instead
        resolve_project(".", [layout["root"]], cwd=tmp_path)


def test_percent_signs_in_file_uris_are_decoded_once(tmp_path):
    folder = tmp_path / "a%41"
    folder.mkdir()
    assert file_uri_to_path(folder.as_uri()) == folder
