"""Evaluation harness: does the Verifier measurably improve precision?

    python -m evals.run_evals                 # run all repos, then score (the one-command eval)
    python -m evals.run_evals run   [--tag T] [--bug-hunter] [--only owner/repo]
    python -m evals.run_evals label [--tag T] # blind, interactive labeling of every raw finding
    python -m evals.run_evals score [--tag T] # writes evals/results.md and the README table

One review run gives both configurations: the Verifier only removes findings, so
Verifier OFF = all raw findings and Verifier ON = confirmed findings, labeled once.
"""

import argparse
import hashlib
import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from src import config
from src.llm import LLMError
from src.orchestrator import ReviewError, ReviewOptions, dedupe, run_review
from src.repo import RepoError, canonicalize
from src.schemas import Finding, ReviewData

REPOS_FILE = config.EVALS_DIR / "repos.json"
RUNS_DIR = config.EVALS_DIR / "runs"
LABELS_FILE = config.EVALS_DIR / "labels.json"
RESULTS_FILE = config.EVALS_DIR / "results.md"
README = config.ROOT / "README.md"
README_START, README_END = "<!-- EVAL_RESULTS:START -->", "<!-- EVAL_RESULTS:END -->"


class GroundTruth(BaseModel):
    id: str
    file: str
    category: Literal["security", "quality", "bug"]
    description: str


class EvalRepo(BaseModel):
    url: str
    commit: str
    kind: str
    notes: str = ""
    ground_truth: list[GroundTruth] = Field(default_factory=list)

    @property
    def slug(self) -> str:
        return canonicalize(self.url).slug


class Label(BaseModel):
    verdict: Literal["real", "false", "skip"]
    gt_id: str | None = None


def load_repos(path: Path = REPOS_FILE) -> list[EvalRepo]:
    return [EvalRepo.model_validate(r) for r in json.loads(path.read_text(encoding="utf-8"))["repos"]]


def fingerprint(data: ReviewData, f: Finding) -> str:
    """Stable id for a finding across label sessions."""
    raw = f"{data.repo}|{data.commit}|{f.file}|{f.line}|{f.issue}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]  # noqa: S324 - an id, not security


def _run_file(tag: str, slug: str) -> Path:
    return RUNS_DIR / tag / f"{slug.replace('/', '__')}.json"


def load_runs(tag: str) -> list[ReviewData]:
    return [ReviewData.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted((RUNS_DIR / tag).glob("*.json"))]


def load_labels(path: Path = LABELS_FILE) -> dict[str, Label]:
    if not path.exists():
        return {}
    return {k: Label.model_validate(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}


def save_labels(labels: dict[str, Label], path: Path = LABELS_FILE) -> None:
    path.write_text(json.dumps({k: v.model_dump() for k, v in sorted(labels.items())}, indent=2), encoding="utf-8")


# --- run ---------------------------------------------------------------------


def cmd_run(tag: str, bug_hunter: bool, only: str | None) -> int:
    failures = 0
    for r in load_repos():
        if only and r.slug != only:
            continue
        print(f"=== {r.slug} @ {r.commit[:10]}", file=sys.stderr)
        try:
            result = run_review(r.url, ReviewOptions(verify=True, bug_hunter=bug_hunter, commit=r.commit),
                                progress=lambda msg: print(f"    ... {msg}", file=sys.stderr))
        except (RepoError, LLMError, ReviewError) as exc:
            print(f"    FAILED: {exc}", file=sys.stderr)
            failures += 1
            continue
        out = _run_file(tag, r.slug)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.data.model_dump_json(indent=2), encoding="utf-8")
        d = result.data
        print(f"    {len(d.raw_findings)} raw, {len(d.confirmed_ids)} confirmed, {d.stats.seconds}s"
              + (" (cached)" if result.cached else ""), file=sys.stderr)
    return 1 if failures else 0


# --- label -------------------------------------------------------------------


def _permalink(data: ReviewData, f: Finding) -> str:
    return f"https://github.com/{data.repo}/blob/{data.commit}/{f.file}#L{f.line}"


def _ask(prompt: str, allowed: set[str]) -> str:
    while True:
        answer = input(prompt).strip().lower()
        if answer in allowed:
            return answer


def cmd_label(tag: str) -> int:
    labels = load_labels()
    gt_by_repo = {r.slug: r.ground_truth for r in load_repos()}
    todo = [(d, f) for d in load_runs(tag) for f in d.raw_findings if fingerprint(d, f) not in labels]
    random.shuffle(todo)  # blind: no order hint, and the Verifier's verdict is never shown
    print(f"{len(todo)} findings to label. Rules: evals/LABELING_RULES.md. r=real f=false s=skip q=quit\n")
    for n, (d, f) in enumerate(todo, start=1):
        print(f"[{n}/{len(todo)}] {d.repo}  {f.severity.upper()}  {f.file}:{f.line}")
        print(f"  issue:    {f.issue}\n  evidence: {f.evidence}\n  code:     {_permalink(d, f)}")
        answer = _ask("  label [r/f/s/q]: ", {"r", "f", "s", "q"})
        if answer == "q":
            break
        label = Label(verdict={"r": "real", "f": "false", "s": "skip"}[answer])
        candidates = [g for g in gt_by_repo.get(d.repo, []) if g.file == f.file]
        if label.verdict == "real" and candidates:
            for g in candidates:
                print(f"    {g.id}: {g.description}")
            gid = _ask("  matches which ground-truth id? (blank = none): ", {"", *(g.id.lower() for g in candidates)})
            label.gt_id = next((g.id for g in candidates if g.id.lower() == gid), None)
        labels[fingerprint(d, f)] = label
        save_labels(labels)  # save after every answer: quitting never loses work
        print()
    return 0


# --- score -------------------------------------------------------------------


@dataclass
class ConfigStats:
    reported: int = 0
    labeled: int = 0
    real: int = 0
    matched_gt: set[str] = field(default_factory=set)

    def add(self, label: Label | None, repo: str) -> None:
        self.reported += 1
        if label is None or label.verdict == "skip":
            return
        self.labeled += 1
        if label.verdict == "real":
            self.real += 1
            if label.gt_id:
                self.matched_gt.add(f"{repo}:{label.gt_id}")

    def precision(self) -> float | None:
        return self.real / self.labeled if self.labeled else None


@dataclass
class Metrics:
    off: ConfigStats
    on: ConfigStats
    total_gt: int
    raw: int  # findings a --no-verify run would report (raw, after the same cross-agent dedupe)
    confirmed: int
    verifier_rejected: int
    avg_seconds_off: float  # run time minus the Verifier stage
    avg_seconds_on: float
    avg_calls_off: float
    avg_calls_on: float
    per_repo: list[dict]

    def rejection_rate(self) -> float | None:
        """Raw findings the Verifier rejected (duplicates are not counted as rejections)."""
        total = self.verifier_rejected + self.confirmed
        return self.verifier_rejected / total if total else None

    def recall(self, cfg: ConfigStats) -> float | None:
        return len(cfg.matched_gt) / self.total_gt if self.total_gt else None


def compute_metrics(runs: list[ReviewData], labels: dict[str, Label], repos: list[EvalRepo]) -> Metrics:
    gt_count = {r.slug: len(r.ground_truth) for r in repos}
    off, on = ConfigStats(), ConfigStats()
    per_repo = []
    for d in runs:
        confirmed = set(d.confirmed_ids)
        baseline, _ = dedupe(d.raw_findings)  # what a real --no-verify run reports
        r_off, r_on = ConfigStats(), ConfigStats()
        for f in baseline:
            label = labels.get(fingerprint(d, f))
            for cfg in (off, r_off):
                cfg.add(label, d.repo)
            if f.id in confirmed:
                for cfg in (on, r_on):
                    cfg.add(label, d.repo)
        per_repo.append({"repo": d.repo, "raw": r_off.reported, "confirmed": r_on.reported,
                         "p_off": r_off.precision(), "p_on": r_on.precision(),
                         "gt": gt_count.get(d.repo, 0), "found_off": len(r_off.matched_gt),
                         "found_on": len(r_on.matched_gt), "seconds": d.stats.seconds,
                         "llm_calls": d.stats.llm_calls})
    n = max(len(runs), 1)

    def avg(values: list[float]) -> float:
        return round(sum(values) / n, 1)

    return Metrics(
        off=off, on=on,
        total_gt=sum(gt_count.get(d.repo, 0) for d in runs),
        raw=off.reported, confirmed=on.reported,
        verifier_rejected=sum(len(d.rejected) for d in runs),
        avg_seconds_off=avg([d.stats.seconds - d.stats.verifier_seconds for d in runs]),
        avg_seconds_on=avg([d.stats.seconds for d in runs]),
        avg_calls_off=avg([d.stats.llm_calls - d.stats.verifier_calls for d in runs]),
        avg_calls_on=avg([d.stats.llm_calls for d in runs]),
        per_repo=per_repo,
    )


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def summary_table(m: Metrics) -> str:
    return "\n".join([
        "| Config | Precision | Recall | Findings | Avg time | Avg LLM calls |",
        "|---|---|---|---|---|---|",
        (f"| No Verifier | {_pct(m.off.precision())} | {_pct(m.recall(m.off))} | {m.raw} | {m.avg_seconds_off}s | "
         f"{m.avg_calls_off} |"),
        (f"| With Verifier | {_pct(m.on.precision())} | {_pct(m.recall(m.on))} | {m.confirmed} | "
         f"{m.avg_seconds_on}s | {m.avg_calls_on} |"),
    ])


def render_results(m: Metrics, tag: str, repos_run: int) -> str:
    rejection = m.rejection_rate()
    unlabeled = m.off.reported - m.off.labeled
    lines = [
        f"# Eval Results ({tag})",
        "",
        (f"{repos_run} repos · model `{config.MODEL_NAME}` · pipeline v{config.PIPELINE_VERSION} · "
        f"rejection rate {_pct(rejection)} · {unlabeled} findings unlabeled or skipped (excluded from precision)"),
        "",
        summary_table(m),
        "",
        ("Precision = real / labeled findings. Recall = ground-truth issues found / ground-truth issues "
        f"({m.total_gt} total). Rules: [LABELING_RULES.md](LABELING_RULES.md)."),
        "",
        "## Per repo",
        "",
        "| Repo | Raw | Confirmed | Precision OFF | Precision ON | GT found OFF | GT found ON | Time | LLM calls |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in m.per_repo:
        gt = f"/{r['gt']}" if r["gt"] else " (no GT)"
        lines.append(f"| {r['repo']} | {r['raw']} | {r['confirmed']} | {_pct(r['p_off'])} | {_pct(r['p_on'])} | "
                     f"{r['found_off']}{gt} | {r['found_on']}{gt} | {r['seconds']}s | {r['llm_calls']} |")
    return "\n".join(lines) + "\n"


def update_readme(table: str, readme: Path = README) -> bool:
    """Replace the README block between the EVAL_RESULTS markers. Returns False if markers are missing."""
    if not readme.exists():
        return False
    text = readme.read_text(encoding="utf-8")
    if README_START not in text or README_END not in text:
        return False
    head, rest = text.split(README_START, 1)
    _, tail = rest.split(README_END, 1)
    readme.write_text(f"{head}{README_START}\n{table}\n{README_END}{tail}", encoding="utf-8")
    return True


def cmd_score(tag: str) -> int:
    runs = load_runs(tag)
    if not runs:
        print(f"No runs in {RUNS_DIR / tag}. Run `python -m evals.run_evals run` first.", file=sys.stderr)
        return 1
    m = compute_metrics(runs, load_labels(), load_repos())
    RESULTS_FILE.write_text(render_results(m, tag, len(runs)), encoding="utf-8")
    if m.off.labeled and tag == "default":
        update_readme(summary_table(m))
    print(summary_table(m))
    print(f"\nWrote {RESULTS_FILE}")
    if not m.off.labeled:
        print("No labels yet: precision is n/a. Run `python -m evals.run_evals label`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="all", choices=["all", "run", "label", "score"])
    parser.add_argument("--tag", help="name of this run set (default: 'default', or 'bughunter' with --bug-hunter)")
    parser.add_argument("--bug-hunter", action="store_true", help="enable the optional Bug Hunter agent")
    parser.add_argument("--only", help="run a single repo, e.g. stamparm/DSVW")
    args = parser.parse_args(argv)
    args.tag = args.tag or ("bughunter" if args.bug_hunter else "default")  # never overwrite the baseline
    if args.command == "label":
        return cmd_label(args.tag)
    if args.command == "score":
        return cmd_score(args.tag)
    status = cmd_run(args.tag, args.bug_hunter, args.only)
    if args.command == "all":
        status = cmd_score(args.tag) or status
    return status


if __name__ == "__main__":
    sys.exit(main())
