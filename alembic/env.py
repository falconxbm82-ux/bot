"""Alembic environment. The database URL comes from the environment, not from alembic.ini."""

from __future__ import annotations

import logging
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dockbot.config import ConfigError, load_settings
from dockbot.models import Base

config = context.config

# The bot process configures logging before migrations. fileConfig would
# replace that setup and hide the polling log. The Alembic CLI has no
# handlers yet, so it still gets the ini configuration.
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name)

try:
    settings = load_settings()
except ConfigError as exc:
    raise RuntimeError(str(exc)) from exc

if not settings.database_url:
    raise RuntimeError("Не задано: DATABASE_URL")

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = settings.database_url
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
