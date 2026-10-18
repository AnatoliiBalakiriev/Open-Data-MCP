"""Pydantic models describing tool outputs; FastMCP publishes them as MCP output schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ErrorInfo(_Model):
    code: str = Field(description="Stable error code, e.g. unsafe_sql, resource_too_large.")
    message: str
    hint: str | None = None


class SourceInfo(_Model):
    portal: str
    dataset_id: str
    dataset_title: str | None
    dataset_url: str | None
    resource_id: str
    resource_name: str | None
    resource_url: str | None
    retrieved_at_utc: str = Field(description="When this server fetched the data (UTC).")
    metadata_retrieved_at_utc: str | None = Field(
        description="When the dataset metadata was fetched; may be older because of the TTL cache."
    )
    dataset_modified_at: str | None = Field(description="Portal's modification date, or null.")
    resource_modified_at: str | None
    license: str | None
    access_method: str = Field(description="'datastore' or 'file_download'.")


class DatasetSummary(_Model):
    dataset_id: str | None
    name: str | None
    title: str | None
    description: str
    organization: str | None
    license: str | None
    dataset_url: str | None
    dataset_modified_at: str | None
    resource_formats: list[str]


class SearchResponse(_Model):
    ok: bool
    error: ErrorInfo | None = None
    portal: str | None = None
    query: str | None = None
    limit_applied: int | None = None
    returned: int | None = None
    retrieved_at_utc: str | None = None
    datasets: list[DatasetSummary] = Field(default_factory=list)


class ResourceListing(_Model):
    resource_id: str | None
    name: str | None
    format: str | None
    queryable: bool
    access: str | None
    note: str


class ColumnSchema(_Model):
    name: str
    type: str | None


class SchemaResponse(_Model):
    ok: bool
    error: ErrorInfo | None = None
    dataset_id: str | None = None
    dataset_title: str | None = None
    dataset_url: str | None = None
    resource_id: str | None = None
    resource_name: str | None = None
    table_name_in_queries: str | None = None
    schema_: list[ColumnSchema] | None = Field(default=None, alias="schema")
    schema_source: str | None = Field(
        default=None, description="'datastore_metadata', 'inferred_from_file' or 'unavailable'."
    )
    authoritative: bool | None = Field(
        default=None, description="False when the schema was only inferred from file contents."
    )
    row_count: int | None = None
    sample_columns: list[str] | None = None
    sample_rows: list[list[Any]] | None = None
    resources: list[ResourceListing] = Field(default_factory=list)
    note: str | None = None
    source: SourceInfo | None = None

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class QueryResponse(_Model):
    ok: bool
    error: ErrorInfo | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    returned_rows: int | None = None
    row_limit: int | None = Field(default=None, description="Server-enforced cap on returned rows.")
    truncated: bool | None = None
    rows_in_resource: int | None = Field(
        default=None, description="Rows in the whole resource the SQL ran over."
    )
    scope_note: str | None = None
    note: str | None = None
    source: SourceInfo | None = Field(
        default=None, description="Provenance; always present when ok is true."
    )
    citation: str | None = Field(
        default=None, description="One-line source note to include in the answer."
    )
