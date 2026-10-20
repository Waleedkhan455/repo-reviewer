# Repo Reviewer: v1 Fixes Prompt for Claude Code

Paste everything in the **Prompt** section into Claude Code, opened in the `repo-reviewer` folder.

**Your own steps** (Claude Code will stop and wait for you):
- confirm the ground truth
- label the eval findings
- click deploy on Streamlit
- record the demo GIF

---

## Prompt

You are working in my repo-reviewer project. Read README.md, PROJECT_PHASES.md, evals/LABELING_RULES.md,
evals/repos.json and evals/run_evals.py before changing anything.

Goal: finish the v1 "proof" items before I start the MCP server version. Do the tasks in order and stop
at each "STOP" so I can do my part.

### 1. Diagnose the eval results
- evals/results.md shows precision "n/a", recall 0% and "72 findings unlabeled or skipped".
- Confirm from the code whether this is only because the `label` step was never run, or whether there
  is also a bug (for example, ground-truth matching never happening, or recall computed wrongly).
- If there is a bug, fix it with a regression test. Do not change LABELING_RULES.md.

### 2. Check the ground truth
- In evals/repos.json, list every ground-truth item marked "draft, please confirm", and every repo with
  an empty ground_truth that should have one.
- For each draft item, show me the file and line where the issue is so I can confirm it. Do not confirm
  them yourself.
- **STOP:** wait for me to confirm or edit the ground truth.

### 3. Labeling (my job, not yours)
- Do NOT label any findings and do NOT write any labels file yourself. Labeling must be blind and done
  by me.
- Tell me the exact command to run (`python -m evals.run_evals label`), how long it will take for the 72
  findings, and how to resume if I stop halfway. If resuming is not supported, add it.
- **STOP:** wait until I tell you labeling is done.

### 4. Score and update the docs
- Run `python -m evals.run_evals score` so evals/results.md and the README table are regenerated.
- Write a short, honest summary of the results under "Results" in the README. Include whether the
  Verifier improved precision, and by how much. If the numbers are weak, say so and do not spin them.

### 5. Mark finished work
- The GitHub PR bot has now been tested on a real pull request in another repository (workflow added,
  GEMINI_API_KEY secret set, inline comments and summary posted).
- Tick that item in PROJECT_PHASES.md and note it in the README's PR bot section.

### 6. Make deployment ready (I will click deploy myself)
- Check that the app runs on Streamlit Community Cloud as the README describes (packages.txt,
  requirements.txt, Python version, secrets read correctly, no writes outside allowed folders).
- Fix anything that would break there.
- Give me a short checklist of the exact steps I must do in the Streamlit dashboard.
- Before I make anything public, scan the full git history for committed secrets (.env, API keys,
  tokens) and report what you find.

### 7. Prepare for the MCP version (refactor only, no new features)
- pr_review.py imports private functions (`_run_specialists`, `_static_analysis`) from orchestrator.py.
- Create a public `src/service.py` with clear functions that the CLI, Streamlit app, PR bot and a future
  MCP server can all call:
  - review a repo URL
  - review changed lines in a local folder
  - load a cached report
- Move callers to it without changing behaviour.
- All existing tests must still pass. `ruff check .` must be clean.

### Rules
- Keep the existing style: plain Python, no agent frameworks, limits in config.py, tests with no network
  or LLM.
- Run pytest and ruff after each code change.
- Before your final message, run the security-auditor, bug-hunter and code-quality-reviewer agents in
  .claude/agents on what you changed, and fix any high findings.
- End with a short summary of:
  - what you changed
  - what is still left for me
  - the final eval numbers
