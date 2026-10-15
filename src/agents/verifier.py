"""Verifier agent: drop findings that are not supported by the real code.

Stage 1 is plain Python (free): the file must exist in the snapshot, the line must be in
range, and the evidence must appear near the cited line. Stage 2 asks the LLM, one call per
file, whether the code really has the problem.
"""

import re
from concurrent.futures import ThreadPoolExecutor

from src import config
from src.agents.base import (
    NONCE_PLACEHOLDER,
    AgentError,
    load_prompt,
    new_nonce,
    number_lines,
    run_agent,
    validate_items,
    wrap_file,
)
from src.logger import submit_in_context
from src.repo import Snapshot
from src.schemas import Finding, RawItems, Rejection, Verdict

_LINE_PREFIX = re.compile(r"^\s*\d+\s*\|\s?", re.MULTILINE)


def normalize_code(s: str) -> str:
    return " ".join(s.replace("...", " ").replace("…", " ").split())


def evidence_near(lines: list[str], line: int, evidence: str, slack: int | None = None) -> bool:
    """True if ``evidence`` (whitespace-insensitive) appears within ``slack`` lines of ``line`` (1-based).

    Short evidence (e.g. ``eval(s)``) proves little as a substring, so it must equal a whole line.
    """
    slack = config.EVIDENCE_SLACK_LINES if slack is None else slack
    target = normalize_code(_LINE_PREFIX.sub("", evidence))
    if not target:
        return False
    lo = max(0, line - 1 - slack)
    hi = min(len(lines), line + slack + evidence.count("\n"))
    if len(target) < config.MIN_EVIDENCE_CHARS:
        return any(normalize_code(text) == target for text in lines[lo:hi])
    return target in normalize_code("\n".join(lines[lo:hi]))


def precheck(finding: Finding, snapshot: Snapshot) -> str | None:
    """Return a rejection reason, or None if the finding passes the plain-Python checks."""
    if not snapshot.has(finding.file):
        return "file not found in repository"
    lines = snapshot.lines(finding.file)
    if not 1 <= finding.line <= len(lines):
        return f"line {finding.line} out of range (file has {len(lines)} lines)"
    if not evidence_near(lines, finding.line, finding.evidence):
        return "evidence snippet not found near the cited line"
    return None


def _context(snapshot: Snapshot, finding: Finding) -> str:
    lines = snapshot.lines(finding.file)
    lo = max(1, finding.line - config.VERIFY_CONTEXT_LINES)
    hi = min(len(lines), finding.line + config.VERIFY_CONTEXT_LINES)
    return number_lines("\n".join(lines[lo - 1 : hi]), start=lo)


def _claim(finding: Finding, snapshot: Snapshot, nonce: str) -> str:
    return (
        f"CLAIM {finding.id}\n"
        f"  file: {finding.file}\n  line: {finding.line}\n  severity: {finding.severity}\n"
        f"  issue: {finding.issue}\n  claimed evidence: {finding.evidence}\n"
        f"Actual code around line {finding.line}:\n{wrap_file(finding.file, _context(snapshot, finding), nonce)}"
    )


def _batches(findings: list[Finding], snapshot: Snapshot) -> list[list[Finding]]:
    """Group findings by file; split a file's group if its claims would exceed the prompt cap."""
    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.file, []).append(f)
    batches = []
    for group in by_file.values():
        batch, size = [], 0
        for f in group:
            cost = len(_claim(f, snapshot, NONCE_PLACEHOLDER))
            if batch and size + cost > config.MAX_PROMPT_CHARS:
                batches.append(batch)
                batch, size = [], 0
            batch.append(f)
            size += cost
        batches.append(batch)
    return batches


def _verify_batch(batch: list[Finding], snapshot: Snapshot) -> tuple[list[Finding], list[Rejection]]:
    nonce = new_nonce()
    user = "\n\n".join(_claim(f, snapshot, nonce) for f in batch)
    try:
        reply = run_agent("verifier", load_prompt("verifier", NONCE=nonce), user, RawItems)
    except AgentError as exc:
        return [], [Rejection(finding=f, stage="error", reason=f"not verified: {exc}") for f in batch]
    valid, _dropped = validate_items("verifier", reply.verdicts, Verdict)  # an invalid verdict = no verdict
    verdicts = {}
    for v in valid:
        verdicts.setdefault(v.id, v)  # first verdict per id wins; unknown ids are ignored
    confirmed, rejected = [], []
    for f in batch:
        v = verdicts.get(f.id)
        if v is None:
            rejected.append(Rejection(finding=f, stage="llm", reason="no verdict returned"))
        elif v.verdict == "confirmed":
            confirmed.append(f)
        else:
            rejected.append(Rejection(finding=f, stage="llm", reason=v.reason or "rejected"))
    return confirmed, rejected


def verify(findings: list[Finding], snapshot: Snapshot) -> tuple[list[Finding], list[Rejection]]:
    """Return (confirmed, rejected). Every input finding ends up in exactly one of the two lists."""
    passed, rejected = [], []
    for f in findings:
        reason = precheck(f, snapshot)
        if reason:
            rejected.append(Rejection(finding=f, stage="precheck", reason=reason))
        else:
            passed.append(f)

    confirmed: list[Finding] = []
    batches = _batches(passed, snapshot)
    with ThreadPoolExecutor(max_workers=config.VERIFY_WORKERS) as pool:
        futures = [submit_in_context(pool, _verify_batch, b, snapshot) for b in batches]
        for fut in futures:
            ok, bad = fut.result()
            confirmed.extend(ok)
            rejected.extend(bad)

    order = {f.id: i for i, f in enumerate(findings)}
    confirmed.sort(key=lambda f: order[f.id])
    return confirmed, rejected
