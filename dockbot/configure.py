"""Перенос сетки склада из окружения в строки базы.

Пустое окружение базу не затирает. Координаты можно не задавать:
напоминания их всё равно требуют отдельно.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dockbot.access import hash_pin, unusable_pin_hash
from dockbot.config import ConfigError, Settings, StaffSpec
from dockbot.models import Gate, GateHour, SiteSettings, StaffRole, StaffUser

log = logging.getLogger(__name__)


def apply_configuration(engine: Engine, settings: Settings) -> None:
    if settings.staff:
        apply_staff(engine, settings.staff)
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


_ROLES = {
    "security": StaffRole.security,
    "warehouse": StaffRole.warehouse,
    "admin": StaffRole.admin,
}


def apply_staff(engine: Engine, specs: tuple[StaffSpec, ...]) -> None:
    """Записывает персонал. Пустой список базу не трогает.

    Уже записанный Telegram ID другой записью не перезаписывается.
    PIN в журнал не попадает.
    """
    if not specs:
        return
    try:
        with Session(engine) as session:
            with session.begin():
                for spec in specs:
                    role = _ROLES[spec.role]
                    row = session.scalar(
                        select(StaffUser).where(
                            StaffUser.role == role,
                            StaffUser.display_name == spec.display_name,
                        )
                    )
                    pin_hash = hash_pin(spec.pin) if spec.pin else None
                    if row is None:
                        session.add(
                            StaffUser(
                                telegram_id=_free_telegram_id(
                                    session, spec.telegram_id, None
                                ),
                                role=role,
                                display_name=spec.display_name,
                                pin_hash=pin_hash or unusable_pin_hash(),
                                is_active=True,
                            )
                        )
                        session.flush()
                        continue
                    row.is_active = True
                    if pin_hash is not None:
                        row.pin_hash = pin_hash
                    if spec.telegram_id is not None and row.telegram_id is None:
                        row.telegram_id = _free_telegram_id(
                            session, spec.telegram_id, row.id
                        )
    except IntegrityError:
        raise ConfigError(
            "Telegram ID персонала уже привязан к другой записи."
        ) from None
    log.info("staff configuration applied count=%s", len(specs))


def _free_telegram_id(
    session: Session, telegram_id: int | None, own_id: int | None
) -> int | None:
    if telegram_id is None:
        return None
    taken = session.scalar(
        select(StaffUser.id).where(StaffUser.telegram_id == telegram_id)
    )
    if taken is not None and taken != own_id:
        log.info("staff telegram id already belongs to another record")
        return None
    return telegram_id


def _decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))
