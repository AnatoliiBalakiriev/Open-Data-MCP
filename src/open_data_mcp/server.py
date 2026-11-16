"""MCP server over stdio: four read-only tools, a schema resource template and a prompt."""

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
from .schemas import ProfileResponse, QueryResponse, SchemaResponse, SearchResponse
from .service import OpenDataService, check_id, safe_call

log = logging.getLogger("open_data_mcp")

SCHEMA_URI_TEMPLATE = "opendata://dataset/{dataset_id}/schema"

INSTRUCTIONS = (
    "Tools for Ukrainian open data (CKAN portals). Workflow: search_datasets -> get_schema -> "
    "query; use profile_dataset for a data-quality check before trusting a table. The resource "
    f"template {SCHEMA_URI_TEMPLATE} reads a dataset's schema by URI, and the prompt "
    "data_quality_report(dataset_id) runs a full quality review. Query results and profiles "
    "always include a `source` object; cite it. `query` runs read-only SQL against one table "
    "named `data`; quote column names that contain spaces or non-ASCII characters with double "
    "quotes. Never guess numbers: if a tool fails, say so."
)

DATA_QUALITY_PROMPT = """\
Prepare a data-quality report for the open dataset `{dataset_id}`.

1. Call the `profile_dataset` tool with dataset_id="{dataset_id}". If the result has no `columns`
   but lists `resources`, choose the queryable resource that is most likely the main table, say
   which one you chose and why, and call `profile_dataset` again with its resource_id. If no
   resource is queryable, say so and stop.
2. Base every statement on the tool result and quote its numbers. Never invent or round away a
   finding. Outliers are candidates to review, not proven errors. If the tool returns ok=false,
   report the error and stop.
3. Write the report with these sections:
   - Verdict: one sentence saying whether the data can be used as it is, with fixes, or not at
     all, justified by the high-severity issues.
   - Key issues: from `issues`, worst first, each with its column and numbers.
   - Columns: a table of column, kind and missing share, with a short note where something is
     off.
   - Coverage: if `coverage.complete` is false, say that only the first rows of a larger file
     were profiled (give the number) and that the results describe that part only.
   - Schema drift: what `schema_drift` found, or that sibling files could not be compared.
   - Source: the `citation` text.
4. Answer in the language the user writes in."""

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

    @server.tool(
        annotations=_READ_ONLY,
        description=(
            "Data-quality passport of one tabular resource. Use it when the user asks whether a "
            "dataset can be trusted, or before relying on it for numbers. Returns ranked `issues` "
            "plus per-column share of missing values (empty cells and placeholders like 'null' "
            "or 'н/д'), exact duplicate rows, repeated identifiers, outliers in numeric "
            "columns, broken dates (impossible, not a date, mixed formats), text damage "
            "(mojibake, padded values), file encoding and schema drift against sibling CSV files "
            "of the same dataset. Downloads the file; takes seconds. Unlike query, a file over "
            f"{mb} MB or {cfg.max_table_rows} rows is profiled on its first rows only: check "
            "`coverage.complete` and say so when it is false. Heuristic: outliers and thresholds "
            "are candidates to review, see `method_note`. Read-only."
        ),
    )
    def profile_dataset(
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
                description="A queryable resource_id from get_schema. Omit it to list the "
                "dataset's resources (or to profile the only queryable one)."
            ),
        ] = None,
        compare_versions: Annotated[
            bool,
            Field(
                description="Also compare the header and column types with up to "
                f"{cfg.drift_max_resources} sibling CSV files of the dataset (reads only the "
                f"first {cfg.drift_prefix_bytes // 1024} KB of each). Set false to skip."
            ),
        ] = True,
    ) -> ProfileResponse:
        return ProfileResponse.model_validate(
            _guard(service.profile_dataset, dataset_id, resource_id, compare_versions)
        )

    @server.resource(
        SCHEMA_URI_TEMPLATE,
        name="dataset_schema",
        title="Dataset schema",
        description=(
            "Schema of a dataset's table, read by URI. Same JSON as the get_schema tool called "
            "without resource_id: when the dataset has exactly one queryable resource, its "
            "columns, types, row count and sample rows; otherwise the list of resources to pick "
            "from. Includes a `source` object. Failures come back as ok=false, never as numbers."
        ),
        mime_type="application/json",
    )
    def dataset_schema(dataset_id: str) -> str:
        result = SchemaResponse.model_validate(_guard(service.get_schema, dataset_id))
        return result.model_dump_json(by_alias=True)

    @server.prompt(
        name="data_quality_report",
        title="Data-quality report",
        description=(
            "Review the quality of an open dataset: runs profile_dataset and writes a report "
            "with a verdict, ranked issues, per-column missing values, coverage, schema drift "
            "and the source."
        ),
    )
    def data_quality_report(
        dataset_id: Annotated[
            str,
            Field(description="The dataset_id (or name) to review, e.g. from search_datasets."),
        ],
    ) -> str:
        return DATA_QUALITY_PROMPT.format(dataset_id=check_id(dataset_id, "dataset_id"))

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
