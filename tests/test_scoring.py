from src.schemas import Finding
from src.scoring import score


def _f(sev):
    return Finding(file="a.py", line=1, severity=sev, issue="i", evidence="e", fix="f")


def test_formula():
    s = score([_f("high"), _f("medium"), _f("low"), _f("low")], loc=2000)
    assert s.score == 100 - (10 + 4 + 2)
    assert s.counts == {"high": 1, "medium": 1, "low": 2}
    assert s.per_kloc == 2.0


def test_floor_and_small_repo():
    s = score([_f("high")] * 11, loc=50)
    assert s.score == 0
    assert s.per_kloc == 11.0  # tiny repos count as 1 kLOC


def test_empty():
    assert score([], loc=0).score == 100


def test_severity_aliases_normalized():
    assert _f("CRITICAL").severity == "high"
    assert _f(" Info ").severity == "low"
