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
