"""Provenance: every successful query result carries a `source` object.

`retrieved_at_utc` is when *this server* fetched the data. It is never a substitute for the
portal's own modification date; unknown source dates stay `None`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from .errors import ProvenanceError

# Fields that must be non-empty for a result to be returned at all.
REQUIRED_FIELDS = ("portal", "dataset_id", "resource_id", "retrieved_at_utc")


def utc_now_iso(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Source:
    portal: str
    dataset_id: str
    dataset_title: str | None
    dataset_url: str | None
    resource_id: str
    resource_name: str | None
    resource_url: str | None
    retrieved_at_utc: str
    metadata_retrieved_at_utc: str | None
    dataset_modified_at: str | None
    resource_modified_at: str | None
    license: str | None
    access_method: str  # "datastore" | "file_download"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def citation(self) -> str:
        """One-line, human-readable source note for the final answer."""
        modified = self.dataset_modified_at or "unknown"
        return (
            f"Source: {self.dataset_title or self.dataset_id} ({self.dataset_url or 'no URL'}), "
            f"resource {self.resource_name or self.resource_id}; portal {self.portal}; "
            f"dataset last modified {modified}; license {self.license or 'unknown'}; "
            f"retrieved {self.retrieved_at_utc} UTC via {self.access_method}."
        )


def build_source(
    *,
    portal: str,
    package: dict[str, Any],
    resource: dict[str, Any],
    dataset_url: str | None,
    retrieved_at_utc: str,
    metadata_retrieved_at_utc: str | None,
    access_method: str,
) -> Source:
    def clean(value: Any) -> str | None:
        return value if isinstance(value, str) and value.strip() else None

    source = Source(
        portal=portal,
        dataset_id=str(package.get("id") or ""),
        dataset_title=clean(package.get("title")),
        dataset_url=dataset_url,
        resource_id=str(resource.get("id") or ""),
        resource_name=clean(resource.get("name")),
        resource_url=clean(resource.get("url")),
        retrieved_at_utc=retrieved_at_utc,
        metadata_retrieved_at_utc=metadata_retrieved_at_utc,
        dataset_modified_at=clean(package.get("metadata_modified")),
        resource_modified_at=clean(resource.get("last_modified") or resource.get("created")),
        license=clean(package.get("license_title") or package.get("license_id")),
        access_method=access_method,
    )
    assert_provenance(source.to_dict())
    return source


def assert_provenance(source: dict[str, Any] | None) -> None:
    """Refuse to hand out a data result without minimum provenance."""
    if not source:
        raise ProvenanceError("Query result has no `source` object.")
    missing = [f for f in REQUIRED_FIELDS if not source.get(f)]
    if missing:
        raise ProvenanceError(f"Provenance incomplete, missing: {', '.join(missing)}")
