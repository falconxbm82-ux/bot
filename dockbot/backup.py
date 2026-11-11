"""Резервная копия PostgreSQL через pg_dump.

Пароль остаётся в окружении процесса (PGPASSWORD), не в аргументах и не в журнале.
Ежедневно хранится 14 копий. Это стендовое число, не норма ТЗ.
Каталог дампов задаётся отдельно от каталога логов.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine.url import make_url

log = logging.getLogger("dockbot.backup")

# Сколько дампов оставлять. Стенд, не норма ТЗ.
KEEP_COPIES = 14
WINDOWS_BACKUP_DIR = Path(r"C:\Apps\DockBot\backups")
_DUMP_NAME = re.compile(r"^dockbot-\d{8}-\d{6}\.dump$")

Runner = Callable[[Sequence[str], dict[str, str]], subprocess.CompletedProcess[bytes]]


def resolve_backup_directory(
    explicit: str | None = None,
    *,
    platform: str | None = None,
) -> Path | None:
    raw = os.environ.get("BACKUP_DIR", "") if explicit is None else explicit
    text = raw.strip()
    if text:
        return Path(text)
    current = sys.platform if platform is None else platform
    if current == "win32":
        return WINDOWS_BACKUP_DIR
    return None


def dump_name(moment: datetime) -> str:
    stamp = moment.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"dockbot-{stamp}.dump"


def copies_to_remove(paths: Iterable[Path], keep: int = KEEP_COPIES) -> list[Path]:
    """Самые старые дампы сверх keep. Чужие файлы каталога не трогает."""
    if keep < 1:
        raise ValueError("keep")
    dumps = sorted(
        (path for path in paths if _DUMP_NAME.match(path.name)),
        key=lambda path: path.name,
    )
    if len(dumps) <= keep:
        return []
    return dumps[: len(dumps) - keep]


def pg_dump_command(
    database_url: str,
    output: Path,
    dump_exe: Sequence[str],
) -> tuple[list[str], dict[str, str]]:
    """Аргументы pg_dump без пароля. Пароль, если он есть, только в PGPASSWORD."""
    url = make_url(database_url)
    if not url.database or not url.username:
        raise ValueError("database url needs a user and a database name")
    command = [
        *dump_exe,
        "--host",
        url.host or "127.0.0.1",
        "--port",
        str(url.port or 5432),
        "--username",
        url.username,
        "--dbname",
        url.database,
        "--format=custom",
        "--file",
        str(output),
        "--no-password",
    ]
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    return command, env


def run_backup(
    *,
    database_url: str,
    directory: Path,
    dump_exe: Sequence[str] = ("pg_dump",),
    now: datetime | None = None,
    keep: int = KEEP_COPIES,
    runner: Runner | None = None,
) -> int:
    """Пишет один дамп и удаляет копии старше лимита.

    Неуспешный pg_dump не удаляет уже лежащие дампы.
    """
    directory.mkdir(parents=True, exist_ok=True)
    moment = now or datetime.now(timezone.utc)
    final = directory / dump_name(moment)
    partial = directory / f"{final.name}.partial"
    try:
        command, env = pg_dump_command(database_url, partial, dump_exe)
    except Exception:
        # Текст исключения может содержать строку подключения. В журнал его не пишем.
        log.error("DATABASE_URL could not be read")
        return 2
    log.info("backup starting")
    invoke = runner or _run
    try:
        completed = invoke(command, env)
    except FileNotFoundError:
        partial.unlink(missing_ok=True)
        log.error("pg_dump is not installed")
        return 127
    if completed.returncode != 0:
        partial.unlink(missing_ok=True)
        log.error("pg_dump failed")
        return completed.returncode
    partial.replace(final)
    for old in copies_to_remove(list(directory.iterdir()), keep):
        old.unlink()
    log.info("backup written")
    return 0


def _run(command: Sequence[str], env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(list(command), env=env, check=False)


def main() -> int:
    from dockbot.config import load_settings
    from dockbot.logging_setup import resolve_log_directory, setup_logging

    setup_logging(resolve_log_directory())
    settings = load_settings()
    if not settings.database_url:
        log.error("DATABASE_URL is missing")
        return 2
    directory = resolve_backup_directory()
    if directory is None:
        log.error("BACKUP_DIR is missing")
        return 2
    try:
        return run_backup(database_url=settings.database_url, directory=directory)
    except ValueError:
        log.error("DATABASE_URL is missing a user or a database name")
        return 2


if __name__ == "__main__":
    sys.exit(main())
