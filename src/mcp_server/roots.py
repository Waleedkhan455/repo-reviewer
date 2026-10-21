"""Local mode: which folders may be reviewed, and confinement of requested paths to them.

Allowed roots, first source that provides any wins: the MCP client's roots (``roots/list``), then
``RR_MCP_ROOTS``, then the folder the server was started in. A requested path must resolve (following
symlinks) to a directory inside an allowed root, and its git work tree must be inside that root too.
"""

import urllib.parse
import urllib.request
from pathlib import Path

from src import config
from src.diffs import GIT_SAFE
from src.proc import ProcTimeoutError, run_bounded, safe_env


class PathNotAllowed(Exception):  # noqa: N818 - reads naturally at call sites ("raise PathNotAllowed")
    """The requested path is outside the allowed folders, missing, or not a git work tree."""


def file_uri_to_path(uri: str) -> Path | None:
    """``file:///C:/work/app`` or ``file:///home/me/app`` -> a Path; anything else -> None."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return None
    return Path(urllib.request.url2pathname(parsed.path))  # url2pathname also percent-decodes


def _is_network_path(path: str) -> bool:
    r"""UNC (``\\host\share``, ``//host/share``) and device (``\\?\``, ``\\.\``) paths. Merely
    resolving one makes Windows contact the host and offer the user's credentials, so they are refused
    before any filesystem call."""
    return path.replace("\\", "/").startswith("//")


def _resolved(paths: list[Path]) -> list[Path]:
    out = []
    for root in paths:
        if _is_network_path(str(root)):
            continue
        try:
            out.append(root.resolve(strict=True))
        except (OSError, RuntimeError):
            continue  # a root that does not exist allows nothing
    return out


def _inside(p: Path, roots: list[Path]) -> bool:
    return any(p == root or p.is_relative_to(root) for root in roots)


def pick_roots(client_roots: list[Path], configured: list[str], cwd: Path) -> list[Path]:
    """The allowed roots, resolved. First non-empty source wins: client roots, RR_MCP_ROOTS, then cwd.

    When RR_MCP_ROOTS is set it is also a ceiling: client roots outside it are ignored.
    """
    ceiling = _resolved([Path(p) for p in configured])
    if client_roots:  # the client named its roots: only those count (a missing one allows nothing)
        client = _resolved(client_roots)
        return [r for r in client if _inside(r, ceiling)] if configured else client
    return ceiling or _resolved([cwd])


def _git_toplevel(path: Path) -> Path:
    try:
        res = run_bounded([*GIT_SAFE, "rev-parse", "--show-toplevel"], cwd=path,
                          timeout=config.GIT_CMD_TIMEOUT_S, env=safe_env(GIT_TERMINAL_PROMPT="0"))
    except ProcTimeoutError as exc:
        raise PathNotAllowed("git did not answer in time") from exc
    if res.returncode != 0 or not res.stdout.strip():
        raise PathNotAllowed("not a git repository (local reviews need a git work tree)")
    return Path(res.stdout.strip()).resolve()


def resolve_project(path: str, allowed_roots: list[Path], cwd: Path | None = None) -> Path:
    """The git work tree to review for ``path``, guaranteed to lie inside an allowed root.

    Relative paths are taken relative to ``cwd`` (the folder the server was started in, usually the
    user's project) when it is inside an allowed root, else relative to the first allowed root.
    """
    if not allowed_roots:
        raise PathNotAllowed("no allowed folders are configured")
    if not path or "\0" in path or len(path) > config.MCP_INPUT_MAX_CHARS:
        raise PathNotAllowed("invalid path")
    if _is_network_path(path):
        raise PathNotAllowed("network and device paths are not allowed")
    candidate = Path(path)
    if not candidate.is_absolute():
        here = _resolved([cwd]) if cwd is not None else []
        base = here[0] if here and _inside(here[0], allowed_roots) else allowed_roots[0]
        candidate = base / candidate
    try:
        real = candidate.resolve(strict=True)  # follows symlinks: escapes are caught below
    except (OSError, RuntimeError) as exc:
        raise PathNotAllowed("path does not exist") from exc
    if not real.is_dir():
        raise PathNotAllowed("path is not a folder")

    def inside(p: Path) -> bool:
        return _inside(p, allowed_roots)

    if not inside(real):
        raise PathNotAllowed("path is outside the allowed folders")
    top = _git_toplevel(real)
    if not inside(top):  # e.g. an allowed subfolder of a larger repo: the review would read outside it
        raise PathNotAllowed("the git repository extends outside the allowed folders")
    return top
