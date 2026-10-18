"""Typed errors. Messages are safe to show to an LLM: no tokens, no raw response bodies."""

from __future__ import annotations


class OpenDataError(Exception):
    """Base class. `code` is a stable machine-readable identifier."""

    code = "error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "hint": self.hint}


class UpstreamError(OpenDataError):
    """The portal could not be reached or answered with a failure."""

    code = "upstream_error"


class UpstreamNotFound(UpstreamError):
    code = "not_found"


class UpstreamProtocolError(UpstreamError):
    """HTTP 200 with an unusable body (not JSON, or CKAN `success: false`)."""

    code = "upstream_protocol_error"


class UnsupportedResource(OpenDataError):
    code = "unsupported_resource"


class ResourceTooLarge(OpenDataError):
    code = "resource_too_large"


class UnsafeUrl(OpenDataError):
    code = "unsafe_url"


class UnsafeSql(OpenDataError):
    code = "unsafe_sql"


class SqlError(OpenDataError):
    """The query is allowed but DuckDB could not run it (unknown column, bad syntax, ...)."""

    code = "sql_error"


class InvalidInput(OpenDataError):
    code = "invalid_input"


class ProvenanceError(OpenDataError):
    code = "provenance_missing"
