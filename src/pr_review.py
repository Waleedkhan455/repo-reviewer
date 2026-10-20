"""Pull-request bot: review only the lines a PR changed and post the results on GitHub.

    python -m src.pr_review                      # inside GitHub Actions (reads the event payload)
    python -m src.pr_review --repo-dir . --base <sha> --head <sha> --dry-run   # locally

The review itself lives in ``src/changes.py`` (called through ``src/service.py``); this module renders
the result, posts it to GitHub and implements the CLI and the merge gate. It reuses the normal pipeline
(static analysis, specialist agents, Verifier, dedupe, scoring) on the changed files only. Agents see
the whole file for context but are told which lines changed, and only confirmed findings on changed
lines become inline comments. A single summary comment is updated on
every push; inline comments are never posted twice.

The PR content is attacker-controlled, so the bot fails closed: anything it could not review is
listed, and with ``fail-on`` set an incomplete review (or a high-severity scanner hit the AI did not
confirm) fails the check instead of passing silently.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from src import config, service
from src.agents import verifier
from src.agents.writer import escape_md
from src.changes import PRReview
from src.github_api import GitHubClient, GitHubError
from src.proc import run_bounded, safe_env
from src.schemas import SEVERITY_ORDER, Finding
from src.service import DiffError, LLMError, ReviewError

SUMMARY_MARKER = "<!-- repo-reviewer:summary -->"
_FP_MARKER = re.compile(r"<!-- repo-reviewer:fp=([0-9a-f]{16}) -->")
SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}
FAIL_ON = ("none", "low", "medium", "high")
UNSAFE_EVENTS = {"pull_request_target", "workflow_run"}  # would run untrusted PR code with secrets


def fingerprint(f: Finding) -> str:
    """Line-independent identity, so a finding is not re-posted when later commits shift lines."""
    raw = "|".join([f.file, f.source, verifier.normalize_code(f.issue).lower(), verifier.normalize_code(f.evidence)])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# --- Rendering -----------------------------------------------------------------


def inline_body(f: Finding) -> str:
    return (f"{SEVERITY_ICON[f.severity]} **{f.severity.upper()}** · {f.source.replace('_', ' ')}\n\n"
            f"{escape_md(f.issue)}\n\n**Fix:** {escape_md(f.fix)}\n\n"
            f"<sub>Repo Reviewer · confirmed by the Verifier</sub>\n<!-- repo-reviewer:fp={fingerprint(f)} -->")


def summary_body(review: PRReview, head: str, note: str = "") -> str:
    s, c = review.result_score, review.result_score.counts
    lines = [SUMMARY_MARKER, "## 🔎 Repo Reviewer", ""]
    if not review.complete:
        lines += [("> ⚠️ **Review incomplete.** Parts of this change were not reviewed (listed below); "
                  "do not treat the score as a clean bill of health."), ""]
    lines += [
        (f"**Score for this change: {s.score} / 100** · 🔴 {c['high']} high · 🟠 {c['medium']} medium · "
         f"🟡 {c['low']} low"),
        "",
        (f"Reviewed {len(review.files)} changed file(s), {review.changed_line_count} changed line(s) at "
         f"`{head[:12]}` · {review.llm_calls} LLM calls · "
         f"{len(review.rejected)} finding(s) rejected by the Verifier"),
    ]
    if review.in_diff:
        lines += ["", "| | Location | Issue |", "|---|---|---|"]
        lines += [f"| {SEVERITY_ICON[f.severity]} | {escape_md(f.file)}:{f.line} | {escape_md(f.issue)} |"
                  for f in review.in_diff]
    else:
        lines += ["", "✅ No confirmed issues on the changed lines."]
    if review.scanner_flags:
        lines += ["", "**Flagged by static analysis on changed lines (not confirmed by the AI review):**"]
        lines += [f"- 🔴 {escape_md(h.file)}:{h.line} · {h.tool} {escape_md(h.rule)}: {escape_md(h.message)}"
                  for h in review.scanner_flags]
    if review.not_reviewed:
        lines += ["", "**Not reviewed:**", *(f"- {escape_md(n)}" for n in review.not_reviewed)]
    if review.outside:
        lines += ["", ("<details><summary>Issues elsewhere in the changed files (not introduced by "
                  f"these lines): {len(review.outside)}</summary>"), ""]
        lines += [f"- {SEVERITY_ICON[f.severity]} {escape_md(f.file)}:{f.line}: {escape_md(f.issue)}"
                  for f in review.outside]
        lines += ["", "</details>"]
    notes = [*review.warnings, *([note] if note else [])]
    if notes:
        lines += ["", *(f"> {escape_md(n)}" for n in notes)]
    return "\n".join(lines) + "\n"


# --- Posting -------------------------------------------------------------------


def _is_mine(comment: dict) -> bool:
    """Only the bot's own comments are trusted (anyone can post text containing our markers)."""
    return (comment.get("user") or {}).get("login") == config.PR_BOT_LOGIN


def post_review(client: GitHubClient, pr: int, head: str, review: PRReview) -> dict:
    """Post new inline comments (skipping ones already on the PR) and upsert the summary comment."""
    mine = [c for c in client.review_comments(pr) if _is_mine(c)]
    existing = {m[1] for c in mine for m in [_FP_MARKER.search(c.get("body") or "")] if m}
    budget = max(0, config.PR_MAX_TOTAL_INLINE_COMMENTS - len(existing))
    new = [f for f in review.in_diff if fingerprint(f) not in existing][: min(budget, config.PR_MAX_INLINE_COMMENTS)]
    note = ""
    if new:
        comments = [{"path": f.file, "line": f.line, "side": "RIGHT", "body": inline_body(f)} for f in new]
        try:
            client.create_review(pr, head, f"Repo Reviewer found {len(new)} new issue(s) on the changed lines.",
                                 comments)
        except GitHubError as exc:
            if exc.status != 422:  # 422: a line is not part of the diff (e.g. the PR moved on); keep the summary
                raise
            note = "Inline comments could not be attached to the diff; see the table above."
            new = []
    body = summary_body(review, head, note)
    summaries = [c for c in client.issue_comments(pr) if _is_mine(c) and SUMMARY_MARKER in (c.get("body") or "")]
    if summaries:
        client.update_issue_comment(summaries[0]["id"], body)
    else:
        client.create_issue_comment(pr, body)
    return {"inline_posted": len(new), "already_posted": len(review.in_diff) - len(new) if not note else 0}


def fail_reasons(review: PRReview, fail_on: str) -> list[str]:
    """Why the check should fail under ``fail_on`` (empty list = pass). Fails closed on incomplete reviews."""
    if fail_on == "none":
        return []
    reasons = []
    if not review.complete:
        reasons.append("the review is incomplete")
    threshold = SEVERITY_ORDER[fail_on]
    if any(SEVERITY_ORDER[f.severity] <= threshold for f in review.in_diff):
        reasons.append(f"confirmed {fail_on}+ issues on changed lines")
    if review.scanner_flags:
        reasons.append("high-severity static-analysis hits on changed lines")
    return reasons


# --- CLI ---------------------------------------------------------------------------


def _event() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("pull_request") or {}
    except (OSError, ValueError):
        return {}


def _write_step_summary(text: str) -> None:
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text.replace(SUMMARY_MARKER, ""))


def _checked_out_head(root: Path) -> str:
    res = run_bounded(["git", "rev-parse", "HEAD"], cwd=root, timeout=config.GIT_CMD_TIMEOUT_S, env=safe_env())
    return res.stdout.strip() if res.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # emoji in the output; Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    event = _event()
    parser = argparse.ArgumentParser(description="Review the lines a pull request changed")
    parser.add_argument("--repo-dir", type=Path, default=Path(os.environ.get("GITHUB_WORKSPACE", ".")))
    parser.add_argument("--base", default=(event.get("base") or {}).get("sha"))
    parser.add_argument("--head", default=(event.get("head") or {}).get("sha"))
    parser.add_argument("--pr", type=int, default=event.get("number"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--fail-on", choices=FAIL_ON, default=os.environ.get("RR_FAIL_ON", "none") or "none")
    parser.add_argument("--bug-hunter", action="store_true", default=config.ENABLE_BUG_HUNTER)
    parser.add_argument("--dry-run", action="store_true", help="print what would be posted; post nothing")
    args = parser.parse_args(argv)

    if os.environ.get("GITHUB_EVENT_NAME") in UNSAFE_EVENTS:
        print("error: refusing to run on pull_request_target/workflow_run: that would review untrusted code "
              "with repository secrets available. Use the pull_request trigger.", file=sys.stderr)
        return 2
    if not config.GEMINI_API_KEY:
        msg = ("Repo Reviewer: not reviewed, GEMINI_API_KEY is not available "
               "(pull requests from forks do not receive repository secrets).")
        print(msg, file=sys.stderr)
        _write_step_summary(f"## 🔎 Repo Reviewer\n\n⚠️ {msg}\n")
        return 1 if args.fail_on != "none" else 0  # fail closed when this check is a merge gate
    if not args.base or not args.head:
        print("error: --base and --head are required outside a pull_request event", file=sys.stderr)
        return 2
    root = args.repo_dir.resolve()
    try:
        review = service.review_changes(root, args.base, args.head, bug_hunter=args.bug_hunter,
                                        progress=lambda m: print(f"... {m}", file=sys.stderr))
    except (DiffError, LLMError, ReviewError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        _write_step_summary(f"## 🔎 Repo Reviewer\n\n⚠️ Not reviewed: {escape_md(str(exc))}\n")
        return 2
    if _checked_out_head(root) not in ("", args.head):
        review.warnings.append("The checked-out commit is not the PR head; comment line numbers may be off "
                               "(check out github.event.pull_request.head.sha).")

    summary = summary_body(review, args.head)
    _write_step_summary(summary)
    if args.dry_run:
        print(json.dumps({"inline_comments": [{"path": f.file, "line": f.line, "body": inline_body(f)}
                                              for f in review.in_diff],
                          "summary": summary}, indent=2, ensure_ascii=False))
    else:
        if not (args.pr and args.repo and config.GITHUB_TOKEN):
            print("error: posting needs --pr, --repo and GITHUB_TOKEN (or use --dry-run)", file=sys.stderr)
            return 2
        try:
            posted = post_review(GitHubClient(config.GITHUB_TOKEN, args.repo), args.pr, args.head, review)
        except GitHubError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"Repo Reviewer: {posted['inline_posted']} inline comment(s) posted, summary updated.",
              file=sys.stderr)
    reasons = fail_reasons(review, args.fail_on)
    if reasons:
        print(f"Repo Reviewer: failing the check ({'; '.join(reasons)}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
