"""
Alembic environment.

The connection URL is taken from the application rather than from
alembic.ini, so migrations always target the same database the app does and
there is no second place to keep a connection string in sync.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Importing app.db resolves DATABASE_URL exactly as the application does,
# including the postgres:// -> postgresql:// rewrite Render's URLs need.
from app.db import SQLALCHEMY_DATABASE_URL, Base
from app import models  # noqa: F401  (registers tables on Base.metadata)

config = context.config

# Injected here rather than written into alembic.ini, which is committed and
# must not carry a database password.
config.set_main_option("sqlalchemy.url", SQLALCHEMY_DATABASE_URL)

# fileConfig() reconfigures the root logger from alembic.ini, which removes
# handlers the application installed. When the app runs a migration itself at
# startup, that silences its own logging for the rest of the process, so it
# passes configure_logger=False and keeps its handler.
if config.config_file_name is not None and config.attributes.get(
    "configure_logger", True
):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=SQLALCHEMY_DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rebuilds
            # the table instead. Harmless on Postgres.
            render_as_batch=SQLALCHEMY_DATABASE_URL.startswith("sqlite"),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
