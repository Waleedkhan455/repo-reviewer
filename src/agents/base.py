"""Shared agent runner: render prompt -> call LLM -> extract JSON -> validate -> one repair retry."""

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from src import config, llm
from src.logger import log_event
from src.repo import Snapshot
from src.schemas import SEVERITY_ORDER, Finding, RawItems
from src.tools.static_scan import ToolFinding

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
NONCE_PLACEHOLDER = "0" * 8  # same length as new_nonce(); used when only measuring prompt size


class AgentError(Exception):
    """The agent could not produce valid output (LLM failure or invalid JSON twice)."""


def chunk_budget() -> int:
    """Characters of file content that fit in one prompt next to the system prompt and tool evidence."""
    return config.MAX_PROMPT_CHARS - config.PROMPT_OVERHEAD_CHARS


def load_prompt(name: str, **values: str) -> str:
    """Load prompts/<name>.txt and fill {{KEY}} placeholders (no str.format: prompts contain JSON)."""
    text = (config.PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def new_nonce() -> str:
    return secrets.token_hex(4)


def number_lines(text: str, start: int = 1) -> str:
    return "\n".join(f"{i:>5}| {line}" for i, line in enumerate(text.split("\n"), start=start))


def wrap_file(path: str, content: str, nonce: str, changed_lines: str = "") -> str:
    """Wrap untrusted repo content in tags the repo cannot forge (it cannot know the nonce)."""
    attrs = f' changed_lines="{changed_lines}"' if changed_lines else ""
    return f'<file_{nonce} path="{path}"{attrs}>\n{content}\n</file_{nonce}>'


def is_truncated(snapshot: Snapshot, rel: str) -> bool:
    return len(number_lines(snapshot.text(rel))) > chunk_budget()


def visible_lines(snapshot: Snapshot, rel: str) -> int:
    """How many lines of the file the agents actually see (fewer than the file has when truncated)."""
    body = number_lines(snapshot.text(rel))
    if len(body) <= chunk_budget():
        return len(snapshot.lines(rel))
    return body[: chunk_budget()].rsplit("\n", 1)[0].count("\n") + 1


def file_block(snapshot: Snapshot, rel: str, nonce: str) -> str:
    """Line-numbered, wrapped file content, truncated so a single file always fits in one prompt."""
    body = number_lines(snapshot.text(rel))
    if len(body) > chunk_budget():
        body = body[: chunk_budget()].rsplit("\n", 1)[0] + "\n[... file truncated ...]"
    return wrap_file(rel, body, nonce, snapshot.focus.get(rel, ""))


def extract_json(text: str) -> dict:
    """Parse a JSON object from an LLM reply, tolerating code fences and surrounding prose."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        data = json.loads(cleaned, strict=False)  # tolerate raw tabs/newlines inside strings
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(cleaned[start : end + 1], strict=False)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def run_agent(agent: str, system: str, user: str, schema: type[T]) -> T:
    """Call the LLM and return a validated ``schema`` instance, with one repair retry."""
    try:
        raw = llm.call_llm(system, user, agent=agent)
        try:
            return schema.model_validate(extract_json(raw))
        except (ValueError, ValidationError) as exc:
            error = str(exc)[:500]
            log_event("invalid_json", agent=agent, error=error)
        repair = (
            f"{user}\n\nYour previous reply was not valid. Error: {error}\n"
            "Reply again with ONLY a valid JSON object that matches the required schema."
        )
        raw = llm.call_llm(system, repair, agent=agent)
        return schema.model_validate(extract_json(raw))
    except (ValueError, ValidationError, llm.LLMError) as exc:
        log_event("agent_failed", agent=agent, error=f"{type(exc).__name__}: {str(exc)[:300]}")
        raise AgentError(f"{agent} failed: {exc}") from exc


def validate_items(agent: str, items: list[dict], model: type[T]) -> tuple[list[T], int]:
    """Validate list items one by one; return (valid items, number dropped)."""
    valid = []
    for item in items:
        try:
            valid.append(model.model_validate(item))
        except ValidationError as exc:
            log_event("invalid_item", agent=agent, error=str(exc)[:300])
    return valid, len(items) - len(valid)


BatchProgress = Callable[[str, int, int, int], None]  # (agent, batches done, total batches, findings so far)


@dataclass
class ChunkReview:
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reviewed_files: set[str] = field(default_factory=set)  # files in chunks that succeeded
    invalid_items: int = 0


def review_chunks(
    agent: str,
    prefix: str,
    chunks: list[list[str]],
    snapshot: Snapshot,
    tool_hits: list[ToolFinding],
    on_batch: BatchProgress | None = None,
) -> ChunkReview:
    """Review every chunk (batch) in turn, then merge, cap and renumber the findings into one result.

    The LLM rate limiter paces the batches. A failed batch is recorded as an error and does not stop
    the others. ``on_batch(agent, done, total, findings_so_far)`` reports progress after each batch.
    """
    out = ChunkReview()
    for done, chunk in enumerate(chunks, start=1):
        nonce = new_nonce()
        in_chunk = set(chunk)
        hits = [h.as_line() for h in tool_hits if h.file in in_chunk][: config.MAX_TOOL_HITS_PER_CHUNK]
        system = load_prompt(agent, NONCE=nonce, MAX_FINDINGS=str(config.MAX_FINDINGS_PER_CHUNK))
        user = (
            "Static analysis evidence (may be empty; may contain false positives):\n"
             f"<tool_hits_{nonce}>\n" + ("\n".join(hits) or "(none)") + f"\n</tool_hits_{nonce}>"
            + "\n\nFiles to review:\n\n"
            + "\n\n".join(file_block(snapshot, rel, nonce) for rel in chunk)
        )
        try:
            reply = run_agent(agent, system, user, RawItems)
        except AgentError as exc:
            out.errors.append(str(exc))
            if on_batch:
                on_batch(agent, done, len(chunks), len(out.findings))
            continue
        found, dropped = validate_items(agent, reply.findings, Finding)
        # Keep findings about files outside the chunk: the Verifier pre-check rejects and counts them,
        # which keeps the Verifier-OFF eval baseline honest.
        out.findings.extend(found)
        out.invalid_items += dropped
        out.reviewed_files |= in_chunk
        if on_batch:
            on_batch(agent, done, len(chunks), len(out.findings))
    out.findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])  # keep the worst when capping
    out.findings = out.findings[: config.MAX_FINDINGS_PER_AGENT]
    for i, f in enumerate(out.findings, start=1):
        f.id, f.source = f"{prefix}-{i}", agent
    return out
