"""Run `profile_dataset` through the real MCP stdio server against a live CKAN portal.

Not part of the test suite or CI. Prints a digest and, with --json, the full structured result.

    uv run python scripts/profile_live.py DATASET_ID [RESOURCE_ID] [--json]

Behind an HTTP proxy without local DNS, also set OPEN_DATA_TRUSTED_DOWNLOAD_HOSTS=data.gov.ua
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from mcp import Client

from open_data_mcp.cli import _server_params


async def run(dataset_id: str, resource_id: str | None, as_json: bool) -> int:
    arguments = {"dataset_id": dataset_id, **({"resource_id": resource_id} if resource_id else {})}
    started = time.monotonic()
    async with Client(_server_params(offline=False)) as client:
        result = await client.call_tool("profile_dataset", arguments)
    out = result.structured_content or {}
    took = time.monotonic() - started
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1
    if not out.get("ok"):
        print(f"FAILED after {took:.1f}s: {out.get('error')}")
        return 1
    if "columns" not in out or not out["columns"]:
        print(f"No resource chosen ({out.get('note')}); resources:")
        for r in out["resources"]:
            print(
                f"  {r['resource_id']}  {r['format']!s:8} queryable={r['queryable']}  {r['name']}"
            )
        return 0
    cov = out["coverage"]
    print(f"{out['dataset_title']}\n  resource {out['resource_name']} ({out['resource_id']})")
    print(f"  took {took:.1f}s; coverage {cov['mode']} complete={cov['complete']}: {cov['note']}")
    print(f"  rows={out['rows']} columns={out['columns_total']} malformed={out['malformed_rows']}")
    print(f"  issues: {out['issue_counts']}")
    for issue in out["issues"]:
        where = f" [{issue['column']}]" if issue["column"] else ""
        print(f"   - {issue['severity']:6} {issue['check']}{where}: {issue['message'][:230]}")
    print(f"  encoding: {out['encoding'] and out['encoding']['detected']}")
    drift = out["schema_drift"]
    if drift:
        print(f"  drift: compared {len(drift['compared'])}, skipped {drift['skipped_total']}")
        for v in drift["compared"]:
            added, removed = v["columns_added"], v["columns_removed"]
            print(f"    {v['name']}: same_schema={v['same_schema']} +{added} -{removed}")
    print(f"  {out['citation']}")
    print(f"  response size: {len(json.dumps(out, ensure_ascii=False))} chars")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_id")
    parser.add_argument("resource_id", nargs="?")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.dataset_id, args.resource_id, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
