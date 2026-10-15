# Repo Reviewer: Multi-Agent AI Code Review System

A step-by-step build guide. Zero budget, free tools only, portfolio-grade.

---

## 1. Project Summary

**What it does:** You paste a public GitHub repo URL. A team of AI agents analyzes the code and produces a professional report: architecture summary, security issues, code quality issues, a score, and a prioritized fix list.

**Key differentiator:** A **Verifier agent** re-checks every finding against the real code and removes hallucinated issues. You will measure this with real numbers (precision before and after).

**Success metric:** On 10 test repos, at least 80% of reported findings are real (precision >= 0.8) after verification.

---

## 2. Architecture

```
                    +------------------+
   GitHub URL ----> |   Orchestrator   |
                    +--------+---------+
                             | plans + assigns files
          +------------------+------------------+
          v                  v                  v
   +-------------+   +---------------+   +----------------+
   |  Architect  |   |   Security    |   |    Quality     |
   |             |   |    Auditor    |   |    Reviewer    |
   +------+------+   +-------+-------+   +--------+-------+
          |                  |                    |
          +------------------+--------------------+
                             v
                    +------------------+
                    |     Verifier     |  drops false findings
                    +--------+---------+
                             v
                    +------------------+
                    |  Report Writer   |
                    +--------+---------+
                             v
                     report.md + score
```

**Design rules**
- Each agent has ONE role, ONE prompt, ONE JSON output schema.
- Agents never talk to each other. They only talk to the orchestrator via structured JSON.
- Static tools (bandit, ruff) provide grounded evidence. LLMs interpret it, they do not invent it.
- Hard limits everywhere: max files, max tokens, max retries, timeouts.

---

## 3. Agent Specifications

### 3.1 Orchestrator
- **Input:** repo file tree, file sizes, detected languages.
- **Output (JSON):**
```json
{
  "languages": ["python"],
  "assignments": {
    "architect": ["README.md", "src/main.py"],
    "security": ["src/auth.py", "src/db.py"],
    "quality": ["src/utils.py", "src/api.py"]
  }
}
```
- **Logic:** mostly plain Python (filter, rank by size and importance). Use the LLM only to pick important files from the tree. Cap at 50 files total.

### 3.2 Architect
- **Goal:** explain what the project is, its modules, entry points, data flow, dependencies.
- **Output:**
```json
{
  "summary": "string",
  "modules": [{"name": "string", "purpose": "string"}],
  "entry_points": ["string"],
  "dependencies": ["string"]
}
```

### 3.3 Security Auditor
- **Inputs:** assigned files + `bandit` output + regex scan results (secrets, `eval`, `exec`, SQL string concatenation, `subprocess` with `shell=True`).
- **Output:**
```json
{
  "findings": [
    {
      "id": "SEC-1",
      "file": "src/db.py",
      "line": 42,
      "severity": "high",
      "issue": "SQL query built with string concatenation",
      "evidence": "exact code snippet",
      "fix": "Use parameterized queries"
    }
  ]
}
```

### 3.4 Quality Reviewer
- **Inputs:** assigned files + `ruff` output.
- **Checks:** duplication, long functions, missing tests, missing docs, poor naming, dead code.
- **Output:** same schema as Security (ids `QUAL-1`, ...).

### 3.5 Verifier (your standout feature)
- **Input:** one finding + the actual file content around the cited line (plus or minus 15 lines).
- **Question to the model:** "Does this exact code contain the described problem? Answer JSON: `{verdict: confirmed|rejected, reason}`."
- **Pre-check in plain Python:** reject automatically if the file does not exist, the line is out of range, or the `evidence` snippet is not found in the file.
- **Output:** list of confirmed findings plus a rejected count (used in your metrics).

### 3.6 Report Writer
- **Input:** Architect output + confirmed findings.
- **Output:** a Markdown report with: overview, architecture, findings grouped by severity, score (0 to 100), top 5 fixes.
- **Score formula (deterministic, in Python, not LLM):**
  `score = 100 - (high*10 + medium*4 + low*1)`, floor at 0.

---

## 4. Free Tech Stack

| Need | Tool | Cost |
|---|---|---|
| LLM | Google Gemini API free tier (aistudio.google.com) | Free |
| LLM alternative | Groq free tier, or Ollama (Qwen2.5-Coder / Llama 3.1 8B) | Free |
| Language | Python 3.11+ | Free |
| Agent loop | Plain Python (no framework) | Free |
| Parallelism | `concurrent.futures.ThreadPoolExecutor` | Free |
| Repo access | `git clone --depth 1` | Free |
| Static analysis | `bandit`, `ruff` | Free |
| Validation | `pydantic` | Free |
| UI | Streamlit | Free |
| Hosting | Streamlit Community Cloud or Hugging Face Spaces | Free |
| Code hosting | GitHub | Free |
| Tests | `pytest` | Free |

**Why no framework:** writing your own loop means you can explain every line in an interview. Add LangGraph later only if you want a comparison section in the README.

---

## 5. Folder Structure

```
repo-reviewer/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── app.py                  # Streamlit UI
├── src/
│   ├── config.py           # limits, model names, env loading
│   ├── llm.py              # single wrapper around the LLM API
│   ├── repo.py             # clone, list, filter, read files
│   ├── tools/
│   │   ├── static_scan.py  # runs bandit + ruff, parses output
│   │   └── patterns.py     # regex secret / unsafe-pattern scan
│   ├── schemas.py          # pydantic models for every agent output
│   ├── agents/
│   │   ├── base.py         # shared agent runner (prompt, call, validate, retry)
│   │   ├── architect.py
│   │   ├── security.py
│   │   ├── quality.py
│   │   ├── verifier.py
│   │   └── writer.py
│   ├── orchestrator.py     # plan, run in parallel, verify, report
│   ├── scoring.py          # deterministic score
│   └── logger.py           # JSONL trace log
├── prompts/                # one .txt per agent
├── evals/
│   ├── repos.json          # test repos + known issues
│   ├── run_evals.py
│   └── results.md
├── tests/
└── docs/
    ├── architecture.png
    └── demo.gif
```

---

## 6. Setup (Day 1)

```bash
mkdir repo-reviewer && cd repo-reviewer
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install google-generativeai pydantic streamlit bandit ruff python-dotenv pytest
pip freeze > requirements.txt
git init
```

`.env`
```
GEMINI_API_KEY=your_key_here
```

`.gitignore`
```
.venv/
.env
__pycache__/
workdir/
logs/
```

Get the key at https://aistudio.google.com (free). **Never commit `.env`.**

---

## 7. Build Plan

### Week 1: One agent works end to end
- [ ] `repo.py`: clone with `--depth 1` into `workdir/`, list files, skip `node_modules`, `.git`, binaries, lockfiles, files over 100 KB.
- [ ] `llm.py`: one function `call_llm(system, user) -> str` with retry (3 tries, exponential backoff) and a timeout.
- [ ] `schemas.py`: pydantic model for the Architect output.
- [ ] `agents/base.py`: call LLM, extract JSON, validate with pydantic, retry once on invalid JSON.
- [ ] `agents/architect.py` works on 5 files.
- [ ] **Done when:** `python -m src.agents.architect <repo_url>` prints valid JSON.

### Week 2: Specialists + grounded tools
- [ ] `static_scan.py`: run `bandit -r -f json` and `ruff check --output-format json`, parse results into a common finding format.
- [ ] `patterns.py`: regexes for API keys, passwords, `eval(`, `exec(`, `shell=True`, SQL concatenation.
- [ ] Build `security.py` and `quality.py`. Pass tool output into the prompt as evidence.
- [ ] Run the three agents in parallel with `ThreadPoolExecutor`.
- [ ] **Done when:** all three agents return validated JSON for one repo in under 2 minutes.

### Week 3: Orchestrator + Verifier + Report
- [ ] `orchestrator.py`: file ranking, assignment, chunking so no prompt exceeds your token cap.
- [ ] `verifier.py`: plain-Python pre-checks first, then LLM check. Count confirmed vs rejected.
- [ ] `scoring.py` and `writer.py`: generate `report.md`.
- [ ] `logger.py`: write every LLM call (agent, prompt size, latency, result) to `logs/trace.jsonl`.
- [ ] **Done when:** one command produces a full report for any small public repo.

### Week 4: Evals, UI, polish
- [ ] Pick 10 repos (a mix of small, messy, and clean). For each, manually list 3 to 5 real issues.
- [ ] `run_evals.py`: run the system with Verifier OFF, then ON. Record precision and recall for both.
- [ ] Streamlit UI: URL input, live per-agent progress, report display, download button, trace viewer.
- [ ] Deploy to Streamlit Community Cloud (add the key in its Secrets settings).
- [ ] Write the README (section 11).

---

## 8. Prompt Templates (starting points)

**Security Auditor (system):**
```
You are a security code reviewer. You will receive source files and static
analysis output. Report ONLY issues you can point to in the provided code.
Every finding MUST include the exact file path, line number, and a verbatim
code snippet as evidence. If you are not sure, do not report it.
Return ONLY valid JSON matching this schema: {schema}
```

**Verifier (system):**
```
You are a strict fact-checker. You receive one claimed issue and the actual
code around the cited line. Decide if the code truly contains this issue.
Be skeptical. If the evidence is missing or the code is safe, reject it.
Return ONLY JSON: {"verdict": "confirmed" | "rejected", "reason": "..."}
```

Tips: tell agents they are allowed to return an empty list, and keep temperature low (0 to 0.2).

---

## 9. Guardrails and Limits (`config.py`)

```python
MAX_FILES = 50
MAX_FILE_BYTES = 100_000
MAX_PROMPT_CHARS = 30_000
LLM_TIMEOUT_S = 60
LLM_RETRIES = 3
ALLOWED_HOSTS = {"github.com"}     # reject any other URL
REPO_CLONE_TIMEOUT_S = 60
```

- Validate the URL is a public `https://github.com/<owner>/<repo>` before cloning.
- Never execute code from the analyzed repo. Read only.
- Delete the cloned folder after each run.
- Cache results by `repo_url + commit_hash` to save free-tier quota.

---

## 10. Evaluation Method

For each of 10 repos:
1. Manually write the ground-truth issues (file, issue type).
2. Run the system with Verifier OFF, then ON.
3. For each reported finding, mark it real or false.

**Metrics:**
- Precision = real findings / total findings reported
- Recall = ground-truth issues found / total ground-truth issues
- Rejection rate = findings dropped by the Verifier / total raw findings
- Average runtime and number of LLM calls per repo

Put the results in a table in the README. Honest numbers beat impressive-looking claims.

| Config | Precision | Recall | Avg time |
|---|---|---|---|
| No Verifier | ? | ? | ? |
| With Verifier | ? | ? | ? |

---

## 11. README Checklist (portfolio)

- [ ] One-line pitch plus a demo GIF at the very top
- [ ] Live demo link
- [ ] Architecture diagram
- [ ] The results table from section 10
- [ ] "Design decisions": why plain Python, why a Verifier, why deterministic scoring
- [ ] "What failed and what I changed" (at least 2 real examples)
- [ ] Limitations stated honestly (language support, repo size, free-tier rate limits)
- [ ] How to run locally in 4 commands
- [ ] Future work (more languages, GitHub Action, PR comments)

---

## 12. Common Pitfalls

| Problem | Fix |
|---|---|
| Free-tier rate limit errors | Retry with backoff, cache results, lower parallelism |
| Model returns broken JSON | Pydantic validation plus one repair retry |
| Hallucinated line numbers | Verifier pre-check in plain Python |
| Huge repos blow the context | File cap, ranking, chunking |
| Slow runs | Parallel agents, skip files static tools already cleared |
| Leaked API key | `.env`, `.gitignore`, rotate the key if exposed |

---

## 13. Stretch Goals (only after everything above works)

1. Support JavaScript/TypeScript (add `eslint`).
2. Run with a local Ollama model and compare quality against Gemini in your eval table.
3. Package as a GitHub Action that comments on pull requests.
4. Add a "fix suggestion" agent that proposes diffs, with a human approval step.

---

## 14. Definition of Done

- [ ] Paste a URL, get a verified report in under 3 minutes
- [ ] Verifier measurably improves precision
- [ ] Evals run with one command
- [ ] Deployed publicly, free
- [ ] README with GIF, diagram, and results table
- [ ] No secrets in git history
