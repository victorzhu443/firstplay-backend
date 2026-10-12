"""
Pytest configuration shared by the whole suite.

The tests must never depend on — or spend — real OpenAI credentials. Several
tests construct a ChatOpenAI client (without calling it), and get_llm() raises
if OPENAI_API_KEY is unset, so we install a dummy key for the test session.

This is set at import time as well as per-test: conftest is imported before any
test module, and app.llm_client calls load_dotenv() at import. load_dotenv()
does not override variables that are already set, so seeding the dummy here
guarantees a developer's real key in .env is never picked up by the suite.
"""
import os
import tempfile

import pytest

TEST_API_KEY = "sk-test-dummy-key-not-a-real-credential"

os.environ["OPENAI_API_KEY"] = TEST_API_KEY

# Pin the provider for the same reason the key above is seeded: app.llm_client
# calls load_dotenv() at import, and a developer with LLM_PROVIDER=cohere in
# .env made get_llm() return a ChatCohere where the provider tests assert
# ChatOpenAI — 6 failures that had nothing to do with the code under test.
os.environ["LLM_PROVIDER"] = "openai"

# The model key is pinned too (audit 2026-10-12, F29). app.jev_client calls
# load_dotenv() at import, so a developer's real OPENROUTER_API_KEY in .env
# would otherwise decide whether /plan builds a JevBinder — and that binder
# would try the network. An empty value is "not configured" to JevClient and
# blocks load_dotenv (which never overrides a variable that is present, even
# empty). A test that wants a key sets one through monkeypatch; a test that
# wants none inherits this.
os.environ["OPENROUTER_API_KEY"] = ""

# Point the suite at a throwaway database, set before app.db is imported since
# it binds its engine at import time. Without this the tests share the
# developer's working firstplay.db: they accumulated rows in it run after run,
# and tests could pass by accidentally matching real local data rather than
# what they set up themselves.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="firstplay-tests-")
TEST_DATABASE_URL = f"sqlite:///{os.path.join(_TEST_DB_DIR, 'test.db')}"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

# Imported after the environment is seeded: app.llm_client calls load_dotenv()
# at import time, app.db reads DATABASE_URL at import time, and app.models must
# be imported for its tables to be registered on Base.metadata before
# create_all() runs.
from app.db import Base, engine  # noqa: E402
from app import models  # noqa: E402,F401
# Imported now, while the sentinels above are in place, so that both modules'
# load_dotenv() calls have already run and cannot be reached later by a lazy
# import inside a test that has unset a variable.
from app import jev_client, llm_client  # noqa: E402,F401


@pytest.fixture(scope="session", autouse=True)
def create_database_tables():
    """Create the schema before any test runs.

    Nothing in the suite reliably creates tables. app/main.py creates them in
    a startup event, but that never fires: the tests instantiate
    `TestClient(app)` at module level rather than as a context manager, and
    Starlette only runs lifespan events for a client used as a context
    manager. The only other creation is a side effect of tests/test_db.py
    calling create_all() directly.

    Alphabetical collection puts test_analysis.py before test_db.py, so on a
    cold database it queried tables that did not exist yet and failed with
    OperationalError. The run then left firstplay.db on disk with the tables
    in place, so every later run passed — hiding the failure locally while
    breaking CI, which always starts cold.
    """
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def test_database_url(monkeypatch):
    """Keep DATABASE_URL pointing at the throwaway database for every test."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)


@pytest.fixture(autouse=True)
def reset_rate_limiters():
    """Clear rate-limit counters between tests.

    The limiters are module-level, so without this they accumulate across the
    whole session: the suite would pass only while it happened to stay under
    the limits, and adding tests later would trip them as confusing 429s in
    unrelated places. Tests for the limiter itself build their own.
    """
    from app import rate_limit

    for dependency in (
        rate_limit.pipeline_limit,
        rate_limit.llm_limit,
        rate_limit.ingest_limit,
        rate_limit.autofill_limit,
    ):
        dependency.limiter.reset()

    yield


@pytest.fixture(autouse=True)
def reset_autofill_process_state(monkeypatch):
    """Fresh theme/option caches and model-status records for every test.

    The autofill router holds process-wide state on purpose (a theme cache
    shared across callers, the last model error for /health). Shared across
    tests it is an order dependency: a test that tripped the model path left
    `model_degraded` true for the next one, and a cached theme or option
    match from one test answered a question in another without a model call
    being observed (audit 2026-10-12, F29). The caches are replaced rather
    than cleared so a test keeping a reference to the old one is unaffected.
    """
    from app.autofill.jev_binder import OptionCache, ThemeCache
    from app.routers import autofill as router

    monkeypatch.setattr(router, "_THEME_CACHE", ThemeCache())
    monkeypatch.setattr(router, "_OPTION_CACHE", OptionCache())
    router._MODEL_STATUS.clear()
    router._MODEL_STATUS.update({"last_error": None, "last_error_at": None, "last_ok_at": None})
    router._CALLER_STATUS.clear()
    router._MODEL_SEQ["n"] = 0
    yield
    router._MODEL_STATUS.clear()
    router._MODEL_STATUS.update({"last_error": None, "last_error_at": None, "last_ok_at": None})
    router._CALLER_STATUS.clear()
    router._MODEL_SEQ["n"] = 0


@pytest.fixture(autouse=True)
def stub_dns_resolution(monkeypatch):
    """Resolve every hostname to a public address, without touching the network.

    The job fetcher resolves each URL to check it is not pointing at
    localhost, the private network, or the cloud metadata service. Left
    unstubbed, that makes the suite depend on working DNS — the same class of
    hidden environment dependency as the database and the PDF fixture.

    An SSRF test overrides this to return the address it wants to exercise.
    """
    import ipaddress

    from app.routers import job as job_router

    monkeypatch.setattr(
        job_router,
        "_resolved_addresses",
        lambda hostname: [ipaddress.ip_address("93.184.216.34")],
    )


@pytest.fixture(scope="session", autouse=True)
def sample_resume_pdf():
    """Ensure the sample resume PDF the upload tests read exists.

    The file is gitignored, so it is absent from a fresh clone, and its
    generator's dependency (reportlab) was undeclared. The suite therefore
    passed only on a machine where someone had run the generator by hand.
    Generating it here removes the undocumented setup step.
    """
    path = os.path.join(os.path.dirname(__file__), "tests", "fixtures", "sample_resume.pdf")

    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas

        pdf = canvas.Canvas(path, pagesize=letter)
        for offset, line in enumerate(
            [
                "JOHN DOE",
                "Software Engineer",
                "",
                "SKILLS",
                "Python, JavaScript, React, FastAPI, SQL",
                "",
                "EXPERIENCE",
                "Software Developer at Tech Company",
                "Built web applications using modern frameworks",
            ]
        ):
            pdf.drawString(100, 750 - offset * 20, line)
        pdf.save()

    return path


@pytest.fixture(autouse=True)
def dummy_openai_key(monkeypatch):
    """Guarantee every test starts with the dummy key in place.

    Autouse so that a test which mutates OPENAI_API_KEY (e.g. the one asserting
    get_llm() raises when it is missing) cannot leak that mutation into the
    tests that run after it. monkeypatch undoes the change automatically.
    """
    monkeypatch.setenv("OPENAI_API_KEY", TEST_API_KEY)


@pytest.fixture(autouse=True)
def no_model_key_unless_set(monkeypatch):
    """Every test starts without an OpenRouter key (audit 2026-10-12, F29).

    Set, not deleted: an absent variable is one a later load_dotenv() would
    fill from a developer's .env, while an empty one is left alone and reads
    as "not configured". A test that needs the model path configured sets
    `OPENROUTER_API_KEY` itself (the endpoint tests use "sk-or-test-…") and
    must then fake the client, since a configured JevBinder calls the network.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
