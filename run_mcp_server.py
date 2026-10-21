"""Start the Repo Reviewer MCP server from any folder: ``python "<this repo>/run_mcp_server.py" [options]``.

MCP clients start the server inside the project being reviewed. ``python -m src.mcp_server`` would then
import that project's own ``src`` package (Python puts the current folder first on ``sys.path``), running
code from the reviewed repository. Running this file puts this repository's folder first instead.
"""

import sys
from pathlib import Path

sys.path[:] = [str(Path(__file__).resolve().parent), *(p for p in sys.path[1:] if p not in ("", "."))]

from src.mcp_server.__main__ import main  # noqa: E402 - needs the path fix above

if __name__ == "__main__":
    sys.exit(main())
