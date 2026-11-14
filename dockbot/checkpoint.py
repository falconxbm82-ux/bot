"""Проверка пропуска и ручные статусы персонала.

«Прибыл» — только из «Забронировано» или «Подтверждено».
«На разгрузке» — только из «Прибыл на КПП».
«Завершено» — только из «На разгрузке».
Назад и через шаг переход не идёт. Водителю об этом сообщении нет.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from dockbot.access import require_role, require_staff
from dockbot.booking import Trip, _changed, _trip
from dockbot.models import (
    TERMINAL_STATUSES,
    Booking,
    BookingStatus,
    BookingStatusEvent,
    Gate,
    StaffRole,
    StatusSource,
)

OCCUPYING = (
    BookingStatus.booked,
    BookingStatus.confirmed,
    BookingStatus.arrived,
    BookingStatus.unloading,
)
DEPARTURE_NOTE = "выехал"
from dockbot.qrscan import decode_qr

log = logging.getLogger(__name__)

NEXT: dict[BookingStatus, BookingStatus] = {
    BookingStatus.booked: BookingStatus.arrived,
    BookingStatus.confirmed: BookingStatus.arrived,
    BookingStatus.arrived: BookingStatus.unloading,
    BookingStatus.unloading: BookingStatus.completed,
}

_STAMP = {
    BookingStatus.arrived: "arrived_at",
    BookingStatus.unloading: "unloading_at",
    BookingStatus.completed: "completed_at",
}

class PassNotFound(Exception):
    """Токен или PIN пропуска не найден. Статус не менялся."""


class TransitionRejected(Exception):
    """Переход статусу не положен. Строка не менялась."""


def allowed_next(status: BookingStatus) -> BookingStatus | None:
    return NEXT.get(status)


def read_pass_image(engine: Engine, telegram_id: int, image: bytes) -> Trip | None:
    """Карточка по фото. Нечитаемый кадр возвращает None и ничего не пишет."""
    require_role(engine, telegram_id, StaffRole.security)
    token = decode_qr(image)
    if not token:
        return None
    return find_pass(engine, telegram_id, token=token)


def find_pass(
    engine: Engine,
    telegram_id: int,
    *,
    token: str | None = None,
    pin: str | None = None,
) -> Trip:
    """Активный пропуск, иначе последняя архивная запись с тем же PIN."""
    require_role(engine, telegram_id, StaffRole.security)
    with Session(engine) as session:
        booking = _lookup(session, token=token, pin=pin)
        if booking is None:
            log.info("pass not found")
            raise PassNotFound()
        return _trip(session, booking)


def load_trip(engine: Engine, telegram_id: int, booking_id: int) -> Trip | None:
    require_staff(engine, telegram_id)
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if booking is None:
            return None
        return _trip(session, booking)


def apply_mark(
    engine: Engine,
    telegram_id: int,
    booking_id: int,
    target: BookingStatus,
    now: datetime | None = None,
    gate_id: int | None = None,
) -> Trip:
    """Условное обновление: проигравшая транзакция видит уже новый статус.

    Отметку ставит охрана или кладовщик. Водитель её не ставит.
    """
    if target not in _STAMP:
        raise TransitionRejected()
    require_staff(engine, telegram_id)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise TransitionRejected()
    with Session(engine) as session:
        with session.begin():
            booking = session.get(Booking, booking_id, with_for_update=True)
            if booking is None:
                log.info("pass not found")
                raise PassNotFound()
            if NEXT.get(booking.status) != target:
                raise TransitionRejected()
            if target is BookingStatus.unloading and gate_id is not None:
                gate = session.get(Gate, gate_id)
                if gate is None or not gate.is_active:
                    raise TransitionRejected()
                if gate.light_only and booking.vehicle_kind != "light":
                    raise TransitionRejected()
                clash = session.scalar(
                    select(Booking.id).where(
                        Booking.gate_id == gate_id,
                        Booking.id != booking.id,
                        Booking.slot_start == booking.slot_start,
                        Booking.status.in_(tuple(OCCUPYING)),
                    )
                )
                if clash is not None:
                    raise TransitionRejected()
                booking.gate_id = gate_id
            previous = booking.status
            booking.status = target
            setattr(booking, _STAMP[target], moment)
            session.add(
                BookingStatusEvent(
                    booking_id=booking.id,
                    from_status=previous,
                    to_status=target,
                    source=StatusSource.staff,
                    actor_telegram_id=telegram_id,
                    created_at=moment,
                )
            )
            session.flush()
            trip = _trip(session, booking)
    _changed(booking_id)
    return trip


def mark_departed(
    engine: Engine,
    telegram_id: int,
    booking_id: int,
    now: datetime | None = None,
) -> Trip:
    """КПП фиксирует выезд. Статус брони остаётся «Завершено»."""
    require_staff(engine, telegram_id)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise TransitionRejected()
    with Session(engine) as session:
        with session.begin():
            booking = session.get(Booking, booking_id, with_for_update=True)
            if booking is None:
                raise PassNotFound()
            if booking.status != BookingStatus.completed:
                raise TransitionRejected()
            if booking.departed_at is not None:
                raise TransitionRejected()
            booking.departed_at = moment
            session.add(
                BookingStatusEvent(
                    booking_id=booking.id,
                    from_status=BookingStatus.completed,
                    to_status=BookingStatus.completed,
                    source=StatusSource.staff,
                    actor_telegram_id=telegram_id,
                    note=DEPARTURE_NOTE,
                    created_at=moment,
                )
            )
            session.flush()
            trip = _trip(session, booking)
    return trip


def _lookup(
    session: Session, *, token: str | None, pin: str | None
) -> Booking | None:
    if token:
        return session.scalar(select(Booking).where(Booking.pass_token == token.strip()))
    secret = (pin or "").strip()
    if not secret:
        return None
    active = session.scalar(
        select(Booking).where(
            Booking.pass_pin == secret,
            Booking.status.notin_(TERMINAL_STATUSES),
        )
    )
    if active is not None:
        return active
    return session.scalar(
        select(Booking)
        .where(
            Booking.pass_pin == secret,
            Booking.status.in_(TERMINAL_STATUSES),
        )
        .order_by(Booking.created_at.desc(), Booking.id.desc())
    )
