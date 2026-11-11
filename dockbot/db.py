"""Подключение к PostgreSQL и миграции Alembic."""

from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dockbot.config import PROJECT_ROOT

log = logging.getLogger(__name__)


def make_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def upgrade_head() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    log.info("applying database migrations")
    command.upgrade(config, "head")
    log.info("database schema is at head")


def check_connection(database_url: str) -> None:
    engine = make_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()
    log.info("database connection ok")
