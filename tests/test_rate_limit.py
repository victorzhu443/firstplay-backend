"""
Tests for per-client rate limiting.

Every endpoint was unlimited, and most spend money — a pipeline run is four
LLM calls. On a public URL with no authentication, one caller in a loop
drains the OpenAI budget.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.main import app
from app.rate_limit import SlidingWindowLimiter, client_key, rate_limit

client = TestClient(app)


class FakeClock:
    """Controllable time, so window tests are exact rather than slow."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# --- the limiter ------------------------------------------------------------

def test_allows_up_to_the_limit():
    limiter = SlidingWindowLimiter(3, 60, clock=FakeClock())

    assert all(limiter.check("a") is None for _ in range(3))


def test_blocks_past_the_limit():
    limiter = SlidingWindowLimiter(3, 60, clock=FakeClock())
    for _ in range(3):
        limiter.check("a")

    retry_after = limiter.check("a")

    assert retry_after is not None
    assert 0 < retry_after <= 60


def test_keys_are_independent():
    """One caller hitting the limit must not block everyone else."""
    limiter = SlidingWindowLimiter(2, 60, clock=FakeClock())
    limiter.check("a")
    limiter.check("a")

    assert limiter.check("a") is not None
    assert limiter.check("b") is None


def test_window_slides_rather_than_resetting():
    """A fixed window lets a caller send 2x the limit across the boundary.

    Full allowance at the end of one window, full allowance at the start of
    the next. A sliding window ages out individual timestamps instead.
    """
    clock = FakeClock()
    limiter = SlidingWindowLimiter(2, 60, clock=clock)

    limiter.check("a")
    clock.advance(59)
    limiter.check("a")

    # Both still inside the window.
    assert limiter.check("a") is not None

    # The first ages out; exactly one slot frees up, not two.
    clock.advance(2)
    assert limiter.check("a") is None
    assert limiter.check("a") is not None


def test_retry_after_reflects_the_oldest_hit():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(1, 60, clock=clock)
    limiter.check("a")

    clock.advance(20)
    retry_after = limiter.check("a")

    assert retry_after == pytest.approx(40, abs=0.01)


# --- client identification --------------------------------------------------

def _request(headers=None, host="1.2.3.4"):
    scope_headers = [
        (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
    ]

    class _Req:
        def __init__(self):
            from starlette.datastructures import Headers

            self.headers = Headers(raw=scope_headers)
            self.client = type("C", (), {"host": host})()

    return _Req()


def test_direct_connection_uses_peer_address():
    assert client_key(_request(host="9.9.9.9")) == "9.9.9.9"


def test_forwarded_header_is_used_behind_a_proxy():
    """Otherwise every request looks like it came from the proxy, and one
    client's limit blocks everyone."""
    request = _request(headers={"X-Forwarded-For": "203.0.113.7"})

    assert client_key(request) == "203.0.113.7"


def test_spoofed_forwarded_entries_are_not_trusted():
    """A caller can send their own X-Forwarded-For and the proxy appends to
    it, so the leftmost entry is attacker-controlled. With one trusted proxy
    the believable entry is the last one."""
    request = _request(
        headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 203.0.113.7"}
    )

    assert client_key(request) == "203.0.113.7"
    assert client_key(request) != "1.1.1.1"


# --- the dependency ---------------------------------------------------------

def test_dependency_raises_429_with_retry_after():
    dependency = rate_limit(1, 60, "test endpoint")
    request = _request()

    dependency(request)

    with pytest.raises(HTTPException) as exc_info:
        dependency(request)

    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers
    assert int(exc_info.value.headers["Retry-After"]) > 0
    assert "test endpoint" in exc_info.value.detail


# --- wired to a real endpoint -----------------------------------------------

def test_pipeline_endpoint_enforces_its_limit():
    """The pipeline is four LLM calls, so it carries the tightest limit."""
    from app.rate_limit import pipeline_limit

    limit = pipeline_limit.limiter.max_requests

    with patch("app.routers.pipeline.run_pipeline") as mock_run:
        mock_run.side_effect = Exception("not reached")

        statuses = [
            client.post(
                "/api/pipeline/run", json={"resume_id": 1, "job_id": 2}
            ).status_code
            for _ in range(limit + 1)
        ]

    assert statuses[-1] == 429
    assert 429 not in statuses[:-1], "limit tripped earlier than configured"


def test_health_is_not_rate_limited():
    """The platform polls it; limiting it would cause restarts."""
    statuses = {client.get("/health").status_code for _ in range(80)}

    assert statuses == {200}
