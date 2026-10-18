from __future__ import annotations

import httpx
import pytest

from open_data_mcp.ckan_client import CkanClient
from open_data_mcp.config import Settings
from open_data_mcp.errors import (
    InvalidInput,
    UpstreamError,
    UpstreamNotFound,
    UpstreamProtocolError,
)

SETTINGS = Settings(portal_base_url="https://ckan.test", backoff_base_s=0.5, backoff_max_s=4.0)


def make_client(handler, *, settings: Settings = SETTINGS, **kw):
    sleeps: list[float] = []
    client = CkanClient(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        **kw,
    )
    return client, sleeps


def ok(result):
    return httpx.Response(200, json={"success": True, "result": result})


def test_success_false_with_http_200_is_an_error():
    def handler(_req):
        body = {"success": False, "error": {"__type": "Authorization Error", "message": "nope"}}
        return httpx.Response(200, json=body)

    client, _ = make_client(handler)
    with pytest.raises(UpstreamProtocolError, match="reported failure"):
        client.status_show()


def test_not_found_maps_to_typed_error():
    body = {"success": False, "error": {"__type": "Not Found Error", "message": "Not found"}}
    client, _ = make_client(lambda _r: httpx.Response(404, json=body))
    with pytest.raises(UpstreamNotFound):
        client.package_show("x")


def test_validation_error_is_invalid_input():
    body = {"success": False, "error": {"__type": "Validation Error", "message": "bad rows"}}
    client, _ = make_client(lambda _r: httpx.Response(409, json=body))
    with pytest.raises(InvalidInput):
        client.package_search("q", rows=1)


def test_non_json_body_is_protocol_error_and_not_leaked():
    secret = "<html>SECRET-TOKEN-abc123</html>"
    client, _ = make_client(lambda _r: httpx.Response(200, text=secret))
    with pytest.raises(UpstreamProtocolError) as exc:
        client.status_show()
    assert "SECRET-TOKEN" not in str(exc.value)


def test_retries_503_then_succeeds_with_exponential_backoff():
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else ok({"ckan_version": "2.10"})

    client, sleeps = make_client(handler)
    assert client.status_show() == {"ckan_version": "2.10"}
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]


def test_retry_after_header_is_honoured_and_capped():
    responses = iter(
        [
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(429, headers={"retry-after": "999"}),
            ok({}),
        ]
    )
    client, sleeps = make_client(lambda _r: next(responses))
    client.status_show()
    assert sleeps == [2.0, 4.0]  # second value capped at backoff_max_s


def test_exhausted_retries_raise_without_body():
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(500, text="internal secret stack trace")

    client, sleeps = make_client(handler)
    with pytest.raises(UpstreamError, match="after 4 attempts") as exc:
        client.status_show()
    assert calls["n"] == 4 and len(sleeps) == 3
    assert "secret" not in str(exc.value)


def test_timeouts_are_retried_then_reported():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    client, sleeps = make_client(handler)
    with pytest.raises(UpstreamError, match="ReadTimeout"):
        client.status_show()
    assert len(sleeps) == 3


def test_client_errors_are_not_retried():
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(
            403, json={"success": False, "error": {"__type": "Authorization Error"}}
        )

    client, sleeps = make_client(handler)
    with pytest.raises(UpstreamProtocolError):
        client.status_show()
    assert calls["n"] == 1 and sleeps == []


def test_pagination_is_bounded_and_stops_at_count():
    seen: list[tuple[int, int]] = []
    items = [{"id": str(i)} for i in range(25)]

    def handler(req):
        start, rows = int(req.url.params["start"]), int(req.url.params["rows"])
        seen.append((start, rows))
        return ok({"count": len(items), "results": items[start : start + rows]})

    client, _ = make_client(handler)
    got = client.search_packages("q", limit=12, page_size=5)
    assert [p["id"] for p in got] == [str(i) for i in range(12)]
    assert seen == [(0, 5), (5, 5), (10, 2)]  # never asks for more than `limit`
    seen.clear()
    assert len(client.search_packages("q", limit=100, page_size=10)) == 25  # stops at `count`


def test_metadata_cache_respects_ttl():
    calls = {"n": 0}
    now = {"t": 0.0}

    def handler(_req):
        calls["n"] += 1
        return ok({"id": "d1"})

    s = Settings(portal_base_url="https://ckan.test", metadata_cache_ttl_s=60)
    client, _ = make_client(handler, settings=s, monotonic=lambda: now["t"])
    client.package_show("d1")
    now["t"] = 59
    client.package_show("d1")
    assert calls["n"] == 1
    now["t"] = 61
    client.package_show("d1")
    assert calls["n"] == 2


def test_unexpected_result_shape_is_protocol_error():
    client, _ = make_client(lambda _r: ok({"count": 1}))
    with pytest.raises(UpstreamProtocolError, match="unexpected result shape"):
        client.package_search("q", rows=1)
