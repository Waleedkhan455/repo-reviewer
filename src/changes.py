"""Diff-aware review: run the normal pipeline on the files a change touched, keep what is on changed lines.

Used by the pull-request bot and by ``service.review_changes``. The change is attacker-controlled, so
the result says what could not be reviewed (``not_reviewed``), whether anything failed (``degraded``)
and which high-severity scanner hits the AI did not confirm (``scanner_flags``).
"""

from dataclasses import dataclass, field
from pathlib import Path

from src import config, llm, repo
from src.agents import verifier
from src.agents.base import visible_lines
from src.diffs import format_ranges
from src.logger import RunStats, current_run
from src.orchestrator import Progress, dedupe, make_plan, run_specialists, static_analysis
from src.repo import Snapshot, language_of
from src.schemas import SEVERITY_ORDER, Finding, Rejection, Score
from src.scoring import score
from src.tools.static_scan import ToolFinding

GATE_TOOLS = {"bandit", "semgrep", "regex"}  # deterministic evidence that counts for fail-on on its own


@dataclass
class PRReview:
    in_diff: list[Finding]  # confirmed, on changed lines (line = the diff line to comment on)
    outside: list[Finding]  # confirmed, but not on a changed line (summary only)
    rejected: list[Rejection]
    result_score: Score
    files: list[str]
    changed_line_count: int
    llm_calls: int
    warnings: list[str] = field(default_factory=list)
    scanner_flags: list[ToolFinding] = field(default_factory=list)  # high scanner hits the AI did not confirm
    not_reviewed: list[str] = field(default_factory=list)  # changed code that was not (fully) reviewed
    degraded: bool = False  # an agent, scanner or Verifier call failed

    @property
    def complete(self) -> bool:
        return not self.not_reviewed and not self.degraded


def _diff_line(f: Finding, changed: set[int]) -> int | None:
    """The changed line to attach a comment to: the cited line, or the nearest changed line close to it."""
    if f.line in changed:
        return f.line
    near = [n for n in changed if abs(n - f.line) <= config.PR_NEARBY_LINES]
    return min(near, key=lambda n: (abs(n - f.line), n)) if near else None


def _not_reviewed(changed: dict[str, set[int]], snapshot: Snapshot, reviewed: set[str]) -> list[str]:
    """Every changed source file the agents did not see in full, with the reason."""
    out = []
    for rel in sorted(changed):
        if not language_of(rel) or not changed[rel]:
            continue
        if rel in snapshot.vendored:
            out.append(f"{rel} (skipped as a bundled third-party asset)")
        elif not snapshot.has(rel):
            out.append(f"{rel} (skipped: over {config.MAX_FILE_BYTES // 1000} KB, binary or unsafe file name)")
        elif rel not in reviewed:
            out.append(f"{rel} (not reviewed: review batch failed or over the batch limit)")
        elif max(changed[rel]) > visible_lines(snapshot, rel):
            out.append(f"{rel} (changed lines beyond the part of this very large file the agents saw)")
    return out


def _scanner_flags(hits: list[ToolFinding], in_diff: list[Finding]) -> list[ToolFinding]:
    """High-severity scanner hits on changed lines that no confirmed finding covers."""
    def covered(h: ToolFinding) -> bool:
        return any(f.file == h.file and abs(f.line - h.line) <= config.PR_NEARBY_LINES for f in in_diff)

    flags = {(h.file, h.line): h for h in hits if h.tool in GATE_TOOLS and h.severity == "high" and not covered(h)}
    return sorted(flags.values(), key=lambda h: (h.file, h.line))


def review_changed_lines(root: Path, changed: dict[str, set[int]], *, bug_hunter: bool = False,
                         progress: Progress | None = None) -> PRReview:
    """Review the files in ``changed`` (path -> changed line numbers) and keep what is on changed lines."""
    notify = progress or (lambda _msg: None)
    llm.ensure_ready()
    stats = RunStats()
    token = current_run.set(stats)
    try:
        vendored: list[str] = []
        files = [f for f in repo.list_files(root, vendored) if f.rel in changed]
        snapshot = Snapshot(root, files, [v for v in vendored if v in changed])
        snapshot.focus = {f.rel: format_ranges(changed[f.rel]) for f in files}
        changed_count = sum(len(lines) for lines in changed.values())
        warnings: list[str] = []
        if ".gitattributes" in changed:
            warnings.append("This pull request changes .gitattributes; diffs are forced to text, but check it.")
        if not snapshot.code_files():
            return PRReview([], [], [], score([], 0), [f.rel for f in files], changed_count, 0, warnings,
                            not_reviewed=_not_reviewed(changed, snapshot, set()))

        notify(f"Static analysis of {len(files)} changed files")
        hits, scan_degraded = static_analysis(root, snapshot, warnings, scoped=True)
        hits = [h for h in hits if h.line in changed.get(h.file, set())]  # evidence about this change only
        plan, chunks = make_plan(snapshot, hits, with_bug_hunter=bug_hunter, include_tests=True)
        notify("Running agents: " + ", ".join(chunks).replace("_", " "))
        spec = run_specialists(snapshot, plan, chunks, hits, notify, include_architect=False)
        warnings += spec.warnings
        raw = spec.findings()
        notify(f"Verifying {len(raw)} findings")
        confirmed, rejected = verifier.verify(raw, snapshot)
        confirmed, _duplicates = dedupe(confirmed)

        in_diff, outside = [], []
        for f in sorted(confirmed, key=lambda f: (SEVERITY_ORDER[f.severity], f.file, f.line)):
            line = _diff_line(f, changed.get(f.file, set()))
            (in_diff if line else outside).append(f.model_copy(update={"line": line}) if line else f)
        return PRReview(
            in_diff, outside, rejected, score(in_diff, changed_count), [f.rel for f in files], changed_count,
            stats.llm_calls, warnings,
            scanner_flags=_scanner_flags(hits, in_diff),
            not_reviewed=_not_reviewed(changed, snapshot, spec.reviewed_files()),
            degraded=spec.degraded or scan_degraded or any(r.stage == "error" for r in rejected),
        )
    finally:
        current_run.reset(token)
