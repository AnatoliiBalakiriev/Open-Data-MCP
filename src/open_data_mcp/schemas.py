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


# --------------------------------------------------------------------------- profile_dataset


class Coverage(_Model):
    mode: str = Field(description="'full', or 'prefix_sample' when the resource exceeds limits.")
    complete: bool
    rows_profiled: int
    rows_in_resource: int | None = Field(description="Known only when the whole resource was read.")
    bytes_read: int | None
    resource_size_bytes: int | None = Field(description="Size declared by the file server.")
    note: str


class Issue(_Model):
    severity: str = Field(description="high | medium | low | info")
    check: str = Field(description="Stable identifier, e.g. broken_dates, mojibake.")
    column: str | None
    message: str


class NumericOutliers(_Model):
    rule: str
    low_fence: float
    high_fence: float
    mild: int
    extreme: int
    examples: list[float]


class NumericProfile(_Model):
    count: int
    min: float
    q1: float
    median: float
    q3: float
    max: float
    outliers: NumericOutliers | None
    outliers_skipped: str | None


class DateProfile(_Model):
    date_like: int
    valid: int
    impossible: int = Field(description="Date-shaped but not on the calendar, e.g. 31.02.2023.")
    ambiguous_two_digit_year: int
    out_of_range: int
    sentinel: int = Field(description="Valid dates equal to 1900-01-01 or 1970-01-01.")
    not_a_date: int
    layouts: dict[str, int]
    mixed_layouts: bool
    earliest: str | None
    latest: str | None
    assumption: str
    valid_year_range: list[int]
    examples: dict[str, list[str]]


class TextDamage(_Model):
    padded_values: int
    replacement_character_values: int
    control_character_values: int
    mojibake_values: int
    mojibake_examples: list[str]


class Repeats(_Model):
    rows_beyond_first: int
    examples: list[str]


class ColumnProfile(_Model):
    name: str
    kind: str = Field(
        description="empty | date | integer | decimal | numeric_with_text | text (inferred)."
    )
    non_missing: int
    empty: int
    placeholders: int
    placeholder_values: dict[str, int]
    missing_share: float = Field(description="(empty + placeholders) / rows, 0..1.")
    distinct: int
    constant: bool
    numeric: NumericProfile | None
    non_numeric_values: int | None
    non_numeric_examples: list[str]
    dates: DateProfile | None
    text_damage: TextDamage | None
    repeats: Repeats | None = None


class KeyCandidate(_Model):
    column: str
    rows_beyond_first: int
    examples: list[str]


class DuplicateInfo(_Model):
    exact_duplicate_rows: int | None
    exact_duplicate_share: float | None
    duplicated_groups: int | None
    note: str
    key_candidates: list[KeyCandidate]


class SparseRows(_Model):
    threshold: float
    count: int
    share: float
    note: str


class EncodingInfo(_Model):
    bom: bool
    detected: str
    lines: int
    non_ascii_lines: int
    utf8_lines: int
    windows1251_lines: int
    undecodable_lines: int
    replacement_characters: int
    control_characters: int
    note: str


class Rename(_Model):
    from_: str = Field(alias="from")
    to: str
    reason: str

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class TypeChange(_Model):
    column: str
    reference_kind: str | None
    other_kind: str | None


class VersionDiff(_Model):
    resource_id: str | None
    name: str | None
    rows_sampled: int
    same_schema: bool
    columns_added: list[str]
    columns_removed: list[str]
    possible_renames: list[Rename]
    column_order_changed: bool
    type_changes: list[TypeChange]


class SkippedVersion(_Model):
    resource_id: str | None
    name: str | None
    reason: str


class SchemaDrift(_Model):
    basis: str
    compared: list[VersionDiff]
    skipped: list[SkippedVersion]
    skipped_total: int


class ProfileResponse(_Model):
    ok: bool
    error: ErrorInfo | None = None
    dataset_id: str | None = None
    dataset_title: str | None = None
    dataset_url: str | None = None
    resource_id: str | None = None
    resource_name: str | None = None
    resources: list[ResourceListing] = Field(default_factory=list)
    note: str | None = None
    coverage: Coverage | None = None
    rows: int | None = None
    columns_total: int | None = None
    columns_profiled: int | None = None
    malformed_rows: int | None = Field(
        default=None, description="CSV lines that did not fit the table (null for non-CSV)."
    )
    issue_counts: dict[str, int] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list, description="Ranked findings, worst first.")
    duplicates: DuplicateInfo | None = None
    sparse_rows: SparseRows | None = None
    columns: list[ColumnProfile] = Field(default_factory=list)
    encoding: EncodingInfo | None = None
    schema_drift: SchemaDrift | None = None
    notes: list[str] = Field(default_factory=list)
    method_note: str | None = None
    source: SourceInfo | None = Field(
        default=None, description="Provenance; always present when a profile is returned."
    )
    citation: str | None = None
