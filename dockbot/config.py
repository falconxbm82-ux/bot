"""Чтение окружения.

Токен бота и строка подключения читаются отсюда и нигде не печатаются.
Пустые пояс, геометка или окно отмены не мешают опросу Telegram.
Служба напоминаний без них не стартует: это проверяет require_site.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

# Длина PIN персонала на стенде. В ТЗ не задана.
STAFF_PIN_LENGTH = 4


class ConfigError(Exception):
    """Окружение не даёт запустить запрошенный режим."""


@dataclass(frozen=True, repr=False)
class StaffSpec:
    """Сотрудник из окружения. PIN в представлении объекта не печатается."""

    role: str
    display_name: str
    pin: str | None
    telegram_id: int | None

    def __repr__(self) -> str:
        return (
            f"StaffSpec(role={self.role!r}, display_name={self.display_name!r}, "
            "pin=***, telegram_id="
            f"{self.telegram_id!r})"
        )


@dataclass(frozen=True, repr=False)
class Settings:
    bot_token: str
    database_url: str
    time_zone: str | None
    latitude: float | None
    longitude: float | None
    cancel_window_hours: int | None
    gates: tuple[tuple[str, str], ...]
    weekdays: tuple[int, ...]
    gate_open: time | None
    gate_close: time | None
    slot_minutes: int | None
    staff: tuple[StaffSpec, ...]

    def __repr__(self) -> str:
        return (
            "Settings(bot_token=***, database_url=***, "
            f"time_zone={self.time_zone!r}, latitude={self.latitude!r}, "
            f"longitude={self.longitude!r}, "
            f"cancel_window_hours={self.cancel_window_hours!r}, "
            f"gates={len(self.gates)}, slot_minutes={self.slot_minutes!r}, "
            f"staff={len(self.staff)})"
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
        for spec in self.staff:
            if spec.pin:
                values.append(spec.pin)
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
    if (latitude is None) != (longitude is None):
        raise ConfigError(
            "Геометку задают парой: WAREHOUSE_LATITUDE и WAREHOUSE_LONGITUDE"
        )
    gates = _gates()
    weekdays, gate_open, gate_close, slot_minutes = _schedule(bool(gates))
    cancel_window_hours = _optional_hours("CANCEL_WINDOW_HOURS")
    if gates and (time_zone is None or cancel_window_hours is None):
        raise ConfigError(
            "Для сетки ворот задайте WAREHOUSE_TIMEZONE и CANCEL_WINDOW_HOURS"
        )
    return Settings(
        bot_token=os.environ.get("BOT_TOKEN", "").strip(),
        database_url=os.environ.get("DATABASE_URL", "").strip(),
        time_zone=time_zone,
        latitude=latitude,
        longitude=longitude,
        cancel_window_hours=cancel_window_hours,
        gates=gates,
        weekdays=weekdays,
        gate_open=gate_open,
        gate_close=gate_close,
        slot_minutes=slot_minutes,
        staff=parse_staff_list(os.environ.get("STAFF")),
    )


def parse_staff_list(raw: str | None) -> tuple[StaffSpec, ...]:
    """Список персонала: роль:имя:PIN или роль:имя:PIN:telegram_id."""
    if raw is None or not raw.strip():
        return ()
    specs: list[StaffSpec] = []
    seen_names: set[tuple[str, str]] = set()
    seen_ids: set[int] = set()
    for chunk in raw.split(";"):
        piece = chunk.strip()
        if not piece:
            continue
        parts = piece.split(":")
        if len(parts) not in (3, 4):
            raise ConfigError(
                "STAFF задают как роль:имя:PIN или роль:имя:PIN:telegram_id"
            )
        role = parts[0].strip()
        name = parts[1].strip()
        pin = parts[2].strip()
        telegram_raw = parts[3].strip() if len(parts) == 4 else ""
        if role not in ("security", "warehouse", "admin"):
            raise ConfigError("Роль персонала: security, warehouse или admin")
        if not name or len(name) > 200:
            raise ConfigError("Укажите имя сотрудника в STAFF")
        if (role, name) in seen_names:
            raise ConfigError("В STAFF повторяется одна и та же роль и имя")
        if pin and not (pin.isdigit() and len(pin) == STAFF_PIN_LENGTH):
            raise ConfigError("PIN персонала на стенде — 4 цифры")
        telegram_id: int | None = None
        if telegram_raw:
            if not telegram_raw.isdigit() or int(telegram_raw) <= 0:
                raise ConfigError("Telegram ID персонала — положительное число")
            telegram_id = int(telegram_raw)
        if not pin and telegram_id is None:
            raise ConfigError("Для сотрудника нужен PIN или Telegram ID")
        if telegram_id is not None:
            if telegram_id in seen_ids:
                raise ConfigError("В STAFF один Telegram ID указан дважды")
            seen_ids.add(telegram_id)
        seen_names.add((role, name))
        specs.append(
            StaffSpec(
                role=role,
                display_name=name,
                pin=pin or None,
                telegram_id=telegram_id,
            )
        )
    return tuple(specs)


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


def _gates() -> tuple[tuple[str, str], ...]:
    raw = os.environ.get("GATES", "").strip()
    if not raw:
        return ()
    gates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in raw.split(";"):
        item = part.strip()
        if not item:
            continue
        if ":" not in item:
            raise ConfigError("GATES: каждая запись в виде код:название")
        code, name = item.split(":", 1)
        code = code.strip()
        name = name.strip()
        if not code or not name:
            raise ConfigError("GATES: код и название не могут быть пустыми")
        if len(code) > 32 or len(name) > 200:
            raise ConfigError("GATES: код длиннее 32 или название длиннее 200 символов")
        if code in seen:
            raise ConfigError("GATES: код ворот повторяется")
        seen.add(code)
        gates.append((code, name))
    if not gates:
        raise ConfigError("GATES не содержит ворот")
    return tuple(gates)


def _schedule(
    gates_present: bool,
) -> tuple[tuple[int, ...], time | None, time | None, int | None]:
    weekdays_raw = os.environ.get("GATE_WEEKDAYS", "").strip()
    open_raw = os.environ.get("GATE_OPEN", "").strip()
    close_raw = os.environ.get("GATE_CLOSE", "").strip()
    slot_raw = os.environ.get("SLOT_MINUTES", "").strip()
    pieces = (weekdays_raw, open_raw, close_raw, slot_raw)
    if not gates_present and not any(pieces):
        return (), None, None, None
    if not gates_present or not all(pieces):
        raise ConfigError(
            "Сетку ворот задают вместе: GATES, GATE_WEEKDAYS, "
            "GATE_OPEN, GATE_CLOSE, SLOT_MINUTES"
        )
    weekdays: list[int] = []
    for item in weekdays_raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            day = int(item)
        except ValueError as exc:
            raise ConfigError("GATE_WEEKDAYS: дни — числа 1–7") from exc
        if day < 1 or day > 7:
            raise ConfigError("GATE_WEEKDAYS: дни — числа 1–7")
        if day in weekdays:
            raise ConfigError("GATE_WEEKDAYS: день повторяется")
        weekdays.append(day)
    if not weekdays:
        raise ConfigError("GATE_WEEKDAYS не содержит дней")
    gate_open = _clock("GATE_OPEN", open_raw)
    gate_close = _clock("GATE_CLOSE", close_raw)
    if gate_close <= gate_open:
        raise ConfigError("GATE_CLOSE должно быть позже GATE_OPEN")
    try:
        slot_minutes = int(slot_raw)
    except ValueError as exc:
        raise ConfigError("SLOT_MINUTES должно быть целым числом") from exc
    if slot_minutes <= 0:
        raise ConfigError("SLOT_MINUTES должно быть больше нуля")
    return tuple(weekdays), gate_open, gate_close, slot_minutes


def _clock(name: str, raw: str) -> time:
    parts = raw.split(":")
    if len(parts) != 2:
        raise ConfigError(f"{name} записывают как ЧЧ:ММ")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        return time(hour, minute)
    except ValueError as exc:
        raise ConfigError(f"{name} записывают как ЧЧ:ММ") from exc


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
