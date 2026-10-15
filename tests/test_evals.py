from evals import run_evals
from evals.run_evals import EvalRepo, Label, compute_metrics, fingerprint, summary_table, update_readme
from src.schemas import Finding
from tests.factories import review_data as _data


def _f(fid, line):
    return Finding(id=fid, file="app.py", line=line, severity="high", issue=f"issue {fid}", evidence="e" * 10, fix="f")


def test_metrics_off_vs_on():
    f1, f2, f3, f4 = _f("SEC-1", 1), _f("SEC-2", 2), _f("SEC-3", 3), _f("SEC-4", 4)
    d = _data("a/b", [f1, f2, f3, f4], confirmed=["SEC-1", "SEC-2"])
    labels = {
        fingerprint(d, f1): Label(verdict="real", gt_id="GT-1"),
        fingerprint(d, f2): Label(verdict="real"),
        fingerprint(d, f3): Label(verdict="false"),
        fingerprint(d, f4): Label(verdict="skip"),
    }
    repos = [EvalRepo(url="https://github.com/a/b", commit="c" * 40, kind="vulnerable", ground_truth=[
        {"id": "GT-1", "file": "app.py", "category": "security", "description": "x"},
        {"id": "GT-2", "file": "app.py", "category": "bug", "description": "y"},
    ])]
    m = compute_metrics([d], labels, repos)
    assert m.off.precision() == 2 / 3  # skip excluded
    assert m.on.precision() == 1.0
    assert m.recall(m.off) == 0.5 and m.recall(m.on) == 0.5
    assert (m.raw, m.confirmed) == (4, 2)
    table = summary_table(m)
    assert "| No Verifier | 67% | 50% | 4 |" in table
    assert "| With Verifier | 100% | 50% | 2 |" in table


def test_no_labels_or_ground_truth_is_na():
    d = _data("a/b", [_f("SEC-1", 1)], confirmed=[])
    m = compute_metrics([d], {}, [])
    assert m.off.precision() is None and m.recall(m.off) is None
    assert "n/a" in summary_table(m)


def test_update_readme_between_markers(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(f"intro\n{run_evals.README_START}\nold\n{run_evals.README_END}\noutro\n", encoding="utf-8")
    assert update_readme("| new |", readme)
    assert readme.read_text(encoding="utf-8") == (
        f"intro\n{run_evals.README_START}\n| new |\n{run_evals.README_END}\noutro\n")
    assert not update_readme("x", tmp_path / "missing.md")


def test_repos_file_is_valid_and_pinned():
    repos = run_evals.load_repos()
    assert len(repos) == 10
    assert all(len(r.commit) == 40 for r in repos)
    assert len({r.slug for r in repos}) == 10


def test_label_session_is_blind_and_resumable(monkeypatch, tmp_path):
    f1, f2 = _f("SEC-1", 1), _f("SEC-2", 2)
    d = _data("a/b", [f1, f2], confirmed=["SEC-1"])
    monkeypatch.setattr(run_evals, "load_runs", lambda tag: [d])
    monkeypatch.setattr(run_evals, "load_repos", lambda: [])
    monkeypatch.setattr(run_evals, "LABELS_FILE", tmp_path / "labels.json")
    monkeypatch.setattr(run_evals, "save_labels", lambda labels: run_evals.LABELS_FILE.write_text(
        __import__("json").dumps({k: v.model_dump() for k, v in labels.items()})))
    monkeypatch.setattr(run_evals, "load_labels", lambda: {})
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    answers = iter(["x", "r", "q"])  # invalid answer is re-asked
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    run_evals.cmd_label("default")
    out = "\n".join(printed)
    assert "confirmed" not in out.lower() and "rejected" not in out.lower()  # blind
    saved = __import__("json").loads(run_evals.LABELS_FILE.read_text())
    assert list(saved.values()) == [{"verdict": "real", "gt_id": None}]


def test_dedupe_is_not_credited_to_the_verifier():
    a = Finding(id="SEC-1", file="app.py", line=5, severity="high", issue="eval", evidence="x = eval(data)",
                fix="f", source="security")
    b = a.model_copy(update={"id": "QUAL-1", "source": "quality"})  # cross-agent duplicate
    d = _data("a/b", [a, b], confirmed=["SEC-1"]).model_copy(update={"duplicates_dropped": ["QUAL-1"]})
    labels = {fingerprint(d, a): Label(verdict="false"), fingerprint(d, b): Label(verdict="false")}
    m = compute_metrics([d], labels, [])
    assert (m.raw, m.confirmed) == (1, 1)  # OFF also dedupes, like a real --no-verify run
    assert m.off.precision() == m.on.precision() == 0.0
    assert m.rejection_rate() == 0.0


def test_off_cost_excludes_verifier_stage():
    d = _data("a/b", [], confirmed=[], seconds=100.0, calls=20)
    d = d.model_copy(update={"stats": d.stats.model_copy(update={"verifier_seconds": 30.0, "verifier_calls": 5})})
    m = compute_metrics([d], {}, [])
    assert (m.avg_seconds_off, m.avg_calls_off) == (70.0, 15.0)
    assert (m.avg_seconds_on, m.avg_calls_on) == (100.0, 20.0)


def test_bug_hunter_runs_never_overwrite_baseline(monkeypatch):
    seen = {}
    monkeypatch.setattr(run_evals, "cmd_run", lambda tag, bug, only: seen.setdefault("tag", tag) and 0)
    monkeypatch.setattr(run_evals, "cmd_score", lambda tag: 0)
    run_evals.main(["run", "--bug-hunter"])
    assert seen["tag"] == "bughunter"
