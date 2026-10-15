"""Regex scan for leaked secrets and unsafe calls. Works on any text file, not only Python."""

import re
from dataclasses import dataclass

from src import config
from src.repo import Snapshot
from src.tools.static_scan import ToolFinding


@dataclass(frozen=True)
class Pattern:
    rule: str
    severity: str
    message: str
    regex: re.Pattern
    secret: bool = False


PATTERNS = [
    Pattern("SECRET-AWS", "high", "AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), True),
    Pattern("SECRET-GOOGLE", "high", "Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), True),
    Pattern("SECRET-GITHUB", "high", "GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), True),
    Pattern("SECRET-SLACK", "high", "Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), True),
    Pattern("SECRET-PRIVKEY", "high", "Private key block",
            re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"), True),
    Pattern("SECRET-ASSIGN", "medium", "Hard-coded credential",
            # optional type annotation: `jwt_secret: str = "..."`, `api_key: str | None = "..."`
            re.compile(r"""(?i)\b\w*(?:password|passwd|secret|api_?key|token)\w*\s*"""
                       r"""(?::\s*[\w\[\], .|]+?\s*)?[:=]\s*["'][^"'\s]{6,}["']"""),
            True),
    Pattern("UNSAFE-EVAL", "high", "eval() call", re.compile(r"(?<![\w.])eval\s*\(")),
    Pattern("UNSAFE-EXEC", "high", "exec() call", re.compile(r"(?<![\w.])exec\s*\(")),
    Pattern("UNSAFE-SHELL", "high", "subprocess with shell=True", re.compile(r"\bshell\s*=\s*True\b")),
    Pattern("UNSAFE-OSSYSTEM", "medium", "os.system() call", re.compile(r"\bos\.system\s*\(")),
    Pattern("UNSAFE-PICKLE", "medium", "pickle load of possibly untrusted data",
            re.compile(r"\b(?:c?pickle)\.loads?\s*\(")),
    Pattern("UNSAFE-YAML", "medium", "yaml.load without an explicit Loader",
            re.compile(r"\byaml\.load\s*\((?![^)]*Loader)")),
    Pattern("SQL-CONCAT", "high", "SQL built with string formatting",
            re.compile(r"""(?i)\bexecute(?:many)?\s*\(\s*(?:f["']|["'][^"']*["']\s*(?:%|\+|\.format\b))""")),
    Pattern("SQL-FSTRING", "high", "SQL statement in an f-string",
            re.compile(r"""(?i)\bf["'][^"']*\b(?:SELECT|INSERT|UPDATE|DELETE)\b[^"']*\{""")),
]

_SECRET_PATTERNS = [p.regex for p in PATTERNS if p.secret and p.rule != "SECRET-ASSIGN"]
_ASSIGN_VALUE = re.compile(r"""(?i)((?:password|passwd|secret|api_?key|token)\w*\s*[:=]\s*["'])([^"'\s]{6,})(["'])""")


def mask_secrets(text: str) -> str:
    """Hide secret values before anything is shown in a report (keep a 4-char prefix)."""
    for regex in _SECRET_PATTERNS:
        text = regex.sub(lambda m: m.group(0)[:4] + "****", text)
    return _ASSIGN_VALUE.sub(lambda m: m.group(1) + m.group(2)[:2] + "****" + m.group(3), text)


def scan_patterns(snapshot: Snapshot) -> list[ToolFinding]:
    hits = []
    for f in snapshot.files:
        for lineno, line in enumerate(snapshot.lines(f.rel), start=1):
            if len(line) > config.MAX_PATTERN_LINE_CHARS:
                continue
            for p in PATTERNS:
                if p.regex.search(line):
                    hits.append(ToolFinding("regex", p.rule, f.rel, lineno, p.severity, p.message))
    return hits
