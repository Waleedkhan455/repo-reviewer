"""Streamlit UI: paste a public GitHub URL, get a verified multi-agent code review.

    streamlit run app.py

A submit stores the job in session_state and reruns, so the form is redrawn disabled while the
review runs on the next script pass (a second click cannot restart or double-charge it).
"""

import json
import uuid
from dataclasses import dataclass

import streamlit as st

from src import config
from src.llm import LLMError
from src.logger import log_event, read_trace
from src.orchestrator import ReviewError, ReviewOptions, ReviewResult, lookup_cache, run_review
from src.quota import Quota
from src.repo import RepoError, canonicalize

st.set_page_config(page_title="Repo Reviewer", page_icon="🔎", layout="wide")

GENERIC_AI_ERROR = "The AI service is unavailable or rate-limited right now. Please try again later."
GENERIC_ERROR = "Something went wrong while reviewing this repository."


@st.cache_resource
def shared_quota() -> Quota:
    """One instance per server process, shared by every browser session."""
    return Quota(config.APP_DAILY_RUN_CAP, config.APP_MAX_CONCURRENT, config.APP_MAX_CONCURRENT_LOOKUPS,
                 config.APP_COOLDOWN_S)


@st.cache_data(ttl=30, show_spinner=False)
def trace_rows(run_id: str) -> list[dict]:
    """Only an allowlist of columns: raw errors and stderr stay in the server log."""
    return [{k: e.get(k) for k in config.APP_TRACE_COLUMNS} for e in read_trace(run_id)]


def client_id() -> str:
    """Client address when available (survives page reloads), else a per-session id."""
    ip = st.context.ip_address
    if ip:
        return ip
    return "session:" + st.session_state.setdefault("sid", uuid.uuid4().hex)


@dataclass
class Outcome:
    result: ReviewResult | None = None
    level: str = ""  # "error" | "warning"
    message: str = ""


def _log_error(exc: BaseException) -> None:
    log_event("app_error", error=f"{type(exc).__name__}: {str(exc)[: config.ERROR_LOG_CHARS]}")


def review(url: str, bug_hunter: bool) -> Outcome:
    """Serve from cache when possible; otherwise enforce limits and run a fresh review."""
    quota = shared_quota()
    opts = ReviewOptions(bug_hunter=bug_hunter)
    try:
        canonicalize(url)
        if not quota.try_lookup():
            return Outcome(level="warning", message="The server is busy. Please try again in a minute.")
        try:
            lookup = lookup_cache(url, opts)
        finally:
            quota.end_lookup()
    except RepoError as exc:
        return Outcome(level="error", message=str(exc))
    if lookup.hit:
        return Outcome(result=lookup.hit)

    refusal = quota.acquire(client_id())
    if refusal:
        return Outcome(level="warning", message=refusal)
    refund = False
    try:
        with st.status("Reviewing repository...", expanded=True) as status:
            # Reuse the commit resolved above: no second ls-remote, and the cache key is unchanged.
            result = run_review(url, ReviewOptions(bug_hunter=bug_hunter, commit=lookup.commit),
                                progress=status.write)
            status.update(label="Review complete", state="complete", expanded=False)
        return Outcome(result=result)
    except RepoError as exc:  # user-facing by design; raised before any LLM call, so refund the run
        refund = True
        return Outcome(level="error", message=str(exc))
    except LLMError as exc:
        refund = "not set" in str(exc)  # missing key: nothing was spent
        _log_error(exc)
        return Outcome(level="error", message=GENERIC_AI_ERROR)
    except ReviewError as exc:
        _log_error(exc)
        return Outcome(level="error", message=GENERIC_AI_ERROR)
    except Exception as exc:  # never show internals (paths, keys) to visitors
        _log_error(exc)
        return Outcome(level="error", message=GENERIC_ERROR)
    finally:
        quota.release(refund=refund)


def show_result(result: ReviewResult) -> None:
    d = result.data
    st.subheader(f"{d.repo} @ {d.commit[:12]}")
    cols = st.columns(5)
    cols[0].metric("Score", f"{d.score.score} / 100")
    cols[1].metric("Confirmed findings", len(d.confirmed_ids))
    cols[2].metric("Rejected by Verifier", len(d.rejected))
    cols[3].metric("Time", f"{d.stats.seconds}s")
    cols[4].metric("LLM calls", d.stats.llm_calls)
    if result.cached:
        st.caption("Served from cache: this commit was already reviewed.")

    report_tab, findings_tab, trace_tab = st.tabs(["Report", "All findings", "Trace"])
    with report_tab:
        # unsafe_allow_html stays off, and the writer escapes all LLM/repo prose (no links or images).
        st.markdown(result.report_md)
    with findings_tab:
        confirmed, duplicates = set(d.confirmed_ids), set(d.duplicates_dropped)
        reasons = {r.id: f"{r.stage}: {r.reason}" for r in d.rejected}

        def status_of(fid: str) -> str:
            if fid in confirmed:
                return "confirmed"
            return "duplicate" if fid in duplicates else "rejected"

        st.dataframe(
            [{"id": f.id, "status": status_of(f.id), "severity": f.severity, "file": f"{f.file}:{f.line}",
              "issue": f.issue, "verifier": reasons.get(f.id, "")} for f in d.raw_findings],
            use_container_width=True, hide_index=True,
        )
    with trace_tab:
        rows = trace_rows(d.run_id)
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.caption("No trace available for this run (served from cache, or the log was rotated).")

    slug = d.repo.replace("/", "__")
    c1, c2 = st.columns(2)
    c1.download_button("Download report.md", result.report_md, f"{slug}-report.md", "text/markdown")
    c2.download_button("Download findings.json", json.dumps(d.model_dump(mode="json"), indent=2),
                       f"{slug}-findings.json", "application/json")


st.title("🔎 Repo Reviewer")
st.write(
    "A team of AI agents reviews a public **Python, JavaScript, TypeScript or Go** GitHub repository for "
    "security, code quality and "
    "architecture. A **Verifier** agent re-checks every finding against the real code and drops "
    "hallucinated ones before the report is written."
)

pending = st.session_state.get("pending")
with st.form("review"):
    url = st.text_input("Public GitHub repository URL", placeholder="https://github.com/pallets/itsdangerous")
    # Extra LLM cost: visitors only get this option if the operator enabled it.
    bug_hunter = config.ENABLE_BUG_HUNTER and st.checkbox("Also run the experimental Bug Hunter agent")
    submitted = st.form_submit_button("Review", type="primary", disabled=pending is not None)

if submitted and url.strip() and pending is None:
    st.session_state.pop("result", None)  # never show the previous repo's result under a new request
    st.session_state.pop("message", None)
    st.session_state.pending = {"url": url.strip(), "bug_hunter": bool(bug_hunter)}
    st.rerun()

if pending is not None:
    try:
        outcome = review(pending["url"], pending["bug_hunter"])
    finally:
        st.session_state.pop("pending", None)
    if outcome.result:
        st.session_state.result = outcome.result
    if outcome.message:
        st.session_state.message = (outcome.level, outcome.message)
    st.rerun()

if "message" in st.session_state:
    level, text = st.session_state.message
    (st.error if level == "error" else st.warning)(text)
if "result" in st.session_state:
    show_result(st.session_state.result)

with st.expander("Limitations"):
    st.markdown(
        f"- Python, JavaScript, TypeScript and Go (other languages get regex checks only).\n"
        f"- Up to {config.MAX_FILES} files are reviewed; very large files are partly reviewed.\n"
        f"- Free-tier AI limits: about {config.APP_DAILY_RUN_CAP} fresh reviews per day for all visitors; "
        "repeated requests for the same commit are served from cache.\n"
        "- Findings are AI-generated and verified, but not guaranteed. Always check before acting."
    )
