"""
Tests that blocking work never runs on the event loop.

Every LLM-calling handler used to be `async def` while calling LangChain's
blocking `.invoke()`. That put a 30s call directly on the event loop, so a
single pipeline run stalled every other request in the worker — including
/health, which on Render means the platform restarts the service mid-request.

Two layers of protection here:
  1. A structural guard, so re-adding `async def` to a blocking handler fails
     loudly instead of silently reintroducing the stall.
  2. A behavioural test that actually races a slow request against /health.
"""
import asyncio
import inspect
import time
from unittest.mock import patch

import httpx
import pytest

from app.db import get_db
from app.main import app
from app.routers import analysis, job, pipeline, resume

# Handlers that perform blocking work (LLM calls, PDF parsing, sync DB I/O).
# FastAPI only moves a handler to its threadpool when it is declared `def`.
BLOCKING_HANDLERS = [
    resume.upload_resume,
    resume.parse_resume,
    resume.improve_resume_endpoint,
    job.submit_manual_jd,
    job.parse_job,
    analysis.analyze,
    analysis.generate_project_ideas,
    pipeline.run_full_pipeline,
]


@pytest.mark.parametrize(
    "handler", BLOCKING_HANDLERS, ids=lambda h: h.__name__
)
def test_blocking_handlers_are_not_async(handler):
    """A blocking handler declared `async def` runs on the event loop."""
    assert not inspect.iscoroutinefunction(handler), (
        f"{handler.__name__} is `async def` but performs blocking work. "
        "Declare it `def` so FastAPI runs it in a threadpool, or await the "
        "blocking call via run_in_threadpool."
    )


def test_url_handler_stays_async():
    """submit_job_url genuinely awaits, so it must remain a coroutine."""
    assert inspect.iscoroutinefunction(job.submit_job_url)


class _StubResume:
    """Minimal stand-in for a Resume row, so the test needs no database."""

    id = 1
    raw_text = "Some resume text"
    parsed_json = None


class _StubQuery:
    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return _StubResume()


class _StubSession:
    def query(self, *args, **kwargs):
        return _StubQuery()

    def commit(self):
        pass

    def refresh(self, obj):
        pass


@pytest.fixture
def stub_db():
    """Override get_db so the handler reaches the LLM call.

    Without this the test depends on a row existing in the local (gitignored)
    firstplay.db: on a fresh checkout the handler would 404 before reaching
    the blocking call and the test would pass without proving anything.
    """
    app.dependency_overrides[get_db] = lambda: _StubSession()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_slow_request_does_not_block_health_check(stub_db):
    """A slow LLM call must not delay an unrelated request.

    Races /api/resume/parse, whose LLM call sleeps, against /health. Timing is
    measured from a single origin taken before either request is launched: if
    the timer started after an `await`, a blocked event loop would delay that
    await too and the stall would be excluded from the measurement.
    """
    SLOW_SECONDS = 0.5

    def slow_parse(_text):
        time.sleep(SLOW_SECONDS)
        raise RuntimeError("deliberate: only the blocking window matters here")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("app.routers.resume.parse_resume_text", side_effect=slow_parse):
            origin = time.perf_counter()
            slow_task = asyncio.create_task(
                client.post("/api/resume/parse", params={"resume_id": 1})
            )

            # Yield so the slow request reaches its blocking call first.
            await asyncio.sleep(0.05)

            health_response = await client.get("/health")
            health_elapsed = time.perf_counter() - origin

            await slow_task
            slow_elapsed = time.perf_counter() - origin

    assert health_response.status_code == 200
    assert slow_elapsed >= SLOW_SECONDS, "the slow handler did not actually block"
    assert health_elapsed < SLOW_SECONDS, (
        f"/health completed at {health_elapsed:.3f}s, after the {SLOW_SECONDS}s "
        "blocking call finished; the handler is running on the event loop"
    )
