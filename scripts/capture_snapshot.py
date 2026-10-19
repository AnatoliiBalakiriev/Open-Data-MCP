"""Capture the real-data evaluation snapshot from data.gov.ua (needs network access).

Writes the raw resource bytes unchanged plus `SNAPSHOT.json` (source URLs, ids, license,
capture time, sha256) under src/open_data_mcp/fixtures/real/. Metadata comes from the live
portal at capture time; nothing is filled in by hand. Personal fields (the dataset's
maintainer/author) are deliberately not copied.

    uv run python scripts/capture_snapshot.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import httpx

from open_data_mcp.ckan_client import CkanClient
from open_data_mcp.config import Settings
from open_data_mcp.provenance import utc_now_iso
from open_data_mcp.safe_fetch import fetch_bytes, system_resolver

DATASET_ID = "f2416e7e-8ea7-449c-9dd2-eaf022a5ea1b"
RESOURCE_ID = "1950d531-434a-48d8-bdea-1f1542a1800b"
OUT_DIR = Path(__file__).resolve().parent.parent / "src" / "open_data_mcp" / "fixtures" / "real"
CSV_NAME = "education_institutions.csv"


def main() -> int:
    settings = Settings.from_env()
    ckan = CkanClient(settings)
    pkg = ckan.package_show(DATASET_ID).value
    res = next(r for r in pkg["resources"] if r["id"] == RESOURCE_ID)
    if pkg.get("license_id") != "cc-by":
        print(f"Refusing to snapshot: license is {pkg.get('license_id')!r}, expected 'cc-by'.")
        return 1
    data = fetch_bytes(
        res["url"],
        max_bytes=1_000_000,
        client=httpx.Client(timeout=60),
        resolver=system_resolver,
        trusted_hosts=frozenset(settings.trusted_download_hosts),
    )
    org = pkg.get("organization") or {}
    meta = {
        "snapshot_captured_at_utc": utc_now_iso(),
        "portal": "data.gov.ua",
        "dataset_url": f"{settings.portal_base_url}/dataset/{pkg['name']}",
        "api_url": f"{settings.portal_base_url}/api/3/action/package_show?id={DATASET_ID}",
        "dataset": {
            "id": pkg["id"],
            "name": pkg["name"],
            "title": pkg.get("title"),
            "organization": org.get("title"),
            "license_id": pkg.get("license_id"),
            "license_title": pkg.get("license_title"),
            "license_url": pkg.get("license_url"),
            "metadata_created": pkg.get("metadata_created"),
            "metadata_modified": pkg.get("metadata_modified"),
        },
        "resource": {
            "id": res["id"],
            "name": res.get("name"),
            "format": res.get("format"),
            "url": res["url"],
            "size": res.get("size"),
            "created": res.get("created"),
            "last_modified": res.get("last_modified"),
        },
        "file": {
            "path": CSV_NAME,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "note": "Stored byte-for-byte as served (Windows-1251, ';' delimiter, CRLF).",
        },
        "attribution": (
            f"{org.get('title')}, via data.gov.ua, licensed under Creative Commons Attribution "
            "(CC BY): http://www.opendefinition.org/licenses/cc-by"
        ),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / CSV_NAME).write_bytes(data)
    (OUT_DIR / "SNAPSHOT.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {CSV_NAME} ({len(data)} bytes, sha256 {meta['file']['sha256'][:12]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
