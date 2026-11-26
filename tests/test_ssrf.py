"""
Tests for the URL guard on the job-posting fetcher.

POST /api/job/url fetches a URL chosen by the caller, which makes the server
a proxy into whatever it can reach: localhost, the private network, and the
cloud metadata service on 169.254.169.254 that hands out instance
credentials.
"""
import ipaddress

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.routers import job as job_router

client = TestClient(app)


@pytest.fixture
def resolves_to(monkeypatch):
    """Force hostname resolution to a chosen address."""

    def _set(address):
        monkeypatch.setattr(
            job_router,
            "_resolved_addresses",
            lambda hostname: [ipaddress.ip_address(address)],
        )

    return _set


@pytest.mark.parametrize(
    "address,what",
    [
        ("127.0.0.1", "loopback"),
        ("::1", "loopback v6"),
        ("10.0.0.5", "private class A"),
        ("172.16.4.2", "private class B"),
        ("192.168.1.1", "private class C"),
        # The one that actually gets people: on AWS and GCP this endpoint
        # serves instance credentials to anything that asks.
        ("169.254.169.254", "cloud metadata"),
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
        ("fd00::1", "unique local v6"),
    ],
)
def test_non_public_addresses_are_rejected(resolves_to, address, what):
    resolves_to(address)

    with pytest.raises(HTTPException) as exc_info:
        job_router.assert_public_url("https://looks-legit.example.com/job")

    assert exc_info.value.status_code == 400, what


def test_public_address_is_allowed(resolves_to):
    resolves_to("93.184.216.34")

    job_router.assert_public_url("https://example.com/job")


def test_url_without_host_is_rejected():
    with pytest.raises(HTTPException) as exc_info:
        job_router.assert_public_url("not-a-url")

    assert exc_info.value.status_code == 400


def test_endpoint_rejects_internal_target(resolves_to):
    """End-to-end through the API, not just the helper."""
    resolves_to("169.254.169.254")

    response = client.post(
        "/api/job/url", json={"url": "http://metadata.example.com/latest/meta-data/"}
    )

    assert response.status_code == 400
    assert "public address" in response.json()["detail"]


def test_redirect_to_internal_address_is_blocked(monkeypatch):
    """The case a single up-front check misses.

    The submitted URL is public and passes; the server then 302s to the
    metadata service. Redirects are never followed automatically: each hop
    is resolved, checked, and only then connected to — so the second hop is
    refused and the origin never sees a request for it.
    """
    import httpx

    from tests import fetch_fakes

    seen = []

    def _resolve(hostname):
        seen.append(hostname)
        # First hop public, everything after it internal.
        return fetch_fakes.PUBLIC if hostname == "jobs.example.com" else fetch_fakes.METADATA

    def _origin(request):
        return httpx.Response(
            302, headers={"location": "http://metadata.internal/latest/meta-data/"}
        )

    requests = fetch_fakes.install(monkeypatch, _origin, resolve=_resolve)

    response = client.post("/api/job/url", json={"url": "https://jobs.example.com/x"})

    assert response.status_code == 400
    assert "public address" in response.json()["detail"]
    assert "metadata.internal" in seen, "the redirect target was never checked"
    assert [r.headers["host"] for r in requests] == ["jobs.example.com"], \
        "the origin saw a request for the internal hop"
