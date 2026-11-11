"""Журнал и аргументы резервного копирования. NSSM и Планировщик здесь не запускаются."""

from __future__ import annotations

import logging
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dockbot.backup import (
    KEEP_COPIES,
    WINDOWS_BACKUP_DIR,
    copies_to_remove,
    pg_dump_command,
    resolve_backup_directory,
    run_backup,
)
from dockbot.logging_setup import (
    WINDOWS_LOG_DIR,
    resolve_log_directory,
    setup_logging,
)


class LogDirectoryTests(unittest.TestCase):
    def test_windows_default_and_explicit_directory(self) -> None:
        self.assertEqual(
            resolve_log_directory("", platform="win32"),
            WINDOWS_LOG_DIR,
        )
        self.assertIsNone(resolve_log_directory("", platform="linux"))
        self.assertEqual(
            resolve_log_directory("/tmp/dockbot-logs", platform="linux"),
            Path("/tmp/dockbot-logs"),
        )

    def test_backup_directory_stays_outside_the_log_directory(self) -> None:
        self.assertEqual(
            resolve_backup_directory("", platform="win32"),
            WINDOWS_BACKUP_DIR,
        )
        self.assertNotEqual(WINDOWS_BACKUP_DIR, WINDOWS_LOG_DIR)
        self.assertIsNone(resolve_backup_directory("", platform="linux"))


class LogFileTests(unittest.TestCase):
    def setUp(self) -> None:
        root = logging.getLogger()
        self._handlers = root.handlers[:]
        self._level = root.level
        root.handlers.clear()
        self._directory = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in self._handlers:
            root.addHandler(handler)
        root.setLevel(self._level)

    def test_line_format_and_rotation(self) -> None:
        path = setup_logging(self._directory, max_bytes=80, backups=2)
        self.assertIsNotNone(path)
        log = logging.getLogger("dockbot")
        log.info("process starting")
        log.info("x" * 120)
        log.info("y" * 120)
        pieces = [
            path.read_text(encoding="utf-8")
            for path in sorted(self._directory.glob("dockbot.log*"))
        ]
        text = "\n".join(pieces)
        self.assertRegex(
            text,
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ INFO dockbot process starting",
        )
        self.assertTrue((self._directory / "dockbot.log.1").exists())
        self.assertNotIn("bot-token", text)


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._logger = logging.getLogger("dockbot.backup")
        self._handler = logging.NullHandler()
        self._logger.addHandler(self._handler)
        self._propagate = self._logger.propagate
        self._logger.propagate = False

    def tearDown(self) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.propagate = self._propagate

    def test_retention_keeps_fourteen_newest_dumps(self) -> None:
        directory = Path(tempfile.mkdtemp())
        names = [f"dockbot-20260101-{index:06d}.dump" for index in range(16)]
        for name in names:
            (directory / name).write_text("old", encoding="utf-8")
        (directory / "notes.txt").write_text("keep", encoding="utf-8")
        (directory / "dockbot-20260101-000001.dump.partial").write_text(
            "partial",
            encoding="utf-8",
        )
        expired = copies_to_remove(directory.iterdir(), KEEP_COPIES)
        self.assertEqual(
            [path.name for path in expired],
            names[:2],
        )
        self.assertEqual(KEEP_COPIES, 14)

    def test_password_stays_out_of_the_pg_dump_arguments(self) -> None:
        password = "stand-secret"
        command, env = pg_dump_command(
            f"postgresql+psycopg://dockbot:{password}@127.0.0.1:5432/dockbot",
            Path("C:/Apps/DockBot/backups/dockbot-20261111-020000.dump.partial"),
            ["pg_dump"],
        )
        self.assertNotIn(password, command)
        self.assertNotIn(password, " ".join(command))
        self.assertNotIn("--password", command)
        self.assertEqual(env["PGPASSWORD"], password)
        self.assertEqual(command[0], "pg_dump")
        self.assertIn("--dbname", command)
        self.assertIn("dockbot", command)
        self.assertIn("--no-password", command)

    def test_run_backup_prunes_after_a_successful_dump(self) -> None:
        directory = Path(tempfile.mkdtemp())
        for index in range(KEEP_COPIES):
            (directory / f"dockbot-20260101-{index:06d}.dump").write_text(
                "old",
                encoding="utf-8",
            )
        password = "stand-secret"
        seen: dict[str, object] = {}

        def runner(command, env):
            seen["command"] = list(command)
            seen["password"] = env.get("PGPASSWORD")
            Path(command[command.index("--file") + 1]).write_text("dump", encoding="utf-8")
            return subprocess.CompletedProcess(list(command), 0)

        code = run_backup(
            database_url=f"postgresql+psycopg://dockbot:{password}@127.0.0.1:5432/dockbot",
            directory=directory,
            now=datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc),
            runner=runner,
        )
        self.assertEqual(code, 0)
        command = seen["command"]
        assert isinstance(command, list)
        self.assertNotIn(password, command)
        self.assertEqual(seen["password"], password)
        dumps = sorted(path.name for path in directory.glob("dockbot-*.dump"))
        self.assertEqual(len(dumps), KEEP_COPIES)
        self.assertNotIn("dockbot-20260101-000000.dump", dumps)
        self.assertIn("dockbot-20261111-020000.dump", dumps)
        self.assertFalse(list(directory.glob("*.partial")))

    def test_failed_dump_keeps_existing_copies(self) -> None:
        directory = Path(tempfile.mkdtemp())
        (directory / "dockbot-20260101-000000.dump").write_text("old", encoding="utf-8")

        def runner(command, env):
            return subprocess.CompletedProcess(list(command), 1)

        code = run_backup(
            database_url="postgresql+psycopg://dockbot:stand-secret@127.0.0.1:5432/dockbot",
            directory=directory,
            now=datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc),
            runner=runner,
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            [path.name for path in directory.glob("dockbot-*.dump")],
            ["dockbot-20260101-000000.dump"],
        )
        self.assertFalse(list(directory.glob("*.partial")))

    def test_bad_database_url_is_not_echoed(self) -> None:
        code = run_backup(
            database_url="postgresql+psycopg://dockbot:stand-secret@",
            directory=Path(tempfile.mkdtemp()),
        )
        self.assertEqual(code, 2)

    def test_missing_pg_dump_does_not_delete_copies(self) -> None:
        directory = Path(tempfile.mkdtemp())
        (directory / "dockbot-20260101-000000.dump").write_text("old", encoding="utf-8")

        def runner(command, env):
            raise FileNotFoundError(command[0])

        code = run_backup(
            database_url="postgresql+psycopg://dockbot:stand-secret@127.0.0.1:5432/dockbot",
            directory=directory,
            runner=runner,
        )
        self.assertEqual(code, 127)
        self.assertTrue((directory / "dockbot-20260101-000000.dump").exists())


if __name__ == "__main__":
    unittest.main()
