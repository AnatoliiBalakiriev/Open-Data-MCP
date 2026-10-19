"""MCP server exposing three read-only tools over stdio."""

from __future__ import annotations

import logging
import sys
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from .ckan_client import CkanClient
from .config import Settings
from .errors import OpenDataError
from .schemas import QueryResponse, SchemaResponse, SearchResponse
from .service import OpenDataService, safe_call

log = logging.getLogger("open_data_mcp")

INSTRUCTIONS = (
    "Tools for Ukrainian open data (CKAN portals). Workflow: search_datasets -> get_schema -> "
    "query. Query results always include a `source` object; cite it. `query` runs read-only SQL "
    "against one table named `data`; quote column names that contain spaces or non-ASCII "
    "characters with double quotes. Never guess numbers: if a tool fails, say so."
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)


def _guard(call: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return safe_call(call, *args, **kwargs)
    except Exception:  # unexpected: log details to stderr, return a generic envelope
        log.exception("unexpected error in tool")
        err = OpenDataError("Internal error while running the tool.")
        err.code = "internal_error"
        return {"ok": False, "error": err.to_dict()}


def create_server(service: OpenDataService) -> MCPServer:
    server = MCPServer("open-data-mcp", instructions=INSTRUCTIONS)

    @server.tool(annotations=_READ_ONLY)
    def search_datasets(
        query: Annotated[str, Field(description="Search text, Ukrainian or English.")],
        limit: Annotated[int, Field(description="Max datasets to return (clamped to 20).")] = 10,
    ) -> SearchResponse:
        """Search open-data datasets. Returns IDs, titles, short descriptions, organization,
        license and dataset URL for each hit."""
        return SearchResponse.model_validate(_guard(service.search_datasets, query, limit))

    @server.tool(annotations=_READ_ONLY)
    def get_schema(
        dataset_id: Annotated[str, Field(description="Dataset id or name from search_datasets.")],
        resource_id: Annotated[
            str | None, Field(description="Resource id; optional if the dataset has only one.")
        ] = None,
    ) -> SchemaResponse:
        """Describe a resource's columns and how the schema was obtained (DataStore metadata =
        authoritative; inferred_from_file = best guess). Also lists the dataset's resources."""
        return SchemaResponse.model_validate(_guard(service.get_schema, dataset_id, resource_id))

    @server.tool(annotations=_READ_ONLY)
    def query(
        dataset_id: Annotated[str, Field(description="Dataset id or name.")],
        resource_id: Annotated[str, Field(description="Resource id inside that dataset.")],
        sql: Annotated[
            str, Field(description="One read-only SELECT over the table `data`; CTEs allowed.")
        ],
        limit: Annotated[
            int | None, Field(description="Max rows returned (default 20, hard max 100).")
        ] = None,
    ) -> QueryResponse:
        """Run read-only SQL over one tabular resource (DataStore, CSV or JSON). The SQL always
        covers the whole resource or the call fails; only the returned rows are capped."""
        return QueryResponse.model_validate(
            _guard(service.query, dataset_id, resource_id, sql, limit)
        )

    return server


def build_service(
    settings: Settings | None = None,
    *,
    offline_fixtures: bool = False,
    offline_snapshot: bool = False,
) -> OpenDataService:
    if offline_snapshot:
        from .fakeportal import build_snapshot_service

        return build_snapshot_service()[0]
    if offline_fixtures:
        from .fakeportal import build_offline_service

        return build_offline_service()[0]
    cfg = settings or Settings.from_env()
    return OpenDataService(cfg, CkanClient(cfg))


def run_stdio(*, offline_fixtures: bool = False, offline_snapshot: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    create_server(
        build_service(offline_fixtures=offline_fixtures, offline_snapshot=offline_snapshot)
    ).run("stdio")
