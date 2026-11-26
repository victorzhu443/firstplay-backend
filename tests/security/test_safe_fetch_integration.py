"""The /api/job/url fetch path, end to end, against a scripted origin.

`/api/job/url` fetches a URL the caller chose. The earlier guard resolved the
host and checked the address, then let the HTTP client resolve it again to
connect; a DNS answer that changed between those two lookups reached an
address the check had never seen. The fetch now goes through
`secprim.safe_fetch`, which connects to the exact address it validated and
repeats the check on every redirect hop.

These tests run the real handler with the two seams swapped — the resolver
and the httpx transport (`tests/fetch_fakes.py`) — so the whole path runs
offline and the origin can report precisely what it was asked for.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.job import MAX_HTML_BYTES, MAX_REDIRECTS
from tests import fetch_fakes

client = TestClient(app)

HOST = "jobs.example.com"


def test_connection_is_pinned_to_the_validated_address(monkeypatch):
    """The origin is asked for the validated IP literal, with the real
    hostname in Host (and SNI), never for the hostname itself. A DNS answer
    that changes after validation therefore has nothing left to change."""
    lookups = []

    def _resolve(hostname):
        lookups.append(hostname)
        return fetch_fakes.PUBLIC

    requests = fetch_fakes.install(monkeypatch, fetch_fakes.html(), resolve=_resolve)

    response = client.post("/api/job/url", json={"url": f"https://{HOST}/job/42"})

    assert response.status_code == 200
    assert "job_id" in response.json()
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == fetch_fakes.PUBLIC, "connected by name, not by validated address"
    assert request.url.path == "/job/42"
    assert request.headers["host"] == HOST
    assert request.extensions.get("sni_hostname") == HOST
    assert lookups and set(lookups) == {HOST}


def test_redirect_to_a_non_public_address_is_refused_with_400(monkeypatch):
    """A public first hop that 302s inward is stopped before the connection."""
    table = {HOST: fetch_fakes.PUBLIC, "internal.example": fetch_fakes.METADATA}

    def _origin(request):
        return httpx.Response(302, headers={"location": "http://internal.example/latest/"})

    requests = fetch_fakes.install(monkeypatch, _origin, resolve=table)

    response = client.post("/api/job/url", json={"url": f"https://{HOST}/job"})

    assert response.status_code == 400
    assert "public address" in response.json()["detail"]
    # Only the first hop was ever connected to.
    assert [r.headers["host"] for r in requests] == [HOST]


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", fetch_fakes.METADATA, "::1"])
def test_redirect_hop_cannot_reach_any_non_public_range(monkeypatch, address):
    table = {HOST: fetch_fakes.PUBLIC, "elsewhere.example": address}

    def _origin(request):
        return httpx.Response(301, headers={"location": "https://elsewhere.example/"})

    requests = fetch_fakes.install(monkeypatch, _origin, resolve=table)

    response = client.post("/api/job/url", json={"url": f"https://{HOST}/job"})

    assert response.status_code == 400
    assert len(requests) == 1


def test_relative_redirect_stays_on_the_validated_host(monkeypatch):
    """A relative Location resolves against the hop that issued it, and the
    new hop is validated and pinned like the first."""
    def _origin(request):
        if request.url.path == "/job":
            return httpx.Response(302, headers={"location": "/jobs/42"})
        return httpx.Response(200, content=b"<html><body>Role</body></html>")

    requests = fetch_fakes.install(monkeypatch, _origin)

    response = client.post("/api/job/url", json={"url": f"https://{HOST}/job"})

    assert response.status_code == 200
    assert [r.url.path for r in requests] == ["/job", "/jobs/42"]
    assert all(r.url.host == fetch_fakes.PUBLIC for r in requests)
    assert all(r.headers["host"] == HOST for r in requests)


def test_body_over_the_cap_is_413(monkeypatch):
    body = b"<html>" + b"x" * (MAX_HTML_BYTES + 1) + b"</html>"
    fetch_fakes.install(monkeypatch, fetch_fakes.html(body))

    response = client.post("/api/job/url", json={"url": f"https://{HOST}/job"})

    assert response.status_code == 413
    assert "too large" in response.json()["detail"].lower()


def test_body_at_the_cap_is_accepted(monkeypatch):
    """The ceiling is generous for a posting page; a body exactly at it passes."""
    inner = b"x" * (MAX_HTML_BYTES - len(b"<html><body></body></html>"))
    body = b"<html><body>" + inner + b"</body></html>"
    assert len(body) == MAX_HTML_BYTES
    fetch_fakes.install(monkeypatch, fetch_fakes.html(body))

    response = client.post("/api/job/url", json={"url": f"https://{HOST}/job"})

    assert response.status_code == 200


def test_redirect_chain_over_the_limit_is_502(monkeypatch):
    def _origin(request):
        return httpx.Response(302, headers={"location": f"https://{HOST}{request.url.path}/next"})

    requests = fetch_fakes.install(monkeypatch, _origin)

    response = client.post("/api/job/url", json={"url": f"https://{HOST}/job"})

    assert response.status_code == 502
    assert "redirects" in response.json()["detail"]
    assert len(requests) == MAX_REDIRECTS + 1


def test_redirect_without_a_destination_is_502(monkeypatch):
    fetch_fakes.install(monkeypatch, lambda request: httpx.Response(302))

    response = client.post("/api/job/url", json={"url": f"https://{HOST}/job"})

    assert response.status_code == 502
    assert "without a destination" in response.json()["detail"]


def test_upstream_error_status_is_passed_through(monkeypatch):
    fetch_fakes.install(monkeypatch, fetch_fakes.html(b"nope", status=503))

    response = client.post("/api/job/url", json={"url": f"https://{HOST}/job"})

    assert response.status_code == 503
    assert "HTTP 503" in response.json()["detail"]


def test_unresolvable_host_is_400(monkeypatch):
    fetch_fakes.install(monkeypatch, fetch_fakes.html(), resolve={})

    response = client.post("/api/job/url", json={"url": "https://no-such-host.example/job"})

    assert response.status_code == 400
    assert "resolve" in response.json()["detail"].lower()
