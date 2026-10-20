"""Where logs, the review cache and reports go (config._data_dir)."""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src import config


@pytest.fixture
def env(monkeypatch):
    values = {}
    monkeypatch.setattr(config, "_env", lambda name, default="": values.get(name, default))
    return values


def test_project_folder_used_when_writable(env, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    assert config._data_dir() == tmp_path
    assert not list(tmp_path.glob(".rr-write-test*"))  # probe cleaned up


def test_read_only_project_falls_back_to_private_temp(env, monkeypatch, tmp_path):
    blocker = tmp_path / "read-only"
    blocker.write_text("a file where the project folder should be")
    monkeypatch.setattr(config, "ROOT", blocker / "project")  # cannot be created
    monkeypatch.setattr(config.tempfile, "gettempdir", lambda: str(tmp_path))
    chosen = config._data_dir()
    assert chosen.parent == tmp_path and chosen.name.startswith("repo-reviewer-")


def test_configured_dir_is_relative_to_project_and_warns_when_unusable(env, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    env["RR_DATA_DIR"] = "data"
    assert config._data_dir() == tmp_path / "data"
    blocker = tmp_path / "file"
    blocker.write_text("x")
    env["RR_DATA_DIR"] = str(blocker / "sub")
    assert config._data_dir() == tmp_path  # falls back...
    assert "RR_DATA_DIR" in capsys.readouterr().err  # ...but says so


def test_concurrent_imports_agree_on_the_folder(env, monkeypatch, tmp_path):
    """Regression: a shared probe file name made concurrent processes pick different folders."""
    monkeypatch.setattr(config, "ROOT", tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: config._data_dir(), range(400)))
    assert set(results) == {tmp_path}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership/permission check")
def test_shared_temp_dir_planted_by_another_user_is_rejected(env, monkeypatch, tmp_path):
    monkeypatch.setattr(config.tempfile, "gettempdir", lambda: str(tmp_path))
    planted = tmp_path / f"repo-reviewer-{os.getuid()}"
    planted.mkdir(mode=0o777)
    os.chmod(planted, 0o777)  # group/world writable: someone else could have made it
    assert config._private_temp_dir() is None
    planted.rmdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    Path(planted).symlink_to(target, target_is_directory=True)
    assert config._private_temp_dir() is None
