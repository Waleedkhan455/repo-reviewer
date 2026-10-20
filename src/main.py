"""Command line entry point.

    python -m src.main https://github.com/<owner>/<repo> [--no-verify] [--out reports]
"""

import argparse
import sys
from pathlib import Path

from src import config, service
from src.service import LLMError, RepoError, ReviewError, ReviewOptions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-agent AI code review for a public GitHub repo")
    parser.add_argument("url", help="https://github.com/<owner>/<repo>")
    parser.add_argument("--no-verify", action="store_true", help="skip the Verifier (eval baseline)")
    parser.add_argument("--bug-hunter", action="store_true", default=config.ENABLE_BUG_HUNTER,
                        help="also run the optional Bug Hunter agent")
    parser.add_argument("--commit", help="review this exact 40-char commit SHA instead of HEAD")
    parser.add_argument("--no-cache", action="store_true", help="ignore and do not write the review cache")
    parser.add_argument("--out", type=Path, default=config.REPORTS_DIR, help="output directory")
    args = parser.parse_args(argv)

    try:
        opts = ReviewOptions(verify=not args.no_verify, bug_hunter=args.bug_hunter, commit=args.commit,
                             use_cache=not args.no_cache)
        result = service.review_repo(args.url, opts, progress=lambda msg: print(f"... {msg}", file=sys.stderr))
    except (RepoError, LLMError, ReviewError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    suffix = "-noverify" if args.no_verify else ""
    data = result.data
    out_dir = args.out / f"{data.repo.replace('/', '__')}{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(result.report_md, encoding="utf-8")
    (out_dir / "findings.json").write_text(data.model_dump_json(indent=2), encoding="utf-8")

    print(f"Score {data.score.score}/100 | {len(data.confirmed_ids)} confirmed, {len(data.rejected)} rejected | "
          f"{data.stats.seconds}s, {data.stats.llm_calls} LLM calls")
    print(f"Report: {out_dir / 'report.md'}" + (" (from cache)" if result.cached else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
