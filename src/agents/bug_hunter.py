"""Bug Hunter agent (optional, Phase 2): logic and runtime bugs grounded in ruff's bug rules.

Off by default (RR_BUG_HUNTER=1 or --bug-hunter to enable). Keep it on only if the evals
show it improves recall without hurting precision.
"""

from src.agents.base import BatchProgress, ChunkReview, review_chunks
from src.repo import Snapshot
from src.tools.static_scan import ToolFinding

BUG_RULE_PREFIXES = ("B", "F", "E9", "E722")


def run(chunks: list[list[str]], snapshot: Snapshot, tool_hits: list[ToolFinding],
        on_batch: BatchProgress | None = None) -> ChunkReview:
    hits = [h for h in tool_hits if h.tool == "ruff" and h.rule.startswith(BUG_RULE_PREFIXES)]
    return review_chunks("bug_hunter", "BUG", chunks, snapshot, hits, on_batch)
