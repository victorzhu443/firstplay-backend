"""
Tests that the migrations and the ORM models describe the same schema.

The suite builds its tables with create_all() for speed, so nothing else here
would notice a migration that was never written for a model change. Deployed,
that divergence appears as an OperationalError on the first request touching
the new column.
"""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect

from app.db import Base
from app import models  # noqa: F401  (registers tables on Base.metadata)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_alembic(args, database_url):
    env = {**os.environ, "DATABASE_URL": database_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def migrated_db(tmp_path):
    """A database built by running the migrations, not create_all()."""
    db_path = tmp_path / "migrated.db"
    url = f"sqlite:///{db_path}"

    result = _run_alembic(["upgrade", "head"], url)
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"

    return url


def test_migrations_create_every_model_table(migrated_db):
    engine = create_engine(migrated_db)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    missing = set(Base.metadata.tables) - tables
    assert not missing, f"migrations do not create: {sorted(missing)}"


def test_migrations_and_models_agree_on_columns(migrated_db):
    """Catches a model column added without a matching migration."""
    engine = create_engine(migrated_db)
    try:
        inspector = inspect(engine)
        mismatches = {}

        for name, table in Base.metadata.tables.items():
            migrated_columns = {c["name"] for c in inspector.get_columns(name)}
            model_columns = set(table.columns.keys())

            if migrated_columns != model_columns:
                mismatches[name] = {
                    "only_in_models": sorted(model_columns - migrated_columns),
                    "only_in_migrations": sorted(migrated_columns - model_columns),
                }
    finally:
        engine.dispose()

    assert not mismatches, f"schema drift: {mismatches}"


def test_migrations_are_reversible(tmp_path):
    """A migration that cannot be rolled back is not a usable escape hatch."""
    url = f"sqlite:///{tmp_path / 'reversible.db'}"

    assert _run_alembic(["upgrade", "head"], url).returncode == 0

    downgrade = _run_alembic(["downgrade", "base"], url)
    assert downgrade.returncode == 0, f"downgrade failed:\n{downgrade.stderr}"

    engine = create_engine(url)
    try:
        remaining = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    # Alembic's own bookkeeping table is expected to survive.
    assert remaining <= {"alembic_version"}, f"downgrade left tables: {remaining}"


def test_no_unmigrated_model_changes(migrated_db):
    """Autogenerate against the migrated schema must find nothing to do.

    This is the check that fails when someone edits a model and forgets to
    generate the migration for it.
    """
    result = _run_alembic(
        ["check"], migrated_db
    )

    assert result.returncode == 0, (
        "models have changes with no corresponding migration; "
        f"run `alembic revision --autogenerate`:\n{result.stdout}\n{result.stderr}"
    )
