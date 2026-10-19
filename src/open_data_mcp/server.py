"""MCP server exposing three read-only tools over stdio."""

# No `from __future__ import annotations`: parameter descriptions below are built from runtime
# settings, and the MCP SDK needs the annotations evaluated, not left as strings.

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
    cfg = service.settings
    mb = cfg.max_download_bytes // (1024 * 1024)
    server = MCPServer("open-data-mcp", instructions=INSTRUCTIONS)

    @server.tool(
        annotations=_READ_ONLY,
        description=(
            "Find datasets in the open-data catalogue by free text (Ukrainian or English). "
            "Use this first, when you do not yet have a dataset_id. Returns up to "
            f"{cfg.search_max_limit} hits with dataset_id, title, a short description, "
            "organization, license, dataset_url, modification date and the formats of the "
            "dataset's resources (look for CSV or JSON; spreadsheets and PDFs cannot be "
            "queried). It returns no columns or data: next call get_schema with a dataset_id "
            "from a hit."
        ),
    )
    def search_datasets(
        query: Annotated[
            str,
            Field(
                description="Free text matched against titles and descriptions, e.g. "
                "'бюджет 2023' or 'air quality'. Not SQL."
            ),
        ],
        limit: Annotated[
            int,
            Field(
                description=f"How many datasets to return, 1-{cfg.search_max_limit}; larger "
                "values are clamped. Default 10."
            ),
        ] = 10,
    ) -> SearchResponse:
        return SearchResponse.model_validate(_guard(service.search_datasets, query, limit))

    @server.tool(
        annotations=_READ_ONLY,
        description=(
            "Inspect a dataset before querying it. Use after search_datasets and before query. "
            "Without resource_id it lists the dataset's resources, marking which are queryable; "
            "if exactly one is queryable it also returns that schema, otherwise pick a "
            "resource_id and call again. With resource_id it returns column names and types, "
            "the row count and 3 sample rows, plus how the schema was obtained: "
            "datastore_metadata (authoritative) or inferred_from_file (a best guess: check "
            "sample_rows and note for junk columns). File resources are downloaded and parsed, "
            f"so a call can take seconds; files over {mb} MB or {cfg.max_table_rows} rows and "
            "non-tabular formats (XLSX, PDF) are refused with an error code. Read-only; no "
            "side effects."
        ),
    )
    def get_schema(
        dataset_id: Annotated[
            str,
            Field(
                description="The dataset_id (or name) of a search_datasets hit. URLs and file "
                "paths are rejected."
            ),
        ],
        resource_id: Annotated[
            str | None,
            Field(
                description="A resource_id from this tool's `resources` list. Omit it to list "
                "the dataset's resources."
            ),
        ] = None,
    ) -> SchemaResponse:
        return SchemaResponse.model_validate(_guard(service.get_schema, dataset_id, resource_id))

    @server.tool(
        annotations=_READ_ONLY,
        description=(
            "Run one read-only SQL SELECT over a single tabular resource (CKAN DataStore, CSV or "
            "JSON), exposed as the table `data`. Use after get_schema, with column names exactly "
            "as it returned them. CTEs are allowed; joins across resources, file or URL access and "
            "any non-SELECT statement are rejected (unsafe_sql). The SQL always runs over the "
            f"entire resource or fails (resources over {mb} MB are refused, never sampled), so "
            f"aggregates are complete. Only the returned rows are capped: default "
            f"{cfg.query_default_limit}, at most {cfg.query_max_limit}, flagged by `truncated`. On "
            "success the result includes a `source` object (portal, dataset, resource, license, "
            "retrieval time): cite it. On failure ok is false with an error code and no rows: do "
            "not state a number."
        ),
    )
    def query(
        dataset_id: Annotated[
            str, Field(description="The dataset_id (or name) the resource belongs to.")
        ],
        resource_id: Annotated[
            str,
            Field(
                description="A queryable resource_id from get_schema; it must belong to dataset_id."
            ),
        ],
        sql: Annotated[
            str,
            Field(
                description="One SELECT over `data`. Double-quote column names containing "
                "spaces or non-ASCII letters. Example: SELECT category, count(*) AS n FROM data "
                "GROUP BY category ORDER BY n DESC"
            ),
        ],
        limit: Annotated[
            int | None,
            Field(
                description=f"Rows to return, 1-{cfg.query_max_limit}; default "
                f"{cfg.query_default_limit}; larger values are clamped. It does not limit what "
                "the SQL scans."
            ),
        ] = None,
    ) -> QueryResponse:
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
