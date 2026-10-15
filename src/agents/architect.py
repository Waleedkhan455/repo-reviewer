"""Architect agent: what the project is, its modules, entry points and dependencies."""

import sys

from src import config
from src.agents.base import load_prompt, new_nonce, run_agent, wrap_file
from src.repo import Snapshot
from src.schemas import ArchitectOut


def _file_tree(snapshot: Snapshot) -> str:
    paths = [f.rel for f in snapshot.files]
    tree = "\n".join(paths[: config.MAX_TREE_LINES])
    if len(paths) > config.MAX_TREE_LINES:
        tree += f"\n... and {len(paths) - config.MAX_TREE_LINES} more files"
    return tree


def run(snapshot: Snapshot, files: list[str]) -> ArchitectOut:
    nonce = new_nonce()
    budget = config.MAX_PROMPT_CHARS
    tree = f"<tree_{nonce}>\n{_file_tree(snapshot)}\n</tree_{nonce}>"
    parts = [f"File tree:\n{tree}\n\nKey files (only the start of each):\n"]
    budget -= len(parts[0])
    for rel in files:
        head = snapshot.text(rel)[: config.ARCHITECT_FILE_CHARS]
        block = wrap_file(rel, head, nonce)
        if len(block) > budget:
            break
        parts.append(block)
        budget -= len(block)
    return run_agent("architect", load_prompt("architect", NONCE=nonce), "\n\n".join(parts), ArchitectOut)


def fallback(snapshot: Snapshot) -> ArchitectOut:
    """Used when the LLM is unavailable, so a report can still be produced."""
    return ArchitectOut(summary=f"Architecture summary unavailable. The repo has {len(snapshot.files)} "
                                f"reviewable files, {len(snapshot.code_files())} of them source code.")


if __name__ == "__main__":
    # Week-1 checkpoint: python -m src.agents.architect https://github.com/<owner>/<repo>
    from src import repo

    ref = repo.canonicalize(sys.argv[1])
    repo.check_repo_size(ref)
    with repo.cloned_repo(ref) as root:
        snap = repo.Snapshot(root, repo.list_files(root))
        picks = [f.rel for f in snap.files if f.rel.lower().startswith("readme")] + \
                [f.rel for f in snap.code_files()][:5]
        print(run(snap, picks).model_dump_json(indent=2))
