"""Точка входа: миграции PostgreSQL и опрос Telegram.

Запуск из корня репозитория: python -m dockbot
"""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback

from sqlalchemy import text

from dockbot.bot.app import run_bot
from dockbot.config import ConfigError, load_settings, redact
from dockbot.adminboard import ensure_stand_staff
from dockbot.configure import apply_configuration
from dockbot.db import make_engine, upgrade_head
from dockbot.logging_setup import resolve_log_directory, setup_logging

log = logging.getLogger("dockbot")


def main() -> None:
    setup_logging(None)
    secrets: list[str] = []
    engine = None
    try:
        settings = load_settings()
        secrets = settings.secrets()
        setup_logging(resolve_log_directory())
        log.info("process starting")
        settings.require_bot()
        engine = make_engine(settings.database_url)
        try:
            upgrade_head()
            apply_configuration(engine, settings)
            ensure_stand_staff(engine)
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            log.info("database connection ok")
            asyncio.run(run_bot(settings, engine))
        finally:
            log.info("process stopping")
            if engine is not None:
                engine.dispose()
    except ConfigError as exc:
        log.error("%s", redact(str(exc), secrets))
        sys.exit(2)
    except Exception as exc:
        rendered = redact("".join(traceback.format_exception(exc)), secrets)
        log.error("process stopped\n%s", rendered)
        sys.exit(1)


if __name__ == "__main__":
    main()
