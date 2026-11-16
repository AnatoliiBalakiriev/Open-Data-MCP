"""Entry point for `mcp dev` (MCP Inspector), which needs a module-level server object.

    OPEN_DATA_OFFLINE=1 uv run --with "mcp[cli]" mcp dev src/open_data_mcp/dev.py

With OPEN_DATA_OFFLINE=1 the server uses the SYNTHETIC fixtures and needs no network; without
it, it talks to the live portal configured by the OPEN_DATA_* environment variables.
"""

import os

from open_data_mcp.server import build_service, create_server

server = create_server(build_service(offline_fixtures=os.environ.get("OPEN_DATA_OFFLINE") == "1"))
