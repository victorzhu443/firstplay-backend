import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import resume, job, analysis, pipeline
from app.db import engine, Base, SQLALCHEMY_DATABASE_URL
from app.logging_config import configure_logging
from app.models import Resume, JobDescription, GapAnalysis, ProjectPlan, ImprovedResume

logger = logging.getLogger(__name__)


BASE_DIR = Path(__file__).resolve().parent.parent

# Migrating at startup is belt-and-braces for a start command that is supposed
# to have done it already. It exists because the alternative failed in
# production: a service whose start command is configured in the hosting
# dashboard never runs the one in render.yaml, so `alembic upgrade head` was
# silently skipped and the app served /health happily while every request that
# touched a table returned 500.
#
# Safe for a single instance, which is what this app runs. Concurrent
# migrations from several instances starting at once can race, so set
# MIGRATE_ON_STARTUP=false and migrate from a release step when scaling out.
MIGRATE_ON_STARTUP = os.getenv("MIGRATE_ON_STARTUP", "true").lower() not in (
    "false",
    "0",
    "no",
)


def _missing_tables() -> set:
    """Tables the models expect that the database does not have."""
    from sqlalchemy import inspect

    return set(Base.metadata.tables) - set(inspect(engine).get_table_names())


def _ensure_schema() -> None:
    """Bring the database up to head, and say plainly what happened.

    Running `alembic upgrade head` here is idempotent: it is a no-op when the
    schema is already current, so the normal case costs one query.
    """
    try:
        missing = _missing_tables()
    except Exception:
        # Inspecting can fail outright when the database is unreachable. That
        # must not stop the app booting: a process that exits here leaves no
        # way to reach the logs, and /health answering is what makes the
        # failure diagnosable from outside.
        logger.exception(
            "Could not inspect the database. The API will start, but requests "
            "touching it will return errors until this is resolved."
        )
        return

    if not missing:
        logger.info(
            "Database schema is present (%d tables)", len(Base.metadata.tables)
        )
        return

    logger.warning(
        "Database is missing %d table(s): %s", len(missing), ", ".join(sorted(missing))
    )

    if not MIGRATE_ON_STARTUP:
        logger.error(
            "MIGRATE_ON_STARTUP is disabled and the schema is incomplete. "
            "Run `alembic upgrade head`; requests touching these tables will fail."
        )
        return

    logger.info("Applying migrations")

    try:
        from alembic import command
        from alembic.config import Config

        config = Config(str(BASE_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BASE_DIR / "migrations"))
        # Keep our logging handler; see the guard in migrations/env.py.
        config.attributes["configure_logger"] = False
        command.upgrade(config, "head")
    except Exception:
        # Logged rather than raised: a service that refuses to start gives no
        # way to reach the logs on some platforms, and /health still answering
        # makes the failure diagnosable.
        logger.exception(
            "Migrations failed. The API will start, but requests touching the "
            "database will return errors until this is resolved."
        )
        return

    still_missing = _missing_tables()

    if still_missing:
        logger.error(
            "Migrations ran but %d table(s) are still missing: %s",
            len(still_missing),
            ", ".join(sorted(still_missing)),
        )
    else:
        logger.info(
            "Database schema is ready (%d tables)", len(Base.metadata.tables)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown. Replaces the deprecated @app.on_event hooks."""
    configure_logging()

    # Log the backend, never the URL itself — a managed Postgres connection
    # string carries the password.
    backend = SQLALCHEMY_DATABASE_URL.split("://", 1)[0]
    logger.info("Starting FirstPlay Coach API (database backend: %s)", backend)

    # Schema is owned by Alembic. The start command is meant to have migrated
    # already; this makes the app correct even when it hasn't.
    _ensure_schema()

    yield

    logger.info("Shutting down")


app = FastAPI(
    title="FirstPlay Coach API",
    description="Resume analysis and project planning for early-career CS students",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware.
# `allow_origins` is matched by exact string comparison, so the previous
# "https://*.vercel.app" entry matched nothing and every Vercel preview
# deployment was silently blocked. Wildcards belong in allow_origin_regex.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "https://firstplay-frontend.vercel.app",
    ],
    # Vercel preview deployments: <project>-<hash>-<scope>.vercel.app
    allow_origin_regex=r"https://[a-zA-Z0-9-]+\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(resume.router)
app.include_router(job.router)
app.include_router(analysis.router)
app.include_router(pipeline.router)

@app.get("/")
def read_root():
    return {
        "message": "Welcome to FirstPlay Coach API",
        "docs": "/docs",
        "health": "/health"
    }

@app.get("/health")
def health_check():
    return {"status": "ok"}

