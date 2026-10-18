"""Typed client for the CKAN Action API (https://docs.ckan.org/en/latest/api/).

Checks both the HTTP status and the JSON `success` flag, retries only transient failures with
bounded exponential backoff, and caches *metadata* (never data) for a short TTL.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .errors import InvalidInput, UpstreamError, UpstreamNotFound, UpstreamProtocolError

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
USER_AGENT = "open-data-mcp/0.1 (+https://github.com/AnatoliiBalakiriev/Open-Data-MCP)"


@dataclass(frozen=True)
class CachedValue:
    value: dict[str, Any]
    fetched_monotonic: float
    fetched_at_utc: str


class CkanClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], str] | None = None,
    ) -> None:
        from .provenance import utc_now_iso

        self.settings = settings
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._utc_now = utc_now or utc_now_iso
        self._cache: dict[tuple[str, str], CachedValue] = {}

    # ------------------------------------------------------------------ transport

    def _backoff_delay(self, attempt: int, response: httpx.Response | None) -> float:
        cap = self.settings.backoff_max_s
        if response is not None:
            retry_after = response.headers.get("retry-after", "")
            if retry_after.strip().isdigit():
                return min(float(retry_after), cap)
        return min(self.settings.backoff_base_s * (2.0**attempt), cap)

    def action(self, name: str, params: dict[str, Any] | None = None) -> Any:
        """Call a CKAN action and return `result`. Raises typed errors, never raw bodies."""
        url = f"{self.settings.portal_base_url}/api/3/action/{name}"
        attempts = self.settings.max_retries + 1
        last_problem = "unknown failure"
        for attempt in range(attempts):
            response: httpx.Response | None = None
            try:
                response = self._client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_problem = f"network error ({type(exc).__name__})"
            else:
                if response.status_code in RETRY_STATUS:
                    last_problem = f"HTTP {response.status_code}"
                else:
                    return self._parse(name, response)
            if attempt < attempts - 1:
                self._sleep(self._backoff_delay(attempt, response))
        raise UpstreamError(
            f"CKAN action {name!r} failed after {attempts} attempts: {last_problem}.",
            hint="The portal may be temporarily unavailable. Try again later.",
        )

    def _parse(self, name: str, response: httpx.Response) -> Any:
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise UpstreamProtocolError(
                f"CKAN action {name!r} returned HTTP {response.status_code} with a non-JSON body."
            )
        if body.get("success") is True:
            if response.status_code >= 400:
                raise UpstreamProtocolError(
                    f"CKAN action {name!r} returned HTTP {response.status_code}."
                )
            return body.get("result")
        raw_error = body.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        error_type = str(error.get("__type", ""))
        message = str(error.get("message", ""))[:200]
        if response.status_code == 404 or error_type == "Not Found Error":
            raise UpstreamNotFound(f"CKAN action {name!r}: not found. {message}".strip())
        if error_type == "Validation Error":
            raise InvalidInput(f"CKAN rejected the request to {name!r}: {message}".strip())
        raise UpstreamProtocolError(
            f"CKAN action {name!r} reported failure"
            f" (HTTP {response.status_code}, {error_type or 'no error type'}). {message}".strip()
        )

    # ------------------------------------------------------------------ cache

    def _cached(self, kind: str, key: str, loader: Callable[[], dict[str, Any]]) -> CachedValue:
        now = self._monotonic()
        hit = self._cache.get((kind, key))
        if hit and now - hit.fetched_monotonic < self.settings.metadata_cache_ttl_s:
            return hit
        entry = CachedValue(loader(), now, self._utc_now())
        self._cache[(kind, key)] = entry
        return entry

    # ------------------------------------------------------------------ actions

    def status_show(self) -> dict[str, Any]:
        result = self.action("status_show")
        return result if isinstance(result, dict) else {}

    def package_search(self, q: str, *, rows: int, start: int = 0) -> dict[str, Any]:
        result = self.action("package_search", {"q": q, "rows": rows, "start": start})
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise UpstreamProtocolError("package_search returned an unexpected result shape.")
        return result

    def search_packages(self, q: str, *, limit: int, page_size: int = 10) -> list[dict[str, Any]]:
        """Bounded pagination over `package_search`: never returns more than `limit` items."""
        collected: list[dict[str, Any]] = []
        start = 0
        while len(collected) < limit:
            rows = min(page_size, limit - len(collected))
            page = self.package_search(q, rows=rows, start=start)
            items = page["results"]
            collected.extend(items)
            start += len(items)
            if not items or start >= int(page.get("count", 0)):
                break
        return collected[:limit]

    def package_show(self, dataset_id: str) -> CachedValue:
        def load() -> dict[str, Any]:
            result = self.action("package_show", {"id": dataset_id})
            if not isinstance(result, dict):
                raise UpstreamProtocolError("package_show returned an unexpected result shape.")
            return result

        return self._cached("package", dataset_id, load)

    def datastore_search(self, resource_id: str, *, limit: int, offset: int = 0) -> dict[str, Any]:
        result = self.action(
            "datastore_search",
            {"resource_id": resource_id, "limit": limit, "offset": offset, "sort": "_id"},
        )
        if not isinstance(result, dict) or not isinstance(result.get("fields"), list):
            raise UpstreamProtocolError("datastore_search returned an unexpected result shape.")
        return result

    def close(self) -> None:
        self._client.close()
