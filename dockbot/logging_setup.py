"""Журнал процесса.

На Windows без LOG_DIR каталог C:\\Apps\\DockBot\\logs.
На другой ОС файл пишется только если каталог задан, чтобы тот же код
можно было проверить не на Windows.
Размер ротации — стендовый, не норма ТЗ.
PIN пропуска, PIN персонала и токен бота в эти строки не попадают.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LINE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
WINDOWS_LOG_DIR = Path(r"C:\Apps\DockBot\logs")
# Стенд: 10 МиБ и 10 архивов, чтобы журнал не заполнил диск.
ROTATE_BYTES = 10 * 1024 * 1024
ROTATE_BACKUPS = 10


def resolve_log_directory(
    explicit: str | None = None,
    *,
    platform: str | None = None,
) -> Path | None:
    raw = os.environ.get("LOG_DIR", "") if explicit is None else explicit
    text = raw.strip()
    if text:
        return Path(text)
    current = sys.platform if platform is None else platform
    if current == "win32":
        return WINDOWS_LOG_DIR
    return None


def setup_logging(
    directory: Path | None,
    *,
    max_bytes: int = ROTATE_BYTES,
    backups: int = ROTATE_BACKUPS,
) -> Path | None:
    """Консоль всегда. Файл — если каталог задан и его удалось создать."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(LINE_FORMAT)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    if directory is None:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "dockbot.log"
        file_handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        logging.getLogger("dockbot").error("log directory is not available")
        return None
    return path
