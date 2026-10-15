"""Plan the review, run the specialist agents in parallel, verify, score and write the report."""

import re
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from queue import Empty, Queue

from src import cache, config, llm, repo
from src.agents import architect, bug_hunter, quality, security, verifier, writer
from src.agents.base import (
    NONCE_PLACEHOLDER,
    AgentError,
    ChunkReview,
    chunk_budget,
    file_block,
    is_truncated,
    load_prompt,
    new_nonce,
    run_agent,
)
from src.logger import RunStats, current_run, log_event, submit_in_context
from src.repo import LANGUAGE_EXTS, RepoFile, RepoRef, Snapshot, language_of
from src.schemas import (
    ArchitectOut,
    FilePick,
    Finding,
    Plan,
    RejectedOut,
    Rejection,
    ReviewData,
    RunMeta,
    RunStatsOut,
    Score,
)
from src.scoring import score
from src.tools.patterns import mask_secrets, scan_patterns
from src.tools.static_scan import ScanError, ToolFinding, run_bandit, run_ruff, run_semgrep


class ReviewError(Exception):
    """The review could not produce a trustworthy report."""


ENTRY_NAMES = {
    "main.py", "app.py", "__main__.py", "manage.py", "cli.py", "wsgi.py", "asgi.py", "server.py", "run.py", "api.py",
    "index.js", "server.js", "app.js", "main.js", "index.ts", "server.ts", "app.ts", "main.ts", "main.go",
}
META_NAMES = {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Dockerfile", "package.json",
              "tsconfig.json", "go.mod"}
SECURITY_WORDS = ("auth", "login", "passw", "token", "secret", "crypt", "db", "sql", "query", "api",
                  "route", "view", "upload", "shell", "exec", "admin", "session", "config", "settings",
                  "user", "payment")
REVIEW_EXTS = {*LANGUAGE_EXTS, ".yml", ".yaml", ".json", ".ini", ".cfg", ".toml", ".env", ".sh", ".conf"}
TEST_DIRS = {"test", "tests", "__tests__"}
# Name-based test detection only uses conventional patterns, so production code cannot hide from
# review by being named like a test (e.g. "auth.spec.impl.js", "test_utils_login.js").
TEST_NAME = re.compile(r"^test_.*\.py$|^conftest\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$")

Progress = Callable[[str], None]


# --- Planning ------------------------------------------------------------------


def _is_test(rel: str) -> bool:
    """Test files in Python (test_x.py, x_test.py), JS/TS (x.test.ts, x.spec.js, __tests__/) and Go (x_test.go)."""
    parts = PurePosixPath(rel).parts
    return any(p.lower() in TEST_DIRS for p in parts[:-1]) or bool(TEST_NAME.search(parts[-1].lower()))


def _is_reviewable(f: RepoFile) -> bool:
    name = PurePosixPath(f.rel).name
    if f.suffix in LANGUAGE_EXTS:
        return language_of(f.rel) is not None  # skips type declarations such as x.d.ts
    return f.suffix in REVIEW_EXTS or name == "Dockerfile" or name.startswith(".env")


def rank_files(files: list[RepoFile], hits: list[ToolFinding]) -> list[RepoFile]:
    """Deterministic importance ranking: entry points, security-sensitive names, tool hits, size."""
    hit_counts = Counter(h.file for h in hits)

    def weight(f: RepoFile) -> float:
        rel = f.rel.lower()
        w = 5.0 if PurePosixPath(rel).name in ENTRY_NAMES else 0.0
        w += 3.0 if any(word in rel for word in SECURITY_WORDS) else 0.0
        w += 2.0 * min(hit_counts[f.rel], 5)
        w += min(f.size / 5_000, 2.0)
        w -= 4.0 if _is_test(f.rel) else 0.0
        return w

    return sorted(files, key=lambda f: (-weight(f), f.rel))


def _llm_pick(ranked: list[RepoFile], hits: list[ToolFinding]) -> list[RepoFile]:
    """Only for big repos: let the LLM choose MAX_FILES from the top candidates; fall back to ranking."""
    pool = ranked[: config.MAX_FILES * 3]
    counts = Counter(h.file for h in hits)
    nonce = new_nonce()
    listing = "\n".join(f"{f.rel} ({f.size} bytes, {counts[f.rel]} tool hits)" for f in pool)
    system = load_prompt("orchestrator", MAX_FILES=str(config.MAX_FILES), NONCE=nonce)
    try:
        out = run_agent("orchestrator", system, f"<files_{nonce}>\n{listing}\n</files_{nonce}>", FilePick)
    except AgentError:
        return ranked[: config.MAX_FILES]
    by_rel = {f.rel: f for f in pool}
    picked = list(dict.fromkeys(by_rel[p] for p in out.important_files if p in by_rel))
    for f in ranked:  # top up with the deterministic ranking
        if len(picked) >= config.MAX_FILES:
            break
        if f not in picked:
            picked.append(f)
    return picked[: config.MAX_FILES]


def pack_chunks(rels: list[str], snapshot: Snapshot) -> tuple[list[list[str]], list[str]]:
    """Pack files into prompt-sized chunks, at most MAX_CHUNKS_PER_AGENT; return (chunks, skipped)."""
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for i, rel in enumerate(rels):
        cost = len(file_block(snapshot, rel, NONCE_PLACEHOLDER))
        if current and size + cost > chunk_budget():
            chunks.append(current)
            current, size = [], 0
            if len(chunks) == config.MAX_CHUNKS_PER_AGENT:
                return chunks, rels[i:]
        current.append(rel)
        size += cost
    if current:
        chunks.append(current)
    return chunks, []


def make_plan(snapshot: Snapshot, hits: list[ToolFinding], with_bug_hunter: bool = False,
              include_tests: bool = False) -> tuple[Plan, dict[str, list[list[str]]]]:
    """``include_tests`` (pull-request review): changed test files are reviewed like any other code."""
    candidates = [f for f in snapshot.files if _is_reviewable(f)]
    ranked = rank_files(candidates, hits)
    selected = _llm_pick(ranked, hits) if len(ranked) > config.MAX_FILES else ranked

    readmes = sorted((f.rel for f in snapshot.files if PurePosixPath(f.rel).name.lower().startswith("readme")),
                     key=lambda r: (r.count("/"), r))[:1]
    # Tests are left out of LLM review, except test files with scanner hits: those go to Security,
    # so code placed in a test-looking path cannot hide its findings.
    hit_files = {h.file for h in hits}
    tests = [] if include_tests else [f.rel for f in selected if _is_test(f.rel)]
    meta = [f.rel for f in snapshot.files if f.rel in META_NAMES]
    code_files = [f.rel for f in selected if language_of(f.rel) and f.rel not in tests]
    arch_files = list(dict.fromkeys(readmes + meta + code_files))[: config.MAX_ARCHITECT_FILES]
    sec_files = [f.rel for f in selected if f.rel not in tests or f.rel in hit_files]
    py_files = code_files  # Quality and Bug Hunter review source code in every supported language
    sec_chunks, sec_skipped = pack_chunks(sec_files, snapshot)
    py_chunks, py_skipped = pack_chunks(py_files, snapshot)

    assignments = {"architect": arch_files, "security": sec_files, "quality": py_files}
    chunks = {"security": sec_chunks, "quality": py_chunks}
    if with_bug_hunter:
        assignments["bug_hunter"] = py_files
        chunks["bug_hunter"] = py_chunks
    plan = Plan(
        languages=sorted({lang for f in selected if (lang := language_of(f.rel))}),
        assignments=assignments,
        skipped={
            "not_selected": [f.rel for f in ranked if f not in selected],
            "security_over_budget": sec_skipped,
            "quality_over_budget": py_skipped,
            "truncated": [rel for rel in dict.fromkeys(sec_files + py_files) if is_truncated(snapshot, rel)],
            "tests": [rel for rel in tests if rel not in hit_files],
            "vendored": list(snapshot.vendored),
        },
    )
    return plan, chunks


def plan_warnings(plan: Plan) -> list[str]:
    labels = {
        "not_selected": f"files not reviewed: the repo has more than {config.MAX_FILES} reviewable files",
        "security_over_budget": f"files not reviewed by Security: over the {config.MAX_CHUNKS_PER_AGENT}-batch "
                                "limit per review",
        "quality_over_budget": f"files not reviewed by Quality: over the {config.MAX_CHUNKS_PER_AGENT}-batch "
                               "limit per review",
    }
    notes = [f"{len(plan.skipped[k])} {label}." for k, label in labels.items() if plan.skipped.get(k)]
    for key, label in (("vendored", "bundled third-party asset(s) skipped"),
                       ("tests", "test file(s) without scanner hits not reviewed by the AI agents")):
        skipped = plan.skipped.get(key, [])
        if skipped:
            notes.append(f"{len(skipped)} {label}: " + ", ".join(skipped[:5]) + (" ..." if len(skipped) > 5 else ""))
    truncated = plan.skipped.get("truncated", [])
    if truncated:
        notes.append(f"{len(truncated)} large files were only partly reviewed (truncated): "
                     + ", ".join(truncated[:5]) + (" ..." if len(truncated) > 5 else ""))
    return notes


# --- Pipeline helpers --------------------------------------------------------------


def dedupe(findings: list[Finding]) -> tuple[list[Finding], list[str]]:
    """Drop cross-agent duplicates: same file, line within ±1 and the same evidence. Earlier agents win.

    Findings from the same agent are never merged (two issues can share one line).
    """
    kept: list[Finding] = []
    dropped: list[str] = []
    for f in findings:
        ev = verifier.normalize_code(f.evidence)
        if any(k.source != f.source and k.file == f.file and abs(k.line - f.line) <= 1
               and verifier.normalize_code(k.evidence) == ev for k in kept):
            dropped.append(f.id)
        else:
            kept.append(f)
    return kept, dropped


def _static_analysis(root: Path, snapshot: Snapshot, warnings: list[str],
                     scoped: bool = False) -> tuple[list[ToolFinding], bool]:
    """Scanner hits, plus whether any scanner failed (the review is then not cached).

    ``scoped`` scans only the snapshot's files (pull-request review) instead of the whole repo.
    """
    hits = scan_patterns(snapshot)
    languages = snapshot.languages()
    scanners: list[Callable[[], list[ToolFinding]]] = []
    if "python" in languages:
        py = [f.rel for f in snapshot.python_files()] if scoped else None
        scanners += [lambda: run_bandit(root, py), lambda: run_ruff(root, py)]
    if languages - {"python"}:
        targets = [f.rel for f in snapshot.code_files() if language_of(f.rel) != "python"]
        scanners.append(lambda: run_semgrep(root, targets, languages))
    degraded = False
    for scanner in scanners:
        try:
            hits += scanner()
        except ScanError as exc:
            warnings.append(f"Static analysis degraded: {exc}")
            degraded = True
    return [h for h in hits if snapshot.has(h.file)], degraded


# Finding-producing agents, in dedupe priority order (earlier wins on cross-agent duplicates).
FINDING_AGENTS = {"security": security.run, "bug_hunter": bug_hunter.run, "quality": quality.run}


@dataclass
class SpecialistResults:
    arch: ArchitectOut
    reviews: dict[str, ChunkReview]
    warnings: list[str] = field(default_factory=list)
    degraded: bool = False  # some agent call failed: do not cache this result

    def findings(self) -> list[Finding]:
        return [f for name in FINDING_AGENTS if name in self.reviews for f in self.reviews[name].findings]

    def reviewed_files(self) -> set[str]:
        return set().union(*(r.reviewed_files for r in self.reviews.values()))

    def invalid_items(self) -> int:
        return sum(r.invalid_items for r in self.reviews.values())


def _run_specialists(snapshot: Snapshot, plan: Plan, chunks: dict[str, list[list[str]]],
                     hits: list[ToolFinding], notify: Progress, include_architect: bool = True) -> SpecialistResults:
    progress: Queue[str] = Queue()  # workers report batches here; only this thread calls notify()

    def on_batch(agent: str, done: int, total: int, found: int) -> None:
        progress.put(f"{_label(agent)}: batch {done}/{total} reviewed ({found} findings so far)")

    def drain() -> None:
        while True:
            try:
                notify(progress.get_nowait())
            except Empty:
                return

    with ThreadPoolExecutor(max_workers=config.AGENT_WORKERS) as pool:
        futures = ({submit_in_context(pool, architect.run, snapshot, plan.assignments["architect"]): "architect"}
                   if include_architect else {})
        futures |= {submit_in_context(pool, FINDING_AGENTS[name], agent_chunks, snapshot, hits, on_batch): name
                    for name, agent_chunks in chunks.items()}
        pending = set(futures)
        while pending:  # progress from the calling thread (Streamlit requires that)
            finished, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            drain()
            for fut in finished:
                notify(f"{_label(futures[fut])} agent finished")
        results = {name: fut for fut, name in futures.items()}
        warnings = []
        degraded = False
        try:
            arch = results.pop("architect").result() if include_architect else ArchitectOut(summary="")
        except AgentError as exc:
            arch = architect.fallback(snapshot)
            warnings.append(f"Architect failed: {exc}")
            degraded = True
        res = SpecialistResults(arch=arch, reviews={n: f.result() for n, f in results.items()}, warnings=warnings,
                                degraded=degraded)

    errors = [e for r in res.reviews.values() for e in r.errors]
    total_chunks = sum(len(c) for c in chunks.values())
    if total_chunks and len(errors) == total_chunks:
        raise ReviewError("Every reviewer call failed; no trustworthy report. " + errors[0])
    res.warnings += [f"Partial failure: {e}" for e in errors]
    res.degraded = res.degraded or bool(errors)
    if res.invalid_items():
        res.warnings.append(f"{res.invalid_items()} malformed findings from the LLM were discarded.")
    return res


def _label(agent: str) -> str:
    return agent.replace("_", " ").capitalize()


def _masked(f: Finding) -> Finding:
    return f.model_copy(update={"evidence": mask_secrets(f.evidence), "issue": mask_secrets(f.issue),
                                "fix": mask_secrets(f.fix)})


# --- Pipeline -----------------------------------------------------------------


@dataclass(frozen=True)
class ReviewOptions:
    verify: bool = True
    bug_hunter: bool = field(default_factory=lambda: config.ENABLE_BUG_HUNTER)
    commit: str | None = None  # pin a full SHA (evals, or the commit resolved by lookup_cache)
    use_cache: bool = True


@dataclass
class ReviewResult:
    report_md: str
    data: ReviewData
    cached: bool = False


@dataclass(frozen=True)
class CacheLookup:
    commit: str  # resolved commit; pass it on via ReviewOptions(commit=...) to avoid a second ls-remote
    key: str
    hit: ReviewResult | None


def lookup_cache(url: str, options: ReviewOptions | None = None) -> CacheLookup:
    """Resolve the commit (pinning HEAD) and look the review up. Costs no LLM quota."""
    opts = options or ReviewOptions()
    ref = repo.canonicalize(url)
    commit = opts.commit or repo.remote_head(ref)
    key = cache.cache_key(ref, commit, verify=opts.verify, bug_hunter=opts.bug_hunter)
    hit = cache.load(key)
    return CacheLookup(commit, key, ReviewResult(report_md=hit[0], data=hit[1], cached=True) if hit else None)


def run_review(url: str, options: ReviewOptions | None = None, progress: Progress | None = None) -> ReviewResult:
    opts = options or ReviewOptions()
    notify = progress or (lambda _msg: None)
    ref = repo.canonicalize(url)

    key = None
    commit = opts.commit
    if opts.use_cache:
        notify("Checking cache")
        lookup = lookup_cache(url, opts)  # pins HEAD: the review matches the cache key
        if lookup.hit:
            return lookup.hit
        commit, key = lookup.commit, lookup.key

    llm.ensure_ready()
    stats = RunStats()
    token = current_run.set(stats)
    start = time.monotonic()
    try:
        notify("Checking repository")
        repo.check_repo_size(ref)
        notify("Cloning repository")
        with repo.cloned_repo(ref, commit) as root:
            result = _review_clone(ref, root, opts, stats, start, notify)
    finally:
        current_run.reset(token)
    if key and not result.data.degraded:  # never pin a partial review to this commit for everyone
        try:
            cache.save(key, result.report_md, result.data)
        except OSError as exc:  # a finished review is worth more than its cache entry
            log_event("cache_error", error=str(exc)[: config.ERROR_LOG_CHARS])
    return result


def _review_clone(ref: RepoRef, root: Path, opts: ReviewOptions, stats: RunStats, start: float,
                  notify: Progress) -> ReviewResult:
    vendored: list[str] = []
    snapshot = Snapshot(root, repo.list_files(root, vendored), vendored)
    warnings = repo.language_warnings(snapshot)
    commit = repo.head_commit(root)
    log_event("review_start", repo=ref.slug, commit=commit, files=len(snapshot.files))

    notify("Running static analysis")
    hits, scan_degraded = _static_analysis(root, snapshot, warnings)
    notify("Planning")
    plan, chunks = make_plan(snapshot, hits, with_bug_hunter=opts.bug_hunter)
    warnings += plan_warnings(plan)

    notify("Running agents: " + ", ".join(["architect", *chunks]).replace("_", " "))
    spec = _run_specialists(snapshot, plan, chunks, hits, notify)
    warnings += spec.warnings
    raw = spec.findings()

    verify_start = time.monotonic()
    if opts.verify:
        notify(f"Verifying {len(raw)} findings")
        confirmed, rejected = verifier.verify(raw, snapshot)
    else:
        confirmed, rejected = raw, []
    verify_seconds = time.monotonic() - verify_start
    confirmed, duplicates = dedupe(confirmed)

    loc = snapshot.total_loc()
    result_score = score(confirmed, loc)
    notify("Writing report")
    summary = writer.plan_summary(spec.arch, confirmed)

    seconds = round(time.monotonic() - start, 1)
    selected = set(plan.assignments["security"]) | set(plan.assignments["quality"])
    meta = RunMeta(files_reviewed=len(spec.reviewed_files()), files_selected=len(selected), loc=loc, seconds=seconds,
                   llm_calls=stats.llm_calls, verified=opts.verify, warnings=warnings)
    report_md = writer.render(ref.slug, commit, spec.arch, summary, confirmed, rejected, result_score, meta)
    log_event("review_done", repo=ref.slug, raw=len(raw), confirmed=len(confirmed), seconds=seconds,
              llm_calls=stats.llm_calls)
    degraded = spec.degraded or scan_degraded or any(r.stage == "error" for r in rejected)
    data = _build_data(ref, commit, opts, plan, hits, spec, raw, confirmed, rejected, duplicates,
                       result_score, stats, meta, round(verify_seconds, 1), degraded)
    return ReviewResult(report_md=report_md, data=data)


def _build_data(ref: RepoRef, commit: str, opts: ReviewOptions, plan: Plan, hits: list[ToolFinding],
                spec: SpecialistResults, raw: list[Finding], confirmed: list[Finding], rejected: list[Rejection],
                duplicates: list[str], result_score: Score, stats: RunStats, meta: RunMeta,
                verify_seconds: float, degraded: bool) -> ReviewData:
    """findings.json content. Secrets are masked: this file may be committed or downloaded."""
    return ReviewData(
        run_id=stats.run_id,
        repo=ref.slug,
        commit=commit,
        pipeline_version=config.PIPELINE_VERSION,
        model=config.MODEL_NAME,
        verified=opts.verify,
        bug_hunter=opts.bug_hunter,
        degraded=degraded,
        plan=plan,
        tool_hits=len(hits),
        duplicates_dropped=duplicates,
        invalid_items_dropped=spec.invalid_items(),
        raw_findings=[_masked(f) for f in raw],
        confirmed_ids=[f.id for f in confirmed],
        rejected=[RejectedOut(id=r.finding.id, stage=r.stage, reason=mask_secrets(r.reason)) for r in rejected],
        architecture=spec.arch,
        score=result_score,
        stats=RunStatsOut(seconds=meta.seconds, llm_calls=stats.llm_calls, llm_errors=stats.llm_errors,
                          llm_seconds=round(stats.llm_seconds, 1), verifier_seconds=verify_seconds,
                          verifier_calls=stats.calls_by_agent.get("verifier", 0)),
        warnings=meta.warnings,
    )
