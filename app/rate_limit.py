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
from collections import OrderedDict, deque
from typing import Callable, Deque, Optional

from fastapi import HTTPException, Request

# Number of proxies in front of the app that append to X-Forwarded-For.
# Render puts exactly one there. The value decides which entry of that header
# is believed, so getting it wrong either lumps every client together (too
# low is not the failure — too *high* trusts a spoofed entry).
TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "1"))

#: The per-address allowance is this many times the per-install one. An
#: install token is client-chosen, so a caller who mints a fresh token per
#: request used to get a fresh allowance per request (audit 2026-10-12, F10;
#: probe p06). Addresses are shared — a campus NAT puts a dorm behind one —
#: so the address cap is a multiple, not the same number: 5 × 600 plans an
#: hour from one address before anyone there is refused.
IP_LIMIT_MULTIPLIER = int(os.getenv("RATE_LIMIT_IP_MULTIPLIER", "5"))

#: Distinct keys one limiter remembers. Idle keys are swept every window;
#: this is the hard bound underneath, least recently seen dropped first,
#: so a flood of fresh tokens (50,000 retained in the probe) costs memory
#: proportional to this and not to the flood.
MAX_TRACKED_KEYS = int(os.getenv("RATE_LIMIT_MAX_KEYS", "10000"))


def install_token(request: Request) -> Optional[str]:
    """The extension's install token, when the header carries a well-formed one.

    A random token generated on first run (X-FirstPlay-Install): an opaque
    UUID, never tied to a person, used only as a limiter key and as the key
    of the per-caller model-status record.
    """
    install = (request.headers.get("x-firstplay-install") or "").strip()
    if install and len(install) <= 64 and install.replace("-", "").isalnum():
        return install
    return None


def client_ip(request: Request) -> str:
    """
    The caller's network address as the trusted proxy observed it.

    Behind a proxy every request appears to come from the proxy, so one
    client's limit would block everyone. X-Forwarded-For carries the real
    address, but a caller can send that header themselves and the proxy
    appends rather than replaces — so the leftmost entry is attacker
    controlled and the trustworthy one is counted from the right, by however
    many proxies actually sit in front of us.
    """
    forwarded = request.headers.get("x-forwarded-for")

    if forwarded and TRUSTED_PROXY_HOPS > 0:
        hops = [part.strip() for part in forwarded.split(",") if part.strip()]
        if hops:
            # With one trusted proxy, hops[-1] is the address it observed.
            index = max(0, len(hops) - TRUSTED_PROXY_HOPS)
            return hops[index]

    return request.client.host if request.client else "unknown"


def client_key(request: Request) -> str:
    """
    Identify the caller: the install token when there is one, else the address.

    Behind Render's proxy many installs share an address; the token keeps
    one applicant's record theirs. Used for the per-caller model-status
    record; the limiter keys on `limit_key`, which binds the token to the
    address it arrived from.

    Args:
        request: The incoming request

    Returns:
        A stable key for the caller
    """
    install = install_token(request)
    if install:
        return "install:" + install
    return client_ip(request)


def limit_key(request: Request) -> str:
    """The per-caller limiter key: (install token, address), or the address.

    The token alone was the whole key, and the token is whatever the caller
    sends. Bound to the address, a rotated token from one address still
    lands under that address's own cap (`IP_LIMIT_MULTIPLIER`); a laptop
    that changes networks gets a fresh per-install bucket, which costs
    nothing.
    """
    install = install_token(request)
    address = client_ip(request)
    if install:
        return "install:{}@{}".format(install, address)
    return "ip:" + address


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
        max_keys: int = MAX_TRACKED_KEYS,
    ):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_keys = max(1, int(max_keys))
        self._clock = clock
        # Least recently seen first, so the bound below evicts the right key.
        self._hits: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._last_sweep = clock()
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
            self._sweep(cutoff, now)

            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = deque()
                while len(self._hits) > self.max_keys:
                    self._hits.popitem(last=False)
            else:
                self._hits.move_to_end(key)

            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= self.max_requests:
                # The oldest hit still in the window is what has to age out.
                return max(0.0, hits[0] + self.window_seconds - now)

            hits.append(now)
            return None

    def _sweep(self, cutoff: float, now: float) -> None:
        """Drop keys whose every hit has aged out. Call under the lock.

        The previous version had this as a dead branch after `append` (a
        deque that was just appended to is never empty), so the map grew by
        one entry per client seen, forever (audit 2026-10-12, F10). Once a
        window: amortised to nothing, and a key is never kept idle longer
        than two windows.
        """
        if now - self._last_sweep < self.window_seconds:
            return
        self._last_sweep = now
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]

    @property
    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._hits)

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
    # Every request also counts against the address it came from, token or
    # not, so minting tokens buys nothing past the address's own allowance.
    ip_limiter = SlidingWindowLimiter(max_requests * IP_LIMIT_MULTIPLIER, window_seconds)

    def dependency(request: Request) -> None:
        retry_after = limiter.check(limit_key(request))
        scope = ""

        if retry_after is None:
            retry_after = ip_limiter.check(client_ip(request))
            scope = " from this network address"

        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Rate limit exceeded for {name}{scope}: "
                    f"{max_requests if not scope else max_requests * IP_LIMIT_MULTIPLIER} requests per "
                    f"{int(window_seconds // 60)} minutes."
                ),
                # Standard, and it tells a well-behaved client when to come
                # back instead of having it retry immediately.
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

    dependency.limiter = limiter
    dependency.ip_limiter = ip_limiter
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

# Autofill plans: one per page the applicant opens, from their own machine.
# 30/hour (the LLM limit) refused a normal evening of applying — the extension
# logged "backend returned 429" on live forms. A plan costs at most one model
# round-trip and about a tenth of a cent; 600/hour bounds a runaway loop
# without touching a person.
autofill_limit = rate_limit(
    int(os.getenv("RATE_LIMIT_AUTOFILL", "600")), _HOUR, "autofill plans"
)

# No LLM call, but each does real work: PDF extraction, or an outbound fetch.
ingest_limit = rate_limit(
    int(os.getenv("RATE_LIMIT_INGEST", "60")), _HOUR, "uploads and fetches"
)
