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

import pytest

TEST_API_KEY = "sk-test-dummy-key-not-a-real-credential"

os.environ["OPENAI_API_KEY"] = TEST_API_KEY

# Imported after the key is seeded: app.llm_client calls load_dotenv() at
# import time, and app.models must be imported for its tables to be registered
# on Base.metadata before create_all() runs.
from app.db import Base, engine  # noqa: E402
from app import models  # noqa: E402,F401


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
def dummy_openai_key(monkeypatch):
    """Guarantee every test starts with the dummy key in place.

    Autouse so that a test which mutates OPENAI_API_KEY (e.g. the one asserting
    get_llm() raises when it is missing) cannot leak that mutation into the
    tests that run after it. monkeypatch undoes the change automatically.
    """
    monkeypatch.setenv("OPENAI_API_KEY", TEST_API_KEY)
