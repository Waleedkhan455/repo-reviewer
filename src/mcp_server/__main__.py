"""Run the MCP server. MCP clients should start it with ``python "<this repo>/run_mcp_server.py"``.

``python -m src.mcp_server`` works too, but only from this repository's folder: ``-m`` puts the current
folder (for a client, the user's project) first on ``sys.path``, so a project with its own ``src``
package would be imported in place of this one. In stdio mode stdout carries the protocol, so all
logging goes to stderr.
"""

import argparse
import logging
import os
import sys

from src import config
from src.mcp_server.jobs import JobManager
from src.mcp_server.server import build_server

MODES = ("local", "remote")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_mcp_server.py", description="Repo Reviewer MCP server")
    parser.add_argument("--mode", default=config.MCP_MODE, help="local (default) or remote")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    mode = args.mode.strip().lower()
    if mode not in MODES:  # argparse does not check choices against a default taken from RR_MCP_MODE
        parser.error(f"--mode / RR_MCP_MODE must be one of {', '.join(MODES)}, not {args.mode!r}")
    if args.transport == "http":
        parser.error("the HTTP transport arrives in Phase 2; use --transport stdio")
    if mode == "remote":
        parser.error("remote mode needs per-caller quotas and caller ids, which arrive with the HTTP transport")

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    jobs = JobManager()
    try:
        build_server(mode, jobs=jobs).run(transport="stdio")
    finally:
        # The client went away: cancel queued reviews and do not wait for running ones (their results
        # could not be delivered, and waiting would keep spending model quota).
        jobs.shutdown()
        logging.shutdown()
        sys.stderr.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
