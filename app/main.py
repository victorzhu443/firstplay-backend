import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import resume, job, analysis, pipeline
from app.db import engine, Base, SQLALCHEMY_DATABASE_URL
from app.logging_config import configure_logging
from app.models import Resume, JobDescription, GapAnalysis, ProjectPlan, ImprovedResume

logger = logging.getLogger(__name__)


def _warn_if_schema_missing() -> None:
    """Fail loudly at startup rather than per-request if migrations never ran.

    Without this, a deployment that skipped `alembic upgrade head` starts
    healthy and then returns OperationalError on the first request that
    touches a table.
    """
    from sqlalchemy import inspect

    present = set(inspect(engine).get_table_names())
    expected = set(Base.metadata.tables)
    missing = expected - present

    if missing:
        logger.error(
            "Database is missing %d table(s): %s. "
            "Run `alembic upgrade head`.",
            len(missing),
            ", ".join(sorted(missing)),
        )
    else:
        logger.info("Database schema is present (%d tables)", len(expected))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown. Replaces the deprecated @app.on_event hooks."""
    configure_logging()

    # Log the backend, never the URL itself — a managed Postgres connection
    # string carries the password.
    backend = SQLALCHEMY_DATABASE_URL.split("://", 1)[0]
    logger.info("Starting FirstPlay Coach API (database backend: %s)", backend)

    # Schema is owned by Alembic; the deployment runs `alembic upgrade head`
    # before this process starts. create_all() used to run here, which cannot
    # apply a change to an existing table and would silently diverge from the
    # migrations once both existed.
    _warn_if_schema_missing()

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

