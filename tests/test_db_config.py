"""
Tests for database URL resolution.

DATABASE_URL was set in render.yaml and .env.example and read by neither:
app/db.py hardcoded a local SQLite path, so the deployment's configuration was
silently ignored.
"""
import pytest

from app.db import DEFAULT_DATABASE_URL, _engine_options, resolve_database_url


def test_falls_back_to_sqlite_when_unset():
    assert resolve_database_url(None) == DEFAULT_DATABASE_URL
    assert resolve_database_url("") == DEFAULT_DATABASE_URL


def test_uses_the_configured_url():
    assert resolve_database_url("sqlite:///./other.db") == "sqlite:///./other.db"


def test_render_postgres_scheme_is_normalised():
    """Render emits postgres://, which SQLAlchemy 2.x refuses to load.

    Left unhandled this fails at startup with
    "Can't load plugin: sqlalchemy.dialects:postgres".
    """
    resolved = resolve_database_url("postgres://user:pw@host:5432/dbname")

    assert resolved == "postgresql://user:pw@host:5432/dbname"


def test_only_the_scheme_is_rewritten():
    """A password or database name containing the scheme must survive."""
    resolved = resolve_database_url("postgres://user:postgres://x@host/db")

    assert resolved.startswith("postgresql://")
    assert resolved.count("postgresql://") == 1


def test_already_normalised_url_is_untouched():
    url = "postgresql://user:pw@host:5432/dbname"

    assert resolve_database_url(url) == url


# --- engine options must match the backend ----------------------------------

def test_sqlite_gets_check_same_thread():
    """FastAPI serves sync handlers from a threadpool, so a connection is not
    confined to the thread that opened it."""
    options = _engine_options("sqlite:///./x.db")

    assert options["connect_args"]["check_same_thread"] is False


def test_postgres_does_not_get_sqlite_only_args():
    """Passing check_same_thread to psycopg2 raises."""
    options = _engine_options("postgresql://user:pw@host/db")

    assert "connect_args" not in options
    assert options["pool_pre_ping"] is True
