"""
Database engine and session management.

The connection URL comes from DATABASE_URL. It was previously hardcoded to a
local SQLite file while render.yaml and .env.example both set DATABASE_URL,
so the deployment's configuration was silently ignored and every environment
shared one filename.
"""
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker

load_dotenv()

DEFAULT_DATABASE_URL = "sqlite:///./firstplay.db"


def resolve_database_url(raw_url: str = None) -> str:
    """
    Normalise the configured database URL.

    Args:
        raw_url: Value of DATABASE_URL, or None to fall back to the default

    Returns:
        A URL SQLAlchemy 2.x accepts
    """
    url = raw_url or DEFAULT_DATABASE_URL

    # Render (and Heroku) hand out postgres:// URLs, which SQLAlchemy 2.x no
    # longer recognises — it wants the explicit driver name. Left unhandled,
    # this fails at startup with "Can't load plugin: sqlalchemy.dialects:postgres".
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    return url


SQLALCHEMY_DATABASE_URL = resolve_database_url(os.getenv("DATABASE_URL"))


def _engine_options(url: str) -> dict:
    """
    Connection arguments appropriate to the backend named by `url`.

    check_same_thread is a SQLite-only flag, and passing it to any other
    driver raises. It is needed here because FastAPI serves sync handlers from
    a threadpool, so a connection is not confined to the thread that made it.
    """
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}

    # Managed Postgres instances drop idle connections; pre-ping replaces a
    # dead one instead of surfacing it as a request error.
    return {"pool_pre_ping": True}


engine = create_engine(SQLALCHEMY_DATABASE_URL, **_engine_options(SQLALCHEMY_DATABASE_URL))

# Create SessionLocal class
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create Base class for ORM models
Base = declarative_base()


# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
