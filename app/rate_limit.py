"""
Per-client rate limiting.

Every endpoint was unlimited, and most of them spend money: a pipeline run is
four LLM calls. On a public URL with no authentication, one caller in a loop
drains the OpenAI budget. These limits are about cost, not abuse detection.

State is in-process, which is deliberate but has consequences worth knowing:
counters reset on restart, and they are per-instance, so scaling to more than
one instance multiplies the effective limit by the instance count. Shared
limits need Redis; this app runs a single instance.
"""
import os
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional

from fastapi import HTTPException, Request

# Number of proxies in front of the app that append to X-Forwarded-For.
# Render puts exactly one there. The value decides which entry of that header
# is believed, so getting it wrong either lumps every client together (too
# low is not the failure — too *high* trusts a spoofed entry).
TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "1"))


def client_key(request: Request) -> str:
    """
    Identify the caller for rate-limiting purposes.

    Behind a proxy every request appears to come from the proxy, so one
    client's limit would block everyone. X-Forwarded-For carries the real
    address, but a caller can send that header themselves and the proxy
    appends rather than replaces — so the leftmost entry is attacker
    controlled and the trustworthy one is counted from the right, by however
    many proxies actually sit in front of us.

    Args:
        request: The incoming request

    Returns:
        A stable key for the caller
    """
    forwarded = request.headers.get("x-forwarded-for")

    if forwarded and TRUSTED_PROXY_HOPS > 0:
        hops = [part.strip() for part in forwarded.split(",") if part.strip()]
        if hops:
            # With one trusted proxy, hops[-1] is the address it observed.
            index = max(0, len(hops) - TRUSTED_PROXY_HOPS)
            return hops[index]

    return request.client.host if request.client else "unknown"


class SlidingWindowLimiter:
    """
    Counts requests per key over a rolling window.

    A fixed window would let a caller send the full allowance at the end of
    one window and again at the start of the next, so a burst of twice the
    limit passes. This drops timestamps that have aged out instead.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits: Dict[str, Deque[float]] = {}
        # Handlers run in FastAPI's threadpool, so this is touched concurrently.
        self._lock = threading.Lock()

    def check(self, key: str) -> Optional[float]:
        """
        Record a request and report whether it exceeds the limit.

        Args:
            key: Caller identity

        Returns:
            None if allowed, otherwise seconds until the caller may retry
        """
        now = self._clock()
        cutoff = now - self.window_seconds

        with self._lock:
            hits = self._hits.setdefault(key, deque())

            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self.max_requests:
                # The oldest hit still in the window is what has to age out.
                return max(0.0, hits[0] + self.window_seconds - now)

            hits.append(now)

            # Keys are never revisited once idle, so drop empty ones rather
            # than growing the map by one entry per client seen, forever.
            if not hits:
                del self._hits[key]

            return None

    def reset(self) -> None:
        """Drop all counters. For tests."""
        with self._lock:
            self._hits.clear()


def rate_limit(max_requests: int, window_seconds: float, name: str):
    """
    Build a FastAPI dependency enforcing a limit.

    Args:
        max_requests: Requests permitted per window
        window_seconds: Window length
        name: Used in the error message so a caller knows which limit they hit

    Returns:
        A dependency raising 429 when the limit is exceeded
    """
    limiter = SlidingWindowLimiter(max_requests, window_seconds)

    def dependency(request: Request) -> None:
        retry_after = limiter.check(client_key(request))

        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Rate limit exceeded for {name}: "
                    f"{max_requests} requests per "
                    f"{int(window_seconds // 60)} minutes."
                ),
                # Standard, and it tells a well-behaved client when to come
                # back instead of having it retry immediately.
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

    dependency.limiter = limiter
    return dependency


_HOUR = 3600

# Four LLM calls per request; by far the most expensive thing on offer.
pipeline_limit = rate_limit(
    int(os.getenv("RATE_LIMIT_PIPELINE", "10")), _HOUR, "the pipeline"
)

# One LLM call each.
llm_limit = rate_limit(
    int(os.getenv("RATE_LIMIT_LLM", "30")), _HOUR, "LLM-backed endpoints"
)

# No LLM call, but each does real work: PDF extraction, or an outbound fetch.
ingest_limit = rate_limit(
    int(os.getenv("RATE_LIMIT_INGEST", "60")), _HOUR, "uploads and fetches"
)
