"""Перенос сетки склада из окружения в строки базы.

Пустое окружение базу не затирает. Координаты можно не задавать:
напоминания их всё равно требуют отдельно.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from dockbot.config import ConfigError, Settings
from dockbot.models import Gate, GateHour, SiteSettings

log = logging.getLogger(__name__)


def apply_configuration(engine: Engine, settings: Settings) -> None:
    if settings.time_zone is None or settings.cancel_window_hours is None:
        return
    with Session(engine) as session:
        with session.begin():
            row = session.get(SiteSettings, 1)
            latitude = _decimal(settings.latitude)
            longitude = _decimal(settings.longitude)
            if row is None:
                session.add(
                    SiteSettings(
                        id=1,
                        time_zone=settings.time_zone,
                        latitude=latitude,
                        longitude=longitude,
                        cancel_window_hours=settings.cancel_window_hours,
                    )
                )
            else:
                row.time_zone = settings.time_zone
                row.cancel_window_hours = settings.cancel_window_hours
                if latitude is not None and longitude is not None:
                    row.latitude = latitude
                    row.longitude = longitude
            if not settings.gates:
                log.info("warehouse timezone and cancel window stored")
                return
            if (
                settings.gate_open is None
                or settings.gate_close is None
                or settings.slot_minutes is None
                or not settings.weekdays
            ):
                raise ConfigError(
                    "Сетку ворот задают вместе: GATES, GATE_WEEKDAYS, "
                    "GATE_OPEN, GATE_CLOSE, SLOT_MINUTES"
                )
            for code, name in settings.gates:
                gate = session.scalar(select(Gate).where(Gate.code == code))
                if gate is None:
                    gate = Gate(code=code, name=name, is_active=True)
                    session.add(gate)
                    session.flush()
                else:
                    gate.name = name
                    gate.is_active = True
                session.execute(delete(GateHour).where(GateHour.gate_id == gate.id))
                for weekday in settings.weekdays:
                    session.add(
                        GateHour(
                            gate_id=gate.id,
                            weekday=weekday,
                            start_time=settings.gate_open,
                            end_time=settings.gate_close,
                            slot_minutes=settings.slot_minutes,
                        )
                    )
    log.info("warehouse configuration applied")


def _decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))
