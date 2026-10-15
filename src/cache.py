"""File cache of finished reviews, keyed by repo + exact commit + pipeline settings.

A repo that has not changed is never reviewed twice, which saves free-tier quota.
"""

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from src import config
from src.repo import RepoRef
from src.schemas import ReviewData


def cache_key(ref: RepoRef, commit: str, *, verify: bool, bug_hunter: bool) -> str:
    """Changing the model, prompts (PIPELINE_VERSION) or options gives a new key, never a stale report."""
    raw = "|".join([ref.url, commit, config.PIPELINE_VERSION, config.MODEL_NAME,
                    f"verify={verify}", f"bug_hunter={bug_hunter}"])
    return hashlib.sha256(raw.encode()).hexdigest()


def _path(key: str) -> Path:
    return config.CACHE_DIR / f"{key}.json"


def load(key: str) -> tuple[str, ReviewData] | None:
    """Return (report_md, data) or None. Unreadable or outdated entries count as a miss."""
    try:
        entry = json.loads(_path(key).read_text(encoding="utf-8"))
        return entry["report_md"], ReviewData.model_validate(entry["data"])
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        return None


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:  # deleted by a concurrent prune
        return 0.0


def save(key: str, report_md: str, data: ReviewData) -> None:
    """Atomic write (temp file + rename), then prune the oldest entries beyond CACHE_MAX_ENTRIES."""
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"report_md": report_md, "data": data.model_dump(mode="json")}, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=config.CACHE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, _path(key))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)
    entries = sorted(config.CACHE_DIR.glob("*.json"), key=_mtime, reverse=True)
    for old in entries[config.CACHE_MAX_ENTRIES :]:
        with contextlib.suppress(OSError):
            old.unlink()
