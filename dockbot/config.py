"""Чтение окружения.

Токен бота и строка подключения читаются отсюда и нигде не печатаются.
Пустые пояс, геометка или окно отмены не мешают опросу Telegram.
Служба напоминаний без них не стартует: это проверяет require_site.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


class ConfigError(Exception):
    """Окружение не даёт запустить запрошенный режим."""


@dataclass(frozen=True, repr=False)
class Settings:
    bot_token: str
    database_url: str
    time_zone: str | None
    latitude: float | None
    longitude: float | None
    cancel_window_hours: int | None

    def __repr__(self) -> str:
        return (
            "Settings(bot_token=***, database_url=***, "
            f"time_zone={self.time_zone!r}, latitude={self.latitude!r}, "
            f"longitude={self.longitude!r}, "
            f"cancel_window_hours={self.cancel_window_hours!r})"
        )

    @property
    def site_is_complete(self) -> bool:
        return (
            self.time_zone is not None
            and self.latitude is not None
            and self.longitude is not None
            and self.cancel_window_hours is not None
        )

    def require_bot(self) -> None:
        missing: list[str] = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.database_url:
            missing.append("DATABASE_URL")
        if missing:
            raise ConfigError("Не задано: " + ", ".join(missing))
        if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ConfigError(
                "DATABASE_URL должен указывать на PostgreSQL "
                "(postgresql+psycopg://...)"
            )

    def require_site(self) -> None:
        missing: list[str] = []
        if not self.time_zone:
            missing.append("WAREHOUSE_TIMEZONE")
        if self.latitude is None or self.longitude is None:
            missing.append("WAREHOUSE_LATITUDE и WAREHOUSE_LONGITUDE")
        if self.cancel_window_hours is None:
            missing.append("CANCEL_WINDOW_HOURS")
        if missing:
            raise ConfigError(
                "Склад не настроен, служба напоминаний не запускается. Не задано: "
                + ", ".join(missing)
            )

    def secrets(self) -> list[str]:
        values = [self.bot_token, self.database_url]
        if self.database_url:
            password = urlparse(self.database_url).password
            if password:
                values.append(password)
        return [value for value in values if value]


def load_settings() -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    time_zone = _optional_str("WAREHOUSE_TIMEZONE")
    if time_zone is not None:
        try:
            ZoneInfo(time_zone)
        except Exception as exc:
            raise ConfigError(
                f"WAREHOUSE_TIMEZONE не является поясом IANA: {time_zone}"
            ) from exc
    latitude = _optional_float("WAREHOUSE_LATITUDE")
    longitude = _optional_float("WAREHOUSE_LONGITUDE")
    if latitude is not None and not -90 <= latitude <= 90:
        raise ConfigError("WAREHOUSE_LATITUDE вне диапазона -90..90")
    if longitude is not None and not -180 <= longitude <= 180:
        raise ConfigError("WAREHOUSE_LONGITUDE вне диапазона -180..180")
    return Settings(
        bot_token=os.environ.get("BOT_TOKEN", "").strip(),
        database_url=os.environ.get("DATABASE_URL", "").strip(),
        time_zone=time_zone,
        latitude=latitude,
        longitude=longitude,
        cancel_window_hours=_optional_hours("CANCEL_WINDOW_HOURS"),
    )


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
        encoded = quote(secret, safe="")
        if encoded != secret:
            text = text.replace(encoded, "***")
    return text


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _optional_str(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _optional_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть числом") from exc


def _optional_hours(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        hours = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть целым числом часов") from exc
    if hours <= 0:
        raise ConfigError(f"{name} должно быть больше нуля")
    return hours
