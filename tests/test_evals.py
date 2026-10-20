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


def test_recall_is_unmeasured_not_zero_without_labels():
    """Regression: with ground truth but no labels, results.md showed recall '0%' instead of 'n/a'."""
    d = _data("a/b", [_f("SEC-1", 1)], confirmed=["SEC-1"])
    repos = [EvalRepo(url="https://github.com/a/b", commit="c" * 40, kind="vulnerable", ground_truth=[
        {"id": "GT-1", "file": "app.py", "category": "security", "description": "x"}])]
    m = compute_metrics([d], {}, repos)
    assert m.recall(m.off) is None and m.recall(m.on) is None
    assert "| No Verifier | n/a | n/a |" in summary_table(m)
    assert "n/a (unlabeled)" in run_evals.render_results(m, "default", [d])
    labeled = compute_metrics([d], {fingerprint(d, d.raw_findings[0]): Label(verdict="false")}, repos)
    assert labeled.recall(labeled.off) == 0.0  # labeled but nothing matched: a real 0%


def test_results_report_the_models_that_actually_answered():
    """Regression: results.md printed the configured model, while every call fell back to another one."""
    d = _data("a/b", [], confirmed=[])
    old = run_evals.run_provenance([d])
    assert "pipeline v1" in old and "not recorded" in old
    d = d.model_copy(update={"stats": d.stats.model_copy(update={"models_used": {"lite": 9, "flash": 1}})})
    assert run_evals.run_provenance([d]) == "pipeline v1 · models that answered: `lite` (9 calls), `flash` (1 calls)"


def _gt_repo(slug, n):
    return EvalRepo(url=f"https://github.com/{slug}", commit="c" * 40, kind="vulnerable", ground_truth=[
        {"id": f"GT-{i}", "file": "app.py", "category": "security", "description": "x"} for i in range(1, n + 1)])


def test_verifier_dropping_every_labeled_true_positive_shows_a_real_drop():
    """Regression: ON recall was 'n/a' when the Verifier rejected all labeled findings."""
    d = _data("a/b", [_f("SEC-1", 1)], confirmed=[])
    labels = {fingerprint(d, d.raw_findings[0]): Label(verdict="real", gt_id="GT-1")}
    m = compute_metrics([d], labels, [_gt_repo("a/b", 2)])
    assert m.recall(m.off) == 0.5 and m.recall(m.on) == 0.0


def test_partial_labeling_counts_only_labeled_repos():
    """Regression: overall recall divided by ground truth of repos nobody had labeled yet."""
    labeled = _data("a/b", [_f("SEC-1", 1)], confirmed=["SEC-1"])
    unlabeled = _data("c/d", [_f("SEC-2", 2)], confirmed=["SEC-2"])
    labels = {fingerprint(labeled, labeled.raw_findings[0]): Label(verdict="real", gt_id="GT-1")}
    m = compute_metrics([labeled, unlabeled], labels, [_gt_repo("a/b", 1), _gt_repo("c/d", 9)])
    assert (m.measured_gt, m.total_gt) == (1, 10)
    assert m.recall(m.off) == 1.0
    assert "1 of 10 counted so far" in run_evals.render_results(m, "default", [labeled, unlabeled])


def test_repo_with_no_findings_is_a_measured_miss():
    """Regression: a GT repo where the tool found nothing showed 'n/a (unlabeled)' forever."""
    d = _data("c/d", [], confirmed=[])
    m = compute_metrics([d], {}, [_gt_repo("c/d", 3)])
    assert m.recall(m.off) == 0.0
    assert "0/3" in run_evals.render_results(m, "default", [d])


def test_provenance_flags_runs_without_model_records():
    new = _data("a/b", [], confirmed=[])
    new = new.model_copy(update={"stats": new.stats.model_copy(update={"models_used": {"lite": 6}})})
    old = _data("c/d", [], confirmed=[])
    assert run_evals.run_provenance([new, old]).endswith("`lite` (6 calls) (not recorded for 1 runs)")


def test_each_run_set_has_its_own_results_file():
    assert run_evals.results_file("default").name == "results.md"
    assert run_evals.results_file("quality-v2").name == "results-quality-v2.md"
