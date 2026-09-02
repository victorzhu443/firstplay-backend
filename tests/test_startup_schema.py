"""
Tests that the app brings its own schema up at startup.

Regression cover for a production outage. The schema was created by
`alembic upgrade head` in render.yaml's start command — but the service's
start command was configured in the hosting dashboard, so render.yaml's was
never used and the migration silently never ran. The app started, served
/health and /docs happily, and returned 500 on every request that touched a
table:

    sqlite3.OperationalError: no such table: resumes

Nothing in the deployment was wrong enough to fail visibly, which is what
made it take a production outage to notice. The app now migrates itself.
"""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _boot_app(database_url, env=None):
    """Start the app in a subprocess against `database_url` and hit /health.

    A subprocess because app.db binds its engine at import time, so pointing
    the app at a different database means importing it fresh.
    """
    script = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "with TestClient(app) as c:\n"
        "    print('HEALTH', c.get('/health').status_code)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url, **(env or {})},
        capture_output=True,
        text=True,
    )
    # Application logs go to stdout; warnings and tracebacks to stderr.
    # Assert against both so a test never depends on which stream a line took.
    result.output = result.stdout + result.stderr
    return result


def _tables(database_url):
    engine = create_engine(database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


@pytest.fixture
def fresh_db(tmp_path):
    """A database file that no migration has ever touched."""
    return f"sqlite:///{tmp_path / 'fresh.db'}"


def test_startup_migrates_an_empty_database(fresh_db):
    """The outage: nothing had created the schema and the app started anyway."""
    result = _boot_app(fresh_db)

    assert "HEALTH 200" in result.stdout, result.output
    assert {"resumes", "job_descriptions", "gap_analyses"} <= _tables(fresh_db)


def test_startup_reports_what_it_did(fresh_db):
    result = _boot_app(fresh_db)

    assert "Database is missing 5 table(s)" in result.output
    assert "Applying migrations" in result.output
    assert "Database schema is ready" in result.output


def test_second_startup_is_a_no_op(fresh_db):
    """Migrating on every boot must not re-run work or fail the second time."""
    _boot_app(fresh_db)
    result = _boot_app(fresh_db)

    assert "HEALTH 200" in result.stdout, result.output
    assert "Database schema is present" in result.output
    assert "Applying migrations" not in result.output


def test_opt_out_refuses_to_migrate(fresh_db):
    """Several instances migrating at once can race, so it must be disableable."""
    result = _boot_app(fresh_db, env={"MIGRATE_ON_STARTUP": "false"})

    assert "HEALTH 200" in result.stdout, result.output
    assert "MIGRATE_ON_STARTUP is disabled" in result.output
    assert _tables(fresh_db) == set(), "it migrated despite being told not to"


def test_health_still_answers_when_the_database_is_unusable(tmp_path):
    """A service that refuses to start gives no way to reach its logs.

    /health answering while the database is broken is exactly what allowed
    the outage to be diagnosed remotely, so an unreachable database must
    degrade the app rather than stop it booting.
    """
    # A directory, not a file: SQLite cannot open this at all.
    unusable = tmp_path / "not-a-file"
    unusable.mkdir()

    result = _boot_app(f"sqlite:///{unusable}")

    assert "HEALTH 200" in result.stdout, result.output
    # Fails at inspection, before a migration is attempted; either way it must
    # say so rather than dying silently.
    assert "ERROR" in result.output
    assert "database" in result.output.lower()
    assert "Traceback" in result.output, "the cause must be in the logs"


def test_database_url_is_never_logged(fresh_db):
    """A managed Postgres connection string carries the password."""
    result = _boot_app(fresh_db)

    assert "database backend: sqlite" in result.output
    assert fresh_db not in result.output
