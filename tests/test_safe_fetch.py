from __future__ import annotations

import httpx
import pytest

from open_data_mcp.errors import ResourceTooLarge, UnsafeUrl, UpstreamError
from open_data_mcp.safe_fetch import fetch_bytes, validate_public_https_url


def public(_host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/a.csv",
        "ftp://example.test/a.csv",
        "file:///etc/passwd",
        "https://user:pw@example.test/a.csv",
        "https://example.test:8443/a.csv",
        "https://127.0.0.1/a.csv",
        "https://[::1]/a.csv",
        "https://10.1.2.3/a.csv",
        "https://169.254.169.254/latest/meta-data",
        "https://192.168.0.1/a.csv",
        "https:///nohost",
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeUrl):
        validate_public_https_url(url, public)


def test_hostname_resolving_to_private_address_rejected():
    with pytest.raises(UnsafeUrl, match="non-public"):
        validate_public_https_url(
            "https://sneaky.test/a.csv", lambda _h: ["93.184.216.34", "10.0.0.7"]
        )


def test_public_https_url_accepted():
    assert validate_public_https_url("https://example.test/a.csv", public)


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_redirect_to_private_address_is_blocked():
    def handler(req):
        if req.url.host == "example.test":
            return httpx.Response(302, headers={"location": "https://internal.test/x.csv"})
        return httpx.Response(200, content=b"secret")

    def resolver(host):
        return ["10.0.0.5"] if host == "internal.test" else ["93.184.216.34"]

    with pytest.raises(UnsafeUrl):
        fetch_bytes(
            "https://example.test/a.csv",
            max_bytes=1000,
            client=client_for(handler),
            resolver=resolver,
        )


def test_redirect_to_http_is_blocked():
    def handler(_req):
        return httpx.Response(302, headers={"location": "http://example.test/x.csv"})

    with pytest.raises(UnsafeUrl):
        fetch_bytes(
            "https://example.test/a.csv",
            max_bytes=1000,
            client=client_for(handler),
            resolver=public,
        )


def test_safe_redirect_followed():
    def handler(req):
        if req.url.path == "/a.csv":
            return httpx.Response(301, headers={"location": "/b.csv"})
        return httpx.Response(200, content=b"x,y\n1,2\n")

    data = fetch_bytes(
        "https://example.test/a.csv", max_bytes=1000, client=client_for(handler), resolver=public
    )
    assert data == b"x,y\n1,2\n"


def test_redirect_loop_is_bounded():
    def handler(_req):
        return httpx.Response(302, headers={"location": "https://example.test/a.csv"})

    with pytest.raises(UpstreamError, match="Too many redirects"):
        fetch_bytes(
            "https://example.test/a.csv",
            max_bytes=1000,
            client=client_for(handler),
            resolver=public,
            max_redirects=2,
        )


def test_declared_content_length_over_limit_refused():
    def handler(_req):
        return httpx.Response(200, content=b"x", headers={"content-length": "5000"})

    with pytest.raises(ResourceTooLarge):
        fetch_bytes(
            "https://example.test/a.csv",
            max_bytes=1000,
            client=client_for(handler),
            resolver=public,
        )


def test_streamed_body_over_limit_refused_without_content_length():
    def handler(_req):
        return httpx.Response(200, content=iter([b"x" * 500] * 10))  # chunked: no Content-Length

    with pytest.raises(ResourceTooLarge, match="exceeds"):
        fetch_bytes(
            "https://example.test/a.csv",
            max_bytes=1000,
            client=client_for(handler),
            resolver=public,
        )


def test_http_error_status_is_reported_without_body():
    def handler(_req):
        return httpx.Response(404, text="private page content")

    with pytest.raises(UpstreamError) as exc:
        fetch_bytes(
            "https://example.test/a.csv",
            max_bytes=1000,
            client=client_for(handler),
            resolver=public,
        )
    assert "private page" not in str(exc.value)
