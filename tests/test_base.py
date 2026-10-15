import pytest

from src.agents.base import AgentError, extract_json, run_agent
from src.schemas import ArchitectOut

VALID = '{"summary": "A tool.", "modules": [], "entry_points": [], "dependencies": []}'


@pytest.mark.parametrize("text", [
    VALID,
    f"```json\n{VALID}\n```",
    f"Here you go:\n{VALID}\nHope that helps!",
])
def test_extract_json(text):
    assert extract_json(text)["summary"] == "A tool."


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2]"])
def test_extract_json_rejects(text):
    with pytest.raises(ValueError):
        extract_json(text)


def test_repair_retry(fake_llm):
    replies = iter(['{"oops": ', VALID])
    calls = fake_llm({"architect": lambda s, u: next(replies)})
    out = run_agent("architect", "sys", "user", ArchitectOut)
    assert out.summary == "A tool."
    assert len(calls) == 2
    assert "previous reply was not valid" in calls[1][1]


def test_schema_violation_triggers_repair(fake_llm):
    replies = iter(['{"modules": []}', VALID])
    fake_llm({"architect": lambda s, u: next(replies)})
    assert run_agent("architect", "sys", "user", ArchitectOut).summary == "A tool."


def test_two_invalid_replies_raise(fake_llm):
    calls = fake_llm({"architect": lambda s, u: "nope"})
    with pytest.raises(AgentError):
        run_agent("architect", "sys", "user", ArchitectOut)
    assert len(calls) == 2  # exactly one repair attempt


def test_one_bad_finding_does_not_sink_chunk(fake_llm, make_repo):
    from src.agents.base import review_chunks
    from src.repo import Snapshot, list_files

    root = make_repo({"a.py": "x = eval(data)\n", "Dockerfile": "FROM python\n"})
    snap = Snapshot(root, list_files(root))
    fake_llm({"security": lambda s, u: {"findings": [
        {"file": "a.py", "line": 1, "severity": "Critical", "issue": "eval", "evidence": "x = eval(data)", "fix": "no"},
        {"file": "a.py", "line": "not a number", "severity": "high", "issue": "bad", "evidence": "e", "fix": "f"},
        {"file": "Dockerfile", "line": None, "severity": "moderate", "issue": "runs as root",
         "evidence": "FROM python", "fix": "add USER"},
    ]}})
    out = review_chunks("security", "SEC", [["a.py", "Dockerfile"]], snap, [])
    assert [(f.id, f.severity, f.line) for f in out.findings] == [("SEC-1", "high", 1), ("SEC-2", "medium", 1)]
    assert out.invalid_items == 1
    assert out.reviewed_files == {"a.py", "Dockerfile"}


def test_failed_chunk_not_counted_as_reviewed(fake_llm, make_repo):
    from src.agents.base import review_chunks
    from src.repo import Snapshot, list_files

    root = make_repo({"a.py": "x = 1\n", "b.py": "y = 2\n"})
    snap = Snapshot(root, list_files(root))
    replies = iter(['{"findings": []}', "garbage", "garbage"])
    fake_llm({"quality": lambda s, u: next(replies)})
    out = review_chunks("quality", "QUAL", [["a.py"], ["b.py"]], snap, [])
    assert out.reviewed_files == {"a.py"}
    assert len(out.errors) == 1


def test_all_batches_reviewed_and_merged_with_progress(fake_llm, make_repo):
    from src.agents.base import review_chunks
    from src.repo import Snapshot, list_files

    root = make_repo({f"m{i}.py": f"x{i} = eval(data)\n" for i in range(5)})
    snap = Snapshot(root, list_files(root))

    def reply(system, user):
        rel = user.split('path="')[1].split('"')[0]
        n = rel[1]
        return {"findings": [{"file": rel, "line": 1, "severity": "high", "issue": "eval",
                              "evidence": f"x{n} = eval(data)", "fix": "no"}]}

    fake_llm({"security": reply})
    progress = []
    out = review_chunks("security", "SEC", [[f"m{i}.py"] for i in range(5)], snap, [],
                        lambda *a: progress.append(a))
    assert len(out.findings) == 5 and out.reviewed_files == {f"m{i}.py" for i in range(5)}
    assert [p[1:] for p in progress] == [(1, 5, 1), (2, 5, 2), (3, 5, 3), (4, 5, 4), (5, 5, 5)]
    assert [f.id for f in out.findings] == ["SEC-1", "SEC-2", "SEC-3", "SEC-4", "SEC-5"]


def test_extract_json_tolerates_raw_control_characters():
    """Regression: a raw tab/newline inside a JSON string used to fail a whole Security batch."""
    raw = '{"findings": [{"issue": "line one\nline two\twith tab", "evidence": "x"}]}'
    assert extract_json(raw)["findings"][0]["issue"] == "line one\nline two\twith tab"
