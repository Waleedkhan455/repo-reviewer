"""Pydantic models for every agent output. LLM JSON is never used without passing through these."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["high", "medium", "low"]
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_SEVERITY_ALIASES = {
    "critical": "high", "major": "high", "error": "high",
    "moderate": "medium", "warning": "medium", "medium-high": "medium",
    "minor": "low", "info": "low", "informational": "low", "note": "low",
}


# --- Orchestrator ------------------------------------------------------------


class FilePick(BaseModel):
    important_files: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    languages: list[str]
    assignments: dict[str, list[str]]
    skipped: dict[str, list[str]] = Field(default_factory=dict)


# --- Architect ---------------------------------------------------------------


class Module(BaseModel):
    name: str
    purpose: str


class ArchitectOut(BaseModel):
    summary: str
    modules: list[Module] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


# --- Security / Quality --------------------------------------------------------


def normalize_path(value: str) -> str:
    p = str(value).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


class Finding(BaseModel):
    id: str = ""
    file: str
    line: int
    severity: Severity
    issue: str
    evidence: str
    fix: str
    source: str = ""  # set by our code ("security" / "quality"), never trusted from the LLM

    @field_validator("severity", mode="before")
    @classmethod
    def _severity(cls, v: object) -> str:
        s = str(v).strip().lower()
        return _SEVERITY_ALIASES.get(s, s)

    @field_validator("file", mode="before")
    @classmethod
    def _file(cls, v: object) -> str:
        return normalize_path(str(v))

    @field_validator("line", mode="before")
    @classmethod
    def _line(cls, v: object) -> object:
        return 1 if v is None else v  # file-level issue (e.g. Dockerfile, requirements.txt)


class RawItems(BaseModel):
    """Agent reply envelope. Items are validated one by one, so one bad item cannot sink the batch."""

    findings: list[dict] = Field(default_factory=list)
    verdicts: list[dict] = Field(default_factory=list)


# --- Verifier ----------------------------------------------------------------


class Verdict(BaseModel):
    id: str
    verdict: Literal["confirmed", "rejected"]
    reason: str = ""

    @field_validator("verdict", mode="before")
    @classmethod
    def _verdict(cls, v: object) -> str:
        return str(v).strip().lower()


class Rejection(BaseModel):
    finding: Finding
    stage: Literal["precheck", "llm", "error"]
    reason: str


# --- Report writer -------------------------------------------------------------


class TopFix(BaseModel):
    id: str
    why: str


class WriterOut(BaseModel):
    overview: str
    top_fixes: list[TopFix] = Field(default_factory=list)


# --- Pipeline result -------------------------------------------------------------


class Score(BaseModel):
    score: int
    per_kloc: float
    counts: dict[str, int]


class RunStatsOut(BaseModel):
    seconds: float
    llm_calls: int
    llm_errors: int
    llm_seconds: float
    verifier_seconds: float = 0.0
    verifier_calls: int = 0


class RunMeta(BaseModel):
    """Facts about the run that the report header and notes need."""

    files_reviewed: int
    files_selected: int = 0
    loc: int
    seconds: float
    llm_calls: int
    verified: bool
    warnings: list[str] = Field(default_factory=list)


class RejectedOut(BaseModel):
    id: str
    stage: str
    reason: str


class ReviewData(BaseModel):
    """Everything a run produced; saved as findings.json and used by evals, cache and UI."""

    run_id: str
    repo: str
    commit: str
    pipeline_version: str
    model: str
    verified: bool
    bug_hunter: bool = False
    degraded: bool = False  # an agent or scanner failed; such results are never cached
    plan: Plan
    tool_hits: int
    duplicates_dropped: list[str]
    invalid_items_dropped: int
    raw_findings: list[Finding]
    confirmed_ids: list[str]
    rejected: list[RejectedOut]
    architecture: ArchitectOut
    score: Score
    stats: RunStatsOut
    warnings: list[str]
