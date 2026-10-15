"""Quality Reviewer agent: maintainability issues grounded in ruff evidence."""

from src.agents.base import BatchProgress, ChunkReview, review_chunks
from src.repo import Snapshot
from src.tools.static_scan import ToolFinding

QUALITY_TOOLS = {"ruff"}


def run(chunks: list[list[str]], snapshot: Snapshot, tool_hits: list[ToolFinding],
        on_batch: BatchProgress | None = None) -> ChunkReview:
    hits = [h for h in tool_hits if h.tool in QUALITY_TOOLS]
    return review_chunks("quality", "QUAL", chunks, snapshot, hits, on_batch)
