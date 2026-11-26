"""
Offline stand-ins for the two seams of the job-page fetch.

`app.routers.job` reaches the network through exactly two module attributes:
`RESOLVER` (getaddrinfo-compatible) and `TRANSPORT` (an httpx transport, or
None for the real one). `secprim.safe_fetch` takes both, so with these swapped
the whole fetch path — resolution, the pinned connection, redirect hops, the
body cap — runs for real against a scripted origin and never touches DNS or
a socket.
"""
import socket

import httpx

from app.routers import job as job_router

#: A public address (example.com's), used wherever a host should pass.
PUBLIC = "93.184.216.34"
#: The cloud metadata address: the destination a redirect must never reach.
METADATA = "169.254.169.254"


def resolver_for(table):
    """A getaddrinfo stand-in. `table` maps hostname -> address string (or
    is a callable doing the same); an unknown host fails to resolve."""
    def _resolve(hostname, port=None, *args, **kwargs):
        address = table(hostname) if callable(table) else table.get(hostname)
        if address is None:
            raise socket.gaierror(8, "nodename nor servname provided")
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port or 0))]
    return _resolve


def install(monkeypatch, handler, resolve=None):
    """Route the fetch through `handler` (an httpx.MockTransport handler) and
    resolve every host to `resolve` (an address, a table, or a callable;
    default PUBLIC). Returns the list the handler appends each request to."""
    seen = []

    def _handler(request):
        seen.append(request)
        return handler(request)

    if resolve is None or isinstance(resolve, str):
        address = PUBLIC if resolve is None else resolve
        table = lambda hostname: address  # noqa: E731
    else:
        table = resolve
    monkeypatch.setattr(job_router, "RESOLVER", resolver_for(table))
    monkeypatch.setattr(job_router, "TRANSPORT", httpx.MockTransport(_handler))
    return seen


def html(body=b"<html><body>Job Description</body></html>", status=200, headers=None):
    """A handler that serves one fixed response."""
    return lambda request: httpx.Response(status, content=body, headers=headers or {})
