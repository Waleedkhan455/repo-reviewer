"""Deterministic score. Never calls the LLM."""

from collections import Counter

from src.schemas import Finding, Score

WEIGHTS = {"high": 10, "medium": 4, "low": 1}


def score(findings: list[Finding], loc: int) -> Score:
    counts = Counter(f.severity for f in findings)
    penalty = sum(WEIGHTS[sev] * n for sev, n in counts.items())
    return Score(
        score=max(0, 100 - penalty),
        per_kloc=round(len(findings) / max(loc / 1000, 1), 2),
        counts={sev: counts.get(sev, 0) for sev in WEIGHTS},
    )
