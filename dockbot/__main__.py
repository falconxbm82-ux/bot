"""Точка входа: миграции PostgreSQL и опрос Telegram.

Запуск из корня репозитория: python -m dockbot
"""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback

from dockbot.bot.app import run_bot
from dockbot.config import ConfigError, load_settings, redact
from dockbot.db import check_connection, upgrade_head

log = logging.getLogger("dockbot")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    secrets: list[str] = []
    try:
        settings = load_settings()
        secrets = settings.secrets()
        settings.require_bot()
        if not settings.site_is_complete:
            log.info(
                "site settings are incomplete; reminders are not started in this process"
            )
        upgrade_head()
        check_connection(settings.database_url)
        asyncio.run(run_bot(settings))
    except ConfigError as exc:
        log.error("%s", redact(str(exc), secrets))
        sys.exit(2)
    except Exception as exc:
        rendered = redact("".join(traceback.format_exception(exc)), secrets)
        log.error("process stopped\n%s", rendered)
        sys.exit(1)


if __name__ == "__main__":
    main()
