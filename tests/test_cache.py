import pytest

from src import cache, config
from src.repo import canonicalize
from tests.factories import review_data


@pytest.fixture(autouse=True)
def _cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")


def test_roundtrip_and_miss():
    data = review_data("a/b", [], confirmed=[])
    assert cache.load("k1") is None
    cache.save("k1", "# report", data)
    md, loaded = cache.load("k1")
    assert md == "# report" and loaded == data


def test_corrupt_or_outdated_entry_is_a_miss():
    config.CACHE_DIR.mkdir(parents=True)
    (config.CACHE_DIR / "bad.json").write_text("{not json", encoding="utf-8")
    (config.CACHE_DIR / "old.json").write_text('{"report_md": "x", "data": {"repo": "a/b"}}', encoding="utf-8")
    assert cache.load("bad") is None
    assert cache.load("old") is None


def test_prune_keeps_newest(monkeypatch):
    monkeypatch.setattr(config, "CACHE_MAX_ENTRIES", 3)
    data = review_data("a/b", [], confirmed=[])
    for i in range(5):
        cache.save(f"k{i}", "r", data)
    assert len(list(config.CACHE_DIR.glob("*.json"))) == 3
    assert not list(config.CACHE_DIR.glob("*.tmp"))


def test_key_depends_on_everything_that_changes_the_output(monkeypatch):
    ref = canonicalize("https://github.com/Owner/Repo")
    base = cache.cache_key(ref, "a" * 40, verify=True, bug_hunter=False)
    assert base == cache.cache_key(canonicalize("https://github.com/owner/repo"), "a" * 40,
                                   verify=True, bug_hunter=False)  # case-insensitive, like GitHub
    assert base != cache.cache_key(ref, "b" * 40, verify=True, bug_hunter=False)
    assert base != cache.cache_key(ref, "a" * 40, verify=False, bug_hunter=False)
    assert base != cache.cache_key(ref, "a" * 40, verify=True, bug_hunter=True)
    monkeypatch.setattr(config, "MODEL_NAME", "other-model")
    assert base != cache.cache_key(ref, "a" * 40, verify=True, bug_hunter=False)
