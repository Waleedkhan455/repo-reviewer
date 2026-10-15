import re

import pytest

from src.agents.verifier import evidence_near, precheck, verify
from src.repo import Snapshot, list_files
from src.schemas import Finding

CODE = "\n".join([
    "import sqlite3",                                   # 1
    "",                                                 # 2
    "def get_user(conn, name):",                        # 3
    "    query = \"SELECT * FROM users WHERE name = '\" + name + \"'\"",  # 4
    "    return conn.execute(query).fetchall()",        # 5
    "",                                                 # 6
    "def safe(conn, name):",                            # 7
    "\treturn conn.execute('SELECT 1 WHERE ?', (name,))",  # 8 (tab indented)
])
LINES = CODE.split("\n")


@pytest.mark.parametrize("line, evidence, ok", [
    (4, "query = \"SELECT * FROM users WHERE name = '\" + name + \"'\"", True),
    (5, "    return conn.execute(query).fetchall()", True),      # leading spaces differ
    (7, "return conn.execute(query).fetchall()", True),          # cited 2 lines off
    (8, "return   conn.execute('SELECT 1 WHERE ?',\n (name,))", True),  # whitespace / newline differences
    (5, "    5| return conn.execute(query).fetchall()", True),   # model kept the line-number prefix
    (1, "return conn.execute(query).fetchall()", False),         # too far from cited line
    (5, "conn.execute(user_input)", False),                      # invented code
    (5, "query", False),                                         # too short to prove anything
])
def test_evidence_near(line, evidence, ok):
    assert evidence_near(LINES, line, evidence) is ok


@pytest.fixture
def snap(make_repo):
    root = make_repo({"src/db.py": CODE + "\n"})
    return Snapshot(root, list_files(root))


def _finding(fid, file="src/db.py", line=4, evidence="query = \"SELECT * FROM users WHERE name = '\" + name"):
    return Finding(id=fid, file=file, line=line, severity="high", issue="SQL injection",
                   evidence=evidence, fix="parameterize")


def test_precheck_reasons(snap):
    assert precheck(_finding("A"), snap) is None
    assert "not found" in precheck(_finding("B", file="src/nope.py"), snap)
    assert "out of range" in precheck(_finding("C", line=0), snap)
    assert "out of range" in precheck(_finding("D", line=500), snap)
    assert "evidence" in precheck(_finding("E", evidence="os.system(cmd)"), snap)
    # path tricks are just unknown paths, never opened
    assert "not found" in precheck(_finding("F", file="../../.env"), snap)


def test_verify_accounts_for_every_finding(snap, fake_llm):
    findings = [
        _finding("SEC-1"),                                   # LLM confirms
        _finding("SEC-2", line=5, evidence="return conn.execute(query).fetchall()"),  # LLM rejects
        _finding("SEC-3", line=8, evidence="return conn.execute('SELECT 1 WHERE ?', (name,))"),  # LLM omits
        _finding("SEC-4", file="src/ghost.py"),              # precheck rejects
    ]

    def reply(system, user):
        ids = re.findall(r"CLAIM (\S+)", user)
        assert ids == ["SEC-1", "SEC-2", "SEC-3"]  # one batched call for the file
        return {"verdicts": [
            {"id": "SEC-1", "verdict": "CONFIRMED", "reason": "concatenation"},
            {"id": "SEC-2", "verdict": "rejected", "reason": "duplicate of SEC-1"},
            {"id": "SEC-99", "verdict": "confirmed", "reason": "unknown id is ignored"},
        ]}

    calls = fake_llm({"verifier": reply})
    confirmed, rejected = verify(findings, snap)
    assert [f.id for f in confirmed] == ["SEC-1"]
    stages = {r.finding.id: r.stage for r in rejected}
    assert stages == {"SEC-2": "llm", "SEC-3": "llm", "SEC-4": "precheck"}
    assert len(confirmed) + len(rejected) == len(findings)
    assert len(calls) == 1


def test_verify_llm_failure_marks_error(snap, fake_llm):
    fake_llm({"verifier": lambda s, u: "not json"})
    confirmed, rejected = verify([_finding("SEC-1")], snap)
    assert confirmed == []
    assert rejected[0].stage == "error"


def test_short_evidence_must_match_whole_line():
    lines = ["def f(s):", "    eval(s)", "    return s"]
    assert evidence_near(lines, 2, "eval(s)")          # 7 chars, equals the whole line
    assert not evidence_near(lines, 2, "s)")           # short fragment proves nothing
    assert not evidence_near(lines, 2, "eval(x)")


def test_invalid_verdict_does_not_sink_batch(snap, fake_llm):
    findings = [_finding("SEC-1"), _finding("SEC-2", line=5, evidence="return conn.execute(query).fetchall()")]
    fake_llm({"verifier": lambda s, u: {"verdicts": [
        {"id": "SEC-1", "verdict": "uncertain", "reason": "?"},
        {"id": "SEC-2", "verdict": "confirmed", "reason": "ok"},
    ]}})
    confirmed, rejected = verify(findings, snap)
    assert [f.id for f in confirmed] == ["SEC-2"]
    assert rejected[0].finding.id == "SEC-1" and rejected[0].stage == "llm"
