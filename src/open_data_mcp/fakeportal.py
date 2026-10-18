"""An in-process fake CKAN portal serving SYNTHETIC fixtures.

Used by the offline test suite, the offline evaluation and `open-data-mcp serve --offline-fixtures`.
Nothing here is real Ukrainian government data, and the portal name says so on purpose.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from .ckan_client import CkanClient
from .config import Settings
from .service import OpenDataService

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FAKE_BASE = "https://portal.fixtures.invalid"
FAKE_FILES = "https://files.fixtures.invalid"
PUBLIC_IP = "93.184.216.34"

OFFLINE_SETTINGS = Settings(
    portal_name="offline-fixtures (SYNTHETIC data, not data.gov.ua)",
    portal_base_url=FAKE_BASE,
    backoff_base_s=0.0,
    datastore_page_size=10,
)


def _resource(rid: str, name: str, fmt: str, url: str, **extra: Any) -> dict[str, Any]:
    return {"id": rid, "name": name, "format": fmt, "url": url, **extra}


def build_packages() -> list[dict[str, Any]]:
    license_ = {"license_title": "Creative Commons Attribution 4.0", "license_id": "cc-by"}
    org = {"title": "SYNTHETIC Test Organization"}
    return [
        {
            "id": "syn-service-requests-2024",
            "name": "syn-service-requests-2024",
            "title": "[SYNTHETIC FIXTURE] City service requests 2024",
            "notes": "Invented citizen service requests for tests. Not real government data.",
            "organization": org,
            "metadata_modified": "2024-12-31T10:00:00.000000",
            **license_,
            "resources": [
                _resource(
                    "syn-res-requests-csv",
                    "service_requests.csv",
                    "CSV",
                    f"{FAKE_FILES}/service_requests.csv",
                    last_modified="2024-12-30T09:00:00.000000",
                    datastore_active=False,
                )
            ],
        },
        {
            "id": "syn-regional-budget",
            "name": "syn-regional-budget",
            "title": "[SYNTHETIC FIXTURE] Regional budget execution 2021-2024",
            "notes": "Invented budget numbers served through a fake CKAN DataStore.",
            "organization": org,
            "metadata_modified": "2025-01-15T08:30:00.000000",
            **license_,
            "resources": [
                _resource(
                    "syn-res-budget-datastore",
                    "budget",
                    "CSV",
                    f"{FAKE_FILES}/budget.csv",
                    datastore_active=True,
                )
            ],
        },
        {
            "id": "syn-file-variants",
            "name": "syn-file-variants",
            "title": "[SYNTHETIC FIXTURE] Problematic file resources",
            "notes": "Resources that must be refused: xlsx, oversized, private host, plain http.",
            "organization": org,
            "metadata_modified": "2024-06-01T00:00:00.000000",
            **license_,
            "resources": [
                _resource("syn-res-xlsx", "table.xlsx", "XLSX", f"{FAKE_FILES}/table.xlsx"),
                _resource("syn-res-huge-csv", "huge.csv", "CSV", f"{FAKE_FILES}/huge.csv"),
                _resource(
                    "syn-res-private-csv",
                    "private.csv",
                    "CSV",
                    "https://internal.fixtures.invalid/private.csv",
                ),
                _resource(
                    "syn-res-http-csv", "plain.csv", "CSV", "http://files.fixtures.invalid/p.csv"
                ),
            ],
        },
        {  # deliberately sparse metadata: no license, no modification date, no organization
            "id": "syn-minimal-metadata",
            "name": "syn-minimal-metadata",
            "title": "[SYNTHETIC FIXTURE] Sparse metadata",
            "notes": "",
            "resources": [
                _resource("syn-res-minimal-csv", "tiny.csv", "CSV", f"{FAKE_FILES}/tiny.csv")
            ],
        },
    ]


class FakePortal:
    """Handler for `httpx.MockTransport`. Records every request for assertions."""

    def __init__(self) -> None:
        self.packages = build_packages()
        self.budget = json.loads((FIXTURE_DIR / "budget_records.json").read_text("utf-8"))
        self.requests_csv = (FIXTURE_DIR / "service_requests.csv").read_bytes()
        self.calls: list[str] = []

    # -- helpers
    @staticmethod
    def _ok(result: Any) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": result})

    @staticmethod
    def _fail(status: int, kind: str, message: str) -> httpx.Response:
        body = {"success": False, "error": {"__type": kind, "message": message}}
        return httpx.Response(status, json=body)

    def _package(self, ident: str) -> dict[str, Any] | None:
        return next((p for p in self.packages if ident in (p["id"], p["name"])), None)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        self.calls.append(f"{url.netloc}{url.path}")
        if url.netloc == "files.fixtures.invalid" or url.netloc.startswith("internal."):
            return self._download(url.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        action = url.path.rsplit("/", 1)[-1]
        if action == "status_show":
            return self._ok({"ckan_version": "fixture", "site_title": "SYNTHETIC fixtures"})
        if action == "package_search":
            return self._search(query)
        if action == "package_show":
            pkg = self._package(query.get("id", ""))
            if pkg is None:
                return self._fail(404, "Not Found Error", "Not found")
            return self._ok(pkg)
        if action == "datastore_search":
            return self._datastore(query)
        return self._fail(404, "Not Found Error", f"Action {action} not found")

    def _search(self, q: dict[str, str]) -> httpx.Response:
        needle = q.get("q", "").lower()
        hits = [
            p
            for p in self.packages
            if not needle or needle in f"{p['title']} {p.get('notes', '')} {p['name']}".lower()
        ]
        start, rows = int(q.get("start", 0)), int(q.get("rows", 10))
        return self._ok({"count": len(hits), "results": hits[start : start + rows]})

    def _datastore(self, q: dict[str, str]) -> httpx.Response:
        if q.get("resource_id") != "syn-res-budget-datastore":
            return self._fail(404, "Not Found Error", "Resource not found")
        limit, offset = int(q.get("limit", 100)), int(q.get("offset", 0))
        fields = [
            {"id": "_id", "type": "int"},
            {"id": "region", "type": "text"},
            {"id": "year", "type": "int4"},
            {"id": "planned_uah", "type": "int8"},
            {"id": "executed_uah", "type": "int8"},
        ]
        records = [{"_id": i + 1, **row} for i, row in enumerate(self.budget)]
        return self._ok(
            {"fields": fields, "records": records[offset : offset + limit], "total": len(records)}
        )

    def _download(self, path: str) -> httpx.Response:
        if path == "/service_requests.csv":
            return httpx.Response(200, content=self.requests_csv)
        if path == "/tiny.csv":
            return httpx.Response(200, content=b"a,b\n1,2\n3,4\n")
        if path == "/huge.csv":
            return httpx.Response(200, content=b"x", headers={"content-length": "999999999"})
        return httpx.Response(404)


def public_resolver(host: str) -> list[str]:
    """Fake DNS: `internal.*` hosts resolve to a private address, the rest to a public one."""
    return ["10.0.0.5"] if host.startswith("internal.") else [PUBLIC_IP]


def build_offline_service(
    settings: Settings | None = None,
) -> tuple[OpenDataService, FakePortal]:
    """A fully wired service backed by the fake portal; no network access."""
    cfg = settings or OFFLINE_SETTINGS
    portal = FakePortal()
    client = httpx.Client(transport=httpx.MockTransport(portal))
    ckan = CkanClient(replace(cfg), client=client, sleep=lambda _s: None)
    service = OpenDataService(
        cfg,
        ckan,
        download_client=httpx.Client(transport=httpx.MockTransport(portal)),
        resolver=public_resolver,
    )
    return service, portal
