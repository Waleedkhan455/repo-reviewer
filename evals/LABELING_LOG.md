# Labeling Log

**Who labeled:** Claude (AI assistant), at the repo owner's request, on 2026-10-20. Ground truth was reviewed and
confirmed by the repo owner before labeling. These are **AI-made labels, not blind human labels**: treat the
precision numbers as indicative, and spot-check them (every label can be reproduced from the rules below).

**How:** every one of the 72 raw findings was judged against the actual code at the pinned commit (the cited line
plus surrounding context), in a shuffled order, without looking at the Verifier's verdict. The labeler had seen some
of the Verifier's rejections earlier in the project, so this is close to, but not fully, blind.

## Rules applied (on top of [LABELING_RULES.md](LABELING_RULES.md))

| Finding type | Label | Why |
|---|---|---|
| Injection, unsafe deserialization, `exec`/`eval` of request data, weak password hashing, hard-coded signing secrets | real | plausible path to harm |
| SQL built by formatting **hard-coded seed data** (vulpy `bad/db.py`, `bad/db_init.py`) | false | no attacker-controlled input |
| Command built from a **developer CLI argument** (microblog `app/cli.py`) | false | the "attacker" is the person running the command |
| Real bugs: unreachable code, wrong form field, crash on GET, undefined name, leaked temp file, `cur.execute(q, **params)` | real | wrong behaviour a reviewer would want fixed |
| Deep nesting (5 levels, environs `_deserialize`) | real | genuine maintainability cost |
| Missing docstrings, "use a ternary / contextlib.suppress / enumerate", old-style `super()`, unused `e`, misleading-but-harmless names | false | style preference (rule 3) |
| "Too many branches/arguments" on flat dispatch or API functions (pyjwt) | false | reasonable design, not a defect |
| Flask import-at-the-bottom pattern reported as "unused import" | false | standard, intentional pattern |
| Catch-all that returns a default for the operation's **expected** failure (token/cookie/header parsing) | false | intended control flow |
| Catch-all wrapping a whole handler, hiding unrelated errors (Vulnerable-Flask-App login) | real | masks bugs |
| "File opened without a context manager" in DSVW demo lines whose real problem is a different vulnerability | false | the stated issue is trivial and misses the point |
| Vague or wrong description of a real oddity (environs "code after `finally` is unreachable") | false | rule 2: right line, wrong explanation |

## Result

72 findings: **26 real, 46 false**; 11 real findings matched a ground-truth issue (9 of the 18 issues).
Labels are stored in `labels.json` (keyed by finding fingerprint); `python -m evals.run_evals score` recomputes
everything from them.
