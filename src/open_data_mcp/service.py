"""Orchestration behind the three MCP tools. Pure Python, no MCP types, easy to test."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx

from .ckan_client import CachedValue, CkanClient
from .config import Settings
from .errors import (
    InvalidInput,
    OpenDataError,
    ResourceTooLarge,
    UnsupportedResource,
    UpstreamProtocolError,
)
from .provenance import Source, assert_provenance, build_source, utc_now_iso
from .query_engine import PARTIAL_DATA_HINT, TableSession
from .safe_fetch import Resolver, fetch_bytes, system_resolver

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,199}$")
_NOTES_MAX = 300


def _check_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value.strip()):
        raise InvalidInput(
            f"`{label}` must be a CKAN id or name (letters, digits, '-', '_', '.').",
            hint="Take it from search_datasets / get_schema output; URLs and paths are rejected.",
        )
    return value.strip()


# Portals contain formats typed with Cyrillic look-alike letters (seen live: 'сsv' with Cyrillic с).
_LOOKALIKES = str.maketrans("асеорхуі", "aceopxyi")


def normalize_format(raw: object) -> str:
    return str(raw or "").strip().lower().translate(_LOOKALIKES)


def classify_resource(resource: dict[str, Any]) -> tuple[str | None, str]:
    """Return (kind, reason). kind is 'datastore' | 'csv' | 'json' | None."""
    fmt = normalize_format(resource.get("format"))
    url = str(resource.get("url") or "").lower().split("?")[0]
    if resource.get("datastore_active") is True:
        return "datastore", "CKAN DataStore is active for this resource."
    if fmt == "csv" or (not fmt and url.endswith(".csv")):
        return "csv", "Downloadable CSV."
    if fmt == "json" or (not fmt and url.endswith(".json")):
        return "json", "Downloadable JSON records."
    shown = fmt.upper() or "unknown"
    return None, f"Format {shown} is not queryable (supported: DataStore, CSV, JSON)."


class OpenDataService:
    def __init__(
        self,
        settings: Settings,
        ckan: CkanClient,
        *,
        download_client: httpx.Client | None = None,
        resolver: Resolver = system_resolver,
        utc_now: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.settings = settings
        self.ckan = ckan
        self._download = download_client or httpx.Client(
            timeout=httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s),
            headers={"User-Agent": "open-data-mcp/0.1"},
        )
        self._resolver = resolver
        self._utc_now = utc_now

    # ------------------------------------------------------------------ helpers

    def _dataset_url(self, package: dict[str, Any]) -> str | None:
        name = package.get("name") or package.get("id")
        if not name:
            return None
        return self.settings.dataset_url_template.format(
            base=self.settings.portal_base_url, name=name
        )

    def _find_resource(self, package: dict[str, Any], resource_id: str) -> dict[str, Any]:
        for resource in package.get("resources") or []:
            if resource.get("id") == resource_id:
                return resource  # type: ignore[no-any-return]
        raise InvalidInput(
            f"Resource {resource_id!r} does not belong to dataset {package.get('id')!r}.",
            hint="Call get_schema(dataset_id) to list the dataset's resources.",
        )

    def _source(
        self,
        cached: CachedValue,
        resource: dict[str, Any],
        *,
        retrieved_at: str,
        access_method: str,
    ) -> Source:
        return build_source(
            portal=self.settings.portal_name,
            package=cached.value,
            resource=resource,
            dataset_url=self._dataset_url(cached.value),
            retrieved_at_utc=retrieved_at,
            metadata_retrieved_at_utc=cached.fetched_at_utc,
            access_method=access_method,
        )

    def _load_table(self, resource: dict[str, Any], kind: str) -> tuple[TableSession, str]:
        """Fetch the resource completely (or refuse) and load it into a locked-down session."""
        session = TableSession(self.settings)
        try:
            if kind == "datastore":
                fields, records = self._read_datastore(str(resource["id"]))
                retrieved_at = self._utc_now()
                session.load_records(fields, records)
            else:
                url = str(resource.get("url") or "")
                if not url:
                    raise UnsupportedResource(
                        "Resource has no download URL in the portal metadata."
                    )
                payload = fetch_bytes(
                    url,
                    max_bytes=self.settings.max_download_bytes,
                    client=self._download,
                    resolver=self._resolver,
                    max_redirects=self.settings.max_redirects,
                    trusted_hosts=frozenset(self.settings.trusted_download_hosts),
                )
                retrieved_at = self._utc_now()
                if kind == "csv":
                    session.load_csv(payload)
                else:
                    session.load_json(payload)
        except BaseException:
            session.close()
            raise
        return session, retrieved_at

    def _read_datastore(
        self, resource_id: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        limit = self.settings.max_table_rows
        page_size = min(self.settings.datastore_page_size, limit)
        fields: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.ckan.datastore_search(resource_id, limit=page_size, offset=offset)
            if not fields:
                fields = [f for f in page["fields"] if isinstance(f, dict) and "id" in f]
            batch = page.get("records")
            if not isinstance(batch, list):
                raise UpstreamProtocolError("datastore_search returned no `records` list.")
            records.extend(batch)
            if len(records) > limit:
                raise ResourceTooLarge(
                    f"DataStore resource has more than {limit} rows.",
                    hint=PARTIAL_DATA_HINT,
                )
            total = page.get("total")
            offset += len(batch)
            if not batch or (isinstance(total, int) and offset >= total):
                break
        return fields, records

    # ------------------------------------------------------------------ tools

    def search_datasets(self, query: str, limit: int = 10) -> dict[str, Any]:
        if not isinstance(query, str):
            raise InvalidInput("`query` must be a string.")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise InvalidInput("`limit` must be an integer.")
        effective = max(1, min(limit, self.settings.search_max_limit))
        packages = self.ckan.search_packages(query.strip(), limit=effective)
        datasets = []
        for pkg in packages:
            org = pkg.get("organization") if isinstance(pkg.get("organization"), dict) else {}
            notes = str(pkg.get("notes") or "").strip()
            resources = pkg.get("resources") or []
            datasets.append(
                {
                    "dataset_id": pkg.get("id"),
                    "name": pkg.get("name"),
                    "title": pkg.get("title"),
                    "description": notes[:_NOTES_MAX] + ("…" if len(notes) > _NOTES_MAX else ""),
                    "organization": (org or {}).get("title"),
                    "license": pkg.get("license_title") or pkg.get("license_id"),
                    "dataset_url": self._dataset_url(pkg),
                    "dataset_modified_at": pkg.get("metadata_modified"),
                    "resource_formats": sorted(
                        {str(r.get("format")).upper() for r in resources if r.get("format")}
                    ),
                }
            )
        return {
            "portal": self.settings.portal_name,
            "query": query,
            "limit_applied": effective,
            "returned": len(datasets),
            "retrieved_at_utc": self._utc_now(),
            "datasets": datasets,
        }

    def get_schema(self, dataset_id: str, resource_id: str | None = None) -> dict[str, Any]:
        dataset_id = _check_id(dataset_id, "dataset_id")
        cached = self.ckan.package_show(dataset_id)
        resources = cached.value.get("resources") or []
        listing = []
        for r in resources:
            kind, reason = classify_resource(r)
            listing.append(
                {
                    "resource_id": r.get("id"),
                    "name": r.get("name"),
                    "format": r.get("format"),
                    "queryable": kind is not None,
                    "access": kind,
                    "note": reason,
                }
            )
        if resource_id is None:
            queryable = [r for r in listing if r["queryable"]]
            if len(queryable) != 1:
                return {
                    "dataset_id": cached.value.get("id"),
                    "dataset_title": cached.value.get("title"),
                    "dataset_url": self._dataset_url(cached.value),
                    "schema": None,
                    "schema_source": "unavailable",
                    "resources": listing,
                    "note": (
                        "No resource_id given and the dataset does not have exactly one "
                        "queryable resource. Choose a queryable resource_id."
                    ),
                }
            resource_id = str(queryable[0]["resource_id"])
        else:
            resource_id = _check_id(resource_id, "resource_id")
        resource = self._find_resource(cached.value, resource_id)
        kind, reason = classify_resource(resource)
        if kind is None:
            raise UnsupportedResource(reason, hint="Use a CSV, JSON or DataStore resource.")

        if kind == "datastore":
            page = self.ckan.datastore_search(resource_id, limit=0)
            retrieved_at = self._utc_now()
            fields = [
                {"name": f["id"], "type": f.get("type")}
                for f in page["fields"]
                if isinstance(f, dict) and "id" in f
            ]
            schema_source, authoritative = "datastore_metadata", True
            row_count = page.get("total") if isinstance(page.get("total"), int) else None
            sample: list[list[Any]] | None = None
            sample_columns: list[str] | None = None
            note = "Types come from CKAN DataStore field metadata."
        else:
            session, retrieved_at = self._load_table(resource, kind)
            with session:
                fields = [{"name": c.name, "type": c.type} for c in session.columns]
                preview = session.query(f"SELECT * FROM data LIMIT {3}", 3)
                row_count = session.row_count
            sample, sample_columns = preview.rows, preview.columns
            schema_source, authoritative = "inferred_from_file", False
            degenerate = len(fields) == 1 and any(ch in fields[0]["name"] for ch in ";,\t|")
            note = (
                "Types were inferred by DuckDB from the full file; the portal did not publish a "
                "schema, so treat them as a best guess (dates may be read as text, and so on)."
            )
            if degenerate:
                note += (
                    " WARNING: the file parsed into a single column whose name contains delimiter "
                    "characters; it is probably not a clean table (e.g. a spreadsheet export with "
                    "title rows). Do not trust queries on it without checking sample_rows."
                )
        source = self._source(
            cached,
            resource,
            retrieved_at=retrieved_at,
            access_method="datastore" if kind == "datastore" else "file_download",
        )
        return {
            "dataset_id": cached.value.get("id"),
            "resource_id": resource_id,
            "resource_name": resource.get("name"),
            "table_name_in_queries": "data",
            "schema": fields,
            "schema_source": schema_source,
            "authoritative": authoritative,
            "row_count": row_count,
            "sample_columns": sample_columns,
            "sample_rows": sample,
            "resources": listing,
            "note": note,
            "source": source.to_dict(),
        }

    def query(
        self, dataset_id: str, resource_id: str, sql: str, limit: int | None = None
    ) -> dict[str, Any]:
        dataset_id = _check_id(dataset_id, "dataset_id")
        resource_id = _check_id(resource_id, "resource_id")
        # Validate SQL before any network access, so unsafe input never triggers a download.
        from .query_engine import validate_select

        validate_select(sql)
        cached = self.ckan.package_show(dataset_id)
        resource = self._find_resource(cached.value, resource_id)
        kind, reason = classify_resource(resource)
        if kind is None:
            raise UnsupportedResource(reason, hint="Use a CSV, JSON or DataStore resource.")
        session, retrieved_at = self._load_table(resource, kind)
        with session:
            output = session.query(sql, limit)
        source = self._source(
            cached,
            resource,
            retrieved_at=retrieved_at,
            access_method="datastore" if kind == "datastore" else "file_download",
        )
        result: dict[str, Any] = {
            "columns": output.columns,
            "rows": output.rows,
            "returned_rows": output.returned_rows,
            "row_limit": output.row_limit,
            "truncated": output.truncated,
            "rows_in_resource": output.scanned_rows,
            "scope_note": (
                "SQL ran over the entire resource; `truncated` only means the result set "
                "had more rows than were returned."
            ),
            "source": source.to_dict(),
            "citation": source.citation(),
        }
        if output.returned_rows == 0:
            result["note"] = (
                "The query returned no rows. Do not report a number; check column names/values "
                "with get_schema and retry, or say the data does not contain a match."
            )
        assert_provenance(result["source"])
        return result


def safe_call(fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run a service call and convert typed errors into a stable error envelope."""
    try:
        return {"ok": True, **fn(*args, **kwargs)}
    except OpenDataError as exc:
        return {"ok": False, "error": exc.to_dict()}
