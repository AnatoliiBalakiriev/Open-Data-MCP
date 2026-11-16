"""Bounded, SSRF-aware download of a resource URL taken from validated CKAN metadata.

The LLM never supplies a URL; this module is the second line of defence for URLs that the
portal metadata contains (which are still untrusted data).
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import urljoin, urlsplit

import httpx

from .errors import ResourceTooLarge, UnsafeUrl, UpstreamError

Resolver = Callable[[str], list[str]]
_T = TypeVar("_T")


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UpstreamError(
            f"Could not resolve host {host!r}.",
            hint="Behind an HTTP proxy? List the host in OPEN_DATA_TRUSTED_DOWNLOAD_HOSTS.",
        ) from exc
    return sorted({str(info[4][0]) for info in infos})


def validate_public_https_url(
    url: str,
    resolver: Resolver = system_resolver,
    trusted_hosts: frozenset[str] = frozenset(),
) -> str:
    """Return the URL if it is HTTPS on port 443 and every resolved address is public.

    Hosts in `trusted_hosts` (operator configuration) skip address resolution; IP literals are
    always checked.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnsafeUrl("Only https:// resource URLs are allowed.")
    if parts.username or parts.password:
        raise UnsafeUrl("URLs with embedded credentials are not allowed.")
    host = parts.hostname
    if not host:
        raise UnsafeUrl("Resource URL has no host.")
    if parts.port not in (None, 443):
        raise UnsafeUrl("Only the default HTTPS port is allowed.")
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        if host.lower() in trusted_hosts:
            return url
        addresses = resolver(host)
    if not addresses:
        raise UnsafeUrl("Resource host did not resolve to any address.")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise UnsafeUrl("Resource URL points to a non-public network address.")
    return url


# Transient statuses worth retrying. 520-524 are Cloudflare's "origin unreachable / timed out"
# family; data.gov.ua sits behind it and returns an intermittent 522 (observed live 2026-10-18).
RETRY_STATUS = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524})


def fetch_bytes(
    url: str,
    *,
    max_bytes: int,
    client: httpx.Client,
    resolver: Resolver = system_resolver,
    max_redirects: int = 3,
    trusted_hosts: frozenset[str] = frozenset(),
    total_timeout_s: float = 60.0,
    retries: int = 2,
    backoff_s: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Download `url`, re-validating every redirect hop, refusing anything above `max_bytes`.

    Transient failures (network errors, 429/5xx/Cloudflare 52x) are retried a bounded number of
    times per hop; size, URL-safety and 4xx errors are never retried.
    """
    return _fetch(
        url,
        reader=lambda response, deadline: _read_limited(response, max_bytes, deadline),
        client=client,
        resolver=resolver,
        max_redirects=max_redirects,
        trusted_hosts=trusted_hosts,
        total_timeout_s=total_timeout_s,
        retries=retries,
        backoff_s=backoff_s,
        sleep=sleep,
    )


@dataclass(frozen=True)
class Prefix:
    """The first bytes of a download. `truncated` is True when the resource has more."""

    payload: bytes
    truncated: bool
    declared_size: int | None


def fetch_prefix(
    url: str,
    *,
    max_bytes: int,
    client: httpx.Client,
    resolver: Resolver = system_resolver,
    max_redirects: int = 3,
    trusted_hosts: frozenset[str] = frozenset(),
    total_timeout_s: float = 60.0,
    retries: int = 2,
    backoff_s: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> Prefix:
    """Like `fetch_bytes`, but a resource larger than `max_bytes` yields its first `max_bytes`.

    Only for callers that label the result as a prefix (never for `query`). The connection is
    closed as soon as enough bytes have been read, so a 400 MB file costs `max_bytes` of traffic.
    """
    return _fetch(
        url,
        reader=lambda response, deadline: _read_prefix(response, max_bytes, deadline),
        client=client,
        resolver=resolver,
        max_redirects=max_redirects,
        trusted_hosts=trusted_hosts,
        total_timeout_s=total_timeout_s,
        retries=retries,
        backoff_s=backoff_s,
        sleep=sleep,
    )


def _fetch(
    url: str,
    *,
    reader: Callable[[httpx.Response, float], _T],
    client: httpx.Client,
    resolver: Resolver,
    max_redirects: int,
    trusted_hosts: frozenset[str],
    total_timeout_s: float,
    retries: int,
    backoff_s: float,
    sleep: Callable[[float], None],
) -> _T:
    deadline = time.monotonic() + total_timeout_s
    current = url
    for _ in range(max_redirects + 1):
        validate_public_https_url(current, resolver, trusted_hosts)
        for attempt in range(retries + 1):
            last = attempt == retries
            try:
                with client.stream("GET", current, follow_redirects=False) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise UpstreamError("Redirect without a Location header.")
                        current = urljoin(current, location)
                        break
                    if response.status_code in RETRY_STATUS and not last:
                        sleep(backoff_s * (2.0**attempt))
                        continue
                    if response.status_code != 200:
                        raise UpstreamError(
                            f"Resource download failed with HTTP {response.status_code}."
                        )
                    return reader(response, deadline)
            except httpx.HTTPError as exc:
                if last:
                    raise UpstreamError(
                        f"Resource download failed ({type(exc).__name__})."
                    ) from exc
                sleep(backoff_s * (2.0**attempt))
        else:
            raise UpstreamError("Resource download failed after retries.")
    raise UpstreamError("Too many redirects while downloading the resource.")


def _declared_size(response: httpx.Response) -> int | None:
    declared = response.headers.get("content-length")
    return int(declared) if declared and declared.isdigit() else None


def _read_limited(response: httpx.Response, max_bytes: int, deadline: float) -> bytes:
    declared = _declared_size(response)
    if declared is not None and declared > max_bytes:
        raise ResourceTooLarge(
            f"Resource is {declared} bytes; the limit is {max_bytes}.",
            hint="Pick a smaller resource. Partial data is never queried.",
        )
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResourceTooLarge(
                f"Resource exceeds the {max_bytes}-byte download limit.",
                hint="Pick a smaller resource. Partial data is never queried.",
            )
        if time.monotonic() > deadline:
            raise UpstreamError("Resource download timed out.")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_prefix(response: httpx.Response, max_bytes: int, deadline: float) -> Prefix:
    declared = _declared_size(response)
    chunks: list[bytes] = []
    total = 0
    truncated = declared is not None and declared > max_bytes
    stream = response.iter_bytes()  # one iterator only: httpx refuses a second one
    for chunk in stream:
        if time.monotonic() > deadline:
            raise UpstreamError("Resource download timed out.")
        room = max_bytes - total
        if len(chunk) > room:
            chunks.append(chunk[:room])
            total = max_bytes
            truncated = True
            break
        chunks.append(chunk)
        total += len(chunk)
        if total == max_bytes:
            # Exactly full: more data may follow, so peek before claiming completeness.
            truncated = truncated or next(stream, b"") != b""
            break
    return Prefix(b"".join(chunks), truncated, declared)
