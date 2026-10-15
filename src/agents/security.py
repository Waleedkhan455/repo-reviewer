"""Security Auditor agent: vulnerabilities grounded in bandit, Semgrep and regex evidence."""

from src.agents.base import BatchProgress, ChunkReview, review_chunks
from src.repo import Snapshot
from src.tools.static_scan import ToolFinding

SECURITY_TOOLS = {"bandit", "regex", "semgrep"}


def run(chunks: list[list[str]], snapshot: Snapshot, tool_hits: list[ToolFinding],
        on_batch: BatchProgress | None = None) -> ChunkReview:
    hits = [h for h in tool_hits if h.tool in SECURITY_TOOLS]
    return review_chunks("security", "SEC", chunks, snapshot, hits, on_batch)
