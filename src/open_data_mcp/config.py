"""Runtime configuration, read from environment variables with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    portal_name: str = "data.gov.ua"
    portal_base_url: str = "https://data.gov.ua"
    dataset_url_template: str = "{base}/dataset/{name}"

    # HTTP behaviour
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 30.0
    max_retries: int = 3
    backoff_base_s: float = 0.5
    backoff_max_s: float = 8.0
    metadata_cache_ttl_s: float = 300.0

    # Search / query limits (enforced on the server, not in the prompt)
    search_max_limit: int = 20
    query_default_limit: int = 20
    query_max_limit: int = 100
    max_download_bytes: int = 25 * 1024 * 1024
    max_table_rows: int = 500_000
    datastore_page_size: int = 5000
    query_timeout_s: float = 20.0
    duckdb_memory_limit: str = "512MB"
    max_redirects: int = 3
    # Operator-trusted resource hosts: skip the local DNS/public-address check (useful behind an
    # HTTP proxy that does the resolving). Still HTTPS-only. Empty by default.
    trusted_download_hosts: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Settings:
        base = os.environ.get("OPEN_DATA_PORTAL_URL", cls.portal_base_url).rstrip("/")
        return cls(
            portal_name=os.environ.get("OPEN_DATA_PORTAL_NAME", cls.portal_name),
            portal_base_url=base,
            metadata_cache_ttl_s=_float("OPEN_DATA_CACHE_TTL_S", cls.metadata_cache_ttl_s),
            max_download_bytes=_int("OPEN_DATA_MAX_DOWNLOAD_BYTES", cls.max_download_bytes),
            max_table_rows=_int("OPEN_DATA_MAX_TABLE_ROWS", cls.max_table_rows),
            read_timeout_s=_float("OPEN_DATA_READ_TIMEOUT_S", cls.read_timeout_s),
            trusted_download_hosts=tuple(
                h.strip().lower()
                for h in os.environ.get("OPEN_DATA_TRUSTED_DOWNLOAD_HOSTS", "").split(",")
                if h.strip()
            ),
            query_max_limit=_int("OPEN_DATA_QUERY_MAX_LIMIT", cls.query_max_limit),
        )
