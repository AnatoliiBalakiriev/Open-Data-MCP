"""Live smoke test against a real CKAN portal (default: data.gov.ua). Needs network access.

It is NOT part of the test suite or CI. It records what was actually observed, because HTTP 200
alone proves nothing for CKAN (`success: false` can come back with status 200).

    uv run python scripts/smoke_live.py [--base-url https://data.gov.ua] [--query бюджет]
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any

from open_data_mcp.ckan_client import CkanClient
from open_data_mcp.config import Settings
from open_data_mcp.errors import OpenDataError
from open_data_mcp.service import classify_resource


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://data.gov.ua")
    parser.add_argument("--query", default="")
    args = parser.parse_args()
    settings = Settings(
        portal_base_url=args.base_url.rstrip("/"), max_retries=1, backoff_base_s=0.5
    )
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
        lambda: ckan.search_packages(args.query, limit=5),
    )
    if not hits:
        print("  Nothing more to check without a dataset.")
        return 1
    print(f"       first hits: {[h.get('name') for h in hits[:3]]}")
    package = step("package_show", lambda: ckan.package_show(hits[0]["id"]))
    if package is not None:
        pkg = package.value
        lic = pkg.get("license_title") or pkg.get("license_id")
        print(f"       title={pkg.get('title')!r} license={lic!r}")
        print(f"       metadata_modified={pkg.get('metadata_modified')!r}")
        for res in (pkg.get("resources") or [])[:5]:
            kind, why = classify_resource(res)
            fmt, active = res.get("format"), res.get("datastore_active")
            print(f"       resource {res.get('id')}: format={fmt!r} datastore_active={active!r}")
            print(f"         -> {kind or 'unsupported'} ({why})")
            if kind == "datastore":
                rid = res["id"]
                step(
                    f"datastore_search limit=0 ({rid})",
                    lambda r=rid: ckan.datastore_search(r, limit=0),
                )
                break
    print("RESULT:", "ALL PASSED" if failures == 0 else f"{failures} step(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
