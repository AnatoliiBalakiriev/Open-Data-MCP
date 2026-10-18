"""Live smoke test against a real CKAN portal (default: data.gov.ua). Needs network access.

It is NOT part of the test suite or CI. It records what was actually observed, because HTTP 200
alone proves nothing for CKAN (`success: false` can come back with status 200).

    uv run python scripts/smoke_live.py [--base-url https://data.gov.ua] [--query CSV]

Behind an HTTP proxy without local DNS, also set OPEN_DATA_TRUSTED_DOWNLOAD_HOSTS, e.g.
    OPEN_DATA_TRUSTED_DOWNLOAD_HOSTS=data.gov.ua,opendata.gov.ua
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from open_data_mcp.ckan_client import CkanClient
from open_data_mcp.config import Settings
from open_data_mcp.errors import OpenDataError
from open_data_mcp.service import OpenDataService, classify_resource, safe_call


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://data.gov.ua")
    parser.add_argument("--query", default="CSV")
    args = parser.parse_args()
    settings = replace(Settings.from_env(), portal_base_url=args.base_url.rstrip("/"))
    ckan = CkanClient(settings)
    print(
        f"Live smoke test of {settings.portal_base_url} at {datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}"
    )
    failures = 0

    def step(name: str, fn: Any) -> Any:
        nonlocal failures
        try:
            value = fn()
        except OpenDataError as exc:
            failures += 1
            print(f"  FAIL {name}: [{exc.code}] {exc.message}")
            return None
        print(f"  PASS {name}")
        return value

    status = step("status_show (success=true)", ckan.status_show)
    if status is not None:
        print(f"       ckan_version={status.get('ckan_version')!r}")
    hits = step(
        "package_search (success=true, >=1 result)",
        lambda: ckan.search_packages(args.query, limit=10),
    )
    if not hits:
        print("  Nothing more to check without a dataset.")
        return 1

    portal_host = urlsplit(settings.portal_base_url).hostname
    candidates: list[tuple[str, dict[str, Any]]] = []
    for hit in hits:
        package = step(f"package_show {hit['id']}", lambda h=hit: ckan.package_show(h["id"]))
        if package is None:
            continue
        pkg = package.value
        lic = pkg.get("license_title") or pkg.get("license_id")
        print(f"       title={pkg.get('title')!r} license={lic!r}")
        print(f"       metadata_modified={pkg.get('metadata_modified')!r}")
        for res in (pkg.get("resources") or [])[:5]:
            kind, _why = classify_resource(res)
            print(
                f"       resource {res.get('id')}: format={res.get('format')!r} "
                f"datastore_active={res.get('datastore_active')!r} -> {kind or 'unsupported'}"
            )
            if kind and int(res.get("size") or 0) < 5_000_000:
                candidates.append((hit["id"], res))

    # Prefer a file on the portal's own host (other hosts may be outside a network allowlist).
    candidates.sort(key=lambda c: urlsplit(str(c[1].get("url") or "")).hostname != portal_host)
    chosen = candidates[0] if candidates else None

    if chosen is None:
        print("  No small queryable resource among the first hits; end-to-end step skipped.")
        failures += 1
    else:
        dataset_id, res = chosen
        service = OpenDataService(settings, ckan)
        print(f"  End-to-end on dataset {dataset_id}, resource {res['id']}")
        schema = safe_call(service.get_schema, dataset_id, res["id"])
        if schema["ok"]:
            cols = [c["name"] for c in schema["schema"]][:6]
            print(
                f"  PASS get_schema ({schema['schema_source']}, rows={schema['row_count']}): {cols}"
            )
        else:
            failures += 1
            print(f"  FAIL get_schema: {schema['error']}")
        query = safe_call(service.query, dataset_id, res["id"], "SELECT count(*) AS n FROM data")
        if query["ok"]:
            print(f"  PASS query count(*) -> {query['rows']}")
            src = query["source"]
            print(
                f"       source.dataset_modified_at={src['dataset_modified_at']!r}"
                f" retrieved_at_utc={src['retrieved_at_utc']!r} license={src['license']!r}"
            )
            url = src["dataset_url"]
            code: int | str = "no attempt"
            for _ in range(3):  # the portal returns intermittent 522s
                try:
                    code = httpx.get(url, follow_redirects=True, timeout=20).status_code
                except httpx.HTTPError as exc:
                    code = type(exc).__name__
                if code == 200:
                    break
            ok = code == 200
            failures += 0 if ok else 1
            print(f"  {'PASS' if ok else 'FAIL'} dataset_url opens: {url} -> {code}")
        else:
            failures += 1
            print(f"  FAIL query: {query['error']}")
    print("RESULT:", "ALL PASSED" if failures == 0 else f"{failures} step(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
