"""Report Writer agent. The LLM writes only the overview and picks the top fixes;
everything else in the report is rendered deterministically from validated data."""

import json
import re

from src import config
from src.agents.base import AgentError, load_prompt, run_agent
from src.schemas import (
    SEVERITY_ORDER,
    ArchitectOut,
    Finding,
    Rejection,
    RunMeta,
    Score,
    TopFix,
    WriterOut,
)
from src.tools.patterns import mask_secrets


def _by_severity(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity])


def plan_summary(arch: ArchitectOut, confirmed: list[Finding]) -> WriterOut:
    """Ask the LLM for an overview and top fixes; fall back to a deterministic version on failure."""
    fallback_fixes = [TopFix(id=f.id, why=f.fix) for f in _by_severity(confirmed)[: config.TOP_FIXES]]
    if not confirmed:
        return WriterOut(overview=arch.summary, top_fixes=[])
    user = json.dumps({
        "architecture": arch.model_dump(),
        "confirmed_findings": [f.model_dump(include={"id", "file", "line", "severity", "issue", "fix"})
                               for f in confirmed],
    }, ensure_ascii=False)
    try:
        out = run_agent("writer", load_prompt("writer", TOP_FIXES=str(config.TOP_FIXES)), user, WriterOut)
    except AgentError:
        return WriterOut(overview=arch.summary, top_fixes=fallback_fixes)
    known = {f.id for f in confirmed}
    picked: list[TopFix] = []
    seen: set[str] = set()
    for fix in [*out.top_fixes, *fallback_fixes]:  # LLM picks first (known ids only), then top up
        if len(picked) >= config.TOP_FIXES:
            break
        if fix.id in known and fix.id not in seen:
            picked.append(fix)
            seen.add(fix.id)
    return WriterOut(overview=out.overview, top_fixes=picked)


# --- Markdown rendering ------------------------------------------------------------


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+!|~])")


def _code(text: str) -> str:
    """Fenced block whose fence is longer than any backtick run inside, so content cannot escape it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{mask_secrets(text)}\n{fence}"


def escape_md(text: str) -> str:
    """Untrusted prose (LLM or repo text) as inert Markdown: no links, images, HTML or formatting.

    Escaping stops [x](url) and ![](url); the zero-width space stops bare-URL autolinking, and
    @mentions and #123 / owner/repo#123 / GH-123 references (which notify people or cross-link
    issues on GitHub).
    """
    flat = " ".join(mask_secrets(text).split())
    flat = _MD_SPECIAL.sub(r"\\\1", flat)
    return (flat.replace("://", ":​//").replace("www.", "www​.").replace("@", "@​")
            .replace("#", "#​").replace("GH-", "GH​-"))


def _cell(text: str) -> str:
    return escape_md(text)  # "|" is already escaped


def _header(repo_slug: str, commit: str, s: Score, meta: RunMeta) -> list[str]:
    c = s.counts
    return [
        f"# Code Review: {repo_slug}",
        "",
        (f"Commit `{commit[:12] or 'unknown'}` · {meta.files_reviewed} of {meta.files_selected} files reviewed · "
         f"{meta.loc} lines of code · {meta.seconds}s · {meta.llm_calls} LLM calls"),
        "",
        f"## Score: {s.score} / 100",
        "",
        (f"High **{c['high']}** · Medium **{c['medium']}** · Low **{c['low']}** · "
         f"{s.per_kloc} findings per 1k LOC"),
        "",
        ("_Score = 100 − (10 × high + 4 × medium + 1 × low), floored at 0. It is not adjusted for repo size; "
         "see findings per 1k LOC._"),
    ]


def _architecture(summary: WriterOut, arch: ArchitectOut) -> list[str]:
    out = ["", "## Overview", "", escape_md(summary.overview), "", "## Architecture", "", escape_md(arch.summary)]
    if arch.modules:
        out += ["", "| Module | Purpose |", "|---|---|"]
        out += [f"| {_cell(m.name)} | {_cell(m.purpose)} |" for m in arch.modules]
    if arch.entry_points:
        out += ["", "**Entry points:** " + ", ".join(escape_md(e) for e in arch.entry_points)]
    if arch.dependencies:
        out += ["", "**Dependencies:** " + ", ".join(escape_md(d) for d in arch.dependencies)]
    return out


def _top_fixes(summary: WriterOut, confirmed: list[Finding]) -> list[str]:
    by_id = {f.id: f for f in confirmed}
    out = ["", f"## Top {config.TOP_FIXES} Fixes", ""]
    if not summary.top_fixes:
        return [*out, "No confirmed findings."]
    for n, fix in enumerate(summary.top_fixes, start=1):
        f = by_id[fix.id]
        out.append(f"{n}. **{f.id}** ({f.severity}) {escape_md(f.file)}:{f.line}: {escape_md(fix.why)}")
    return out


def _findings(confirmed: list[Finding]) -> list[str]:
    out = ["", "## Findings"]
    for sev in ("high", "medium", "low"):
        group = [f for f in confirmed if f.severity == sev]
        if not group:
            continue
        out += ["", f"### {sev.capitalize()} ({len(group)})"]
        for f in group:
            out += [
                "",
                f"#### {f.id}: {escape_md(f.issue)}",
                f"{escape_md(f.file)}:{f.line} · found by {f.source}",
                "",
                _code(f.evidence),
                "",
                f"**Fix:** {escape_md(f.fix)}",
            ]
    return out


def _verification(confirmed: list[Finding], rejected: list[Rejection], meta: RunMeta) -> list[str]:
    raw = len(confirmed) + len(rejected)
    stages = {s: sum(1 for r in rejected if r.stage == s) for s in ("precheck", "llm", "error")}
    out = [
        "",
        "## Verification",
        "",
        (f"{raw} raw findings → **{len(confirmed)} confirmed**, {len(rejected)} rejected "
         f"({stages['precheck']} by plain-Python pre-check, {stages['llm']} by the LLM verifier, "
         f"{stages['error']} not verifiable due to errors)."),
    ]
    if not meta.verified:
        out += ["", "_Verifier was disabled for this run: findings are unverified._"]
    return out


def render(
    repo_slug: str,
    commit: str,
    arch: ArchitectOut,
    summary: WriterOut,
    confirmed: list[Finding],
    rejected: list[Rejection],
    result_score: Score,
    meta: RunMeta,
) -> str:
    out = [
        *_header(repo_slug, commit, result_score, meta),
        *_architecture(summary, arch),
        *_top_fixes(summary, confirmed),
        *_findings(confirmed),
        *_verification(confirmed, rejected, meta),
    ]
    if meta.warnings:
        out += ["", "## Notes", ""] + [f"- {escape_md(n)}" for n in meta.warnings]
    return "\n".join(out) + "\n"
