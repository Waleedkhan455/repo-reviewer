"""Shared builders for test data."""

from src.schemas import ArchitectOut, Plan, ReviewData, RunStatsOut, Score


def review_data(repo, findings, confirmed, seconds=10.0, calls=6):
    return ReviewData(
        run_id="r", repo=repo, commit="c" * 40, pipeline_version="1", model="m", verified=True,
        plan=Plan(languages=["py"], assignments={}), tool_hits=0, duplicates_dropped=[],
        invalid_items_dropped=0, raw_findings=findings, confirmed_ids=confirmed, rejected=[],
        architecture=ArchitectOut(summary="s"), score=Score(score=100, per_kloc=0, counts={}),
        stats=RunStatsOut(seconds=seconds, llm_calls=calls, llm_errors=0, llm_seconds=1.0), warnings=[],
    )
