"""Напоминания T−24 ч, T−2 ч, T−30 мин и опоздание T+15 мин.

Числа заданы ТЗ. Точка, которая уже прошла к моменту создания или переноса,
не отправляется. Отметка «отправлено» пишется только после успеха доставки.
Ошибка одного рейса не останавливает остальные.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from dockbot.models import (
    Booking,
    BookingStatus,
    BookingStatusEvent,
    Gate,
    NotificationKind,
    NotificationLog,
    StatusSource,
)
from dockbot.present import gate_label, local_parts

log = logging.getLogger(__name__)

REMIND_BEFORE = {
    NotificationKind.h24: timedelta(hours=24),
    NotificationKind.h2: timedelta(hours=2),
    NotificationKind.m30: timedelta(minutes=30),
}
LATE_AFTER = timedelta(minutes=15)
KINDS = (
    NotificationKind.h24,
    NotificationKind.h2,
    NotificationKind.m30,
    NotificationKind.late,
)
_STAMPS = {
    NotificationKind.h24: "reminded_24_at",
    NotificationKind.h2: "reminded_2_at",
    NotificationKind.m30: "reminded_30_at",
}
_OPEN = (BookingStatus.booked, BookingStatus.confirmed)
_LOCK_CODE = {
    NotificationKind.h24: 1,
    NotificationKind.h2: 2,
    NotificationKind.m30: 3,
    NotificationKind.late: 4,
}


class DeliveryBlocked(Exception):
    """Водитель заблокировал бота. Повтор этой точки не нужен."""


class DeliveryFailed(Exception):
    """Telegram не принял сообщение. В тексте только имя ошибки."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class Outbound:
    def send_reminder(
        self, chat_id: int, latitude: float, longitude: float, text: str
    ) -> None:
        raise NotImplementedError

    def send_late(self, chat_id: int, text: str) -> None:
        raise NotImplementedError


def due_time(slot_start: datetime, kind: NotificationKind) -> datetime:
    if kind is NotificationKind.late:
        return slot_start + LATE_AFTER
    return slot_start - REMIND_BEFORE[kind]


def reminder_text(
    slot_start: datetime,
    slot_end: datetime,
    gate_code: str,
    plate: str,
    zone: ZoneInfo,
    kind: NotificationKind,
) -> str:
    date_text, clock = local_parts(slot_start, slot_end, zone)
    titles = {
        NotificationKind.h24: "Напоминание: до разгрузки 24 часа",
        NotificationKind.h2: "Напоминание: до разгрузки 2 часа",
        NotificationKind.m30: "Напоминание: до разгрузки 30 минут",
    }
    return (
        f"{titles[kind]}\n"
        f"Дата: {date_text}\n"
        f"Время: {clock}\n"
        f"Ворота: {gate_label(gate_code)}\n"
        f"Госномер: {plate}"
    )


def late_text(
    slot_start: datetime,
    slot_end: datetime,
    gate_code: str,
    plate: str,
    zone: ZoneInfo,
) -> str:
    date_text, clock = local_parts(slot_start, slot_end, zone)
    return (
        "Окно снято: нет отметки прибытия на КПП.\n"
        f"Дата: {date_text}\n"
        f"Время: {clock}\n"
        f"Ворота: {gate_label(gate_code)}\n"
        f"Госномер: {plate}"
    )


def catch_up(
    engine: Engine,
    *,
    sender: Outbound,
    now: datetime,
    latitude: float,
    longitude: float,
    zone: ZoneInfo,
) -> None:
    """Досылает точки, которые наступили, пока процесс не работал."""
    horizon = now + REMIND_BEFORE[NotificationKind.h24]
    with Session(engine) as session:
        ids = session.scalars(
            select(Booking.id)
            .where(
                Booking.status.in_(
                    (BookingStatus.booked, BookingStatus.confirmed, BookingStatus.late)
                ),
                Booking.slot_start <= horizon,
            )
            .order_by(Booking.slot_start, Booking.id)
        ).all()
    for booking_id in ids:
        for kind in KINDS:
            try:
                deliver(
                    engine,
                    booking_id,
                    kind,
                    sender=sender,
                    now=now,
                    latitude=latitude,
                    longitude=longitude,
                    zone=zone,
                )
            except Exception:
                log.info(
                    "delivery skipped booking=%s kind=%s", booking_id, kind.value
                )


def deliver(
    engine: Engine,
    booking_id: int,
    kind: NotificationKind,
    *,
    sender: Outbound,
    now: datetime,
    latitude: float,
    longitude: float,
    zone: ZoneInfo,
) -> None:
    if now.tzinfo is None:
        return
    with engine.connect() as lock:
        lock.execute(
            text("SELECT pg_advisory_lock(:booking_id, :kind)"),
            {"booking_id": int(booking_id), "kind": _LOCK_CODE[kind]},
        )
        lock.commit()
        try:
            _deliver_locked(
                engine,
                booking_id,
                kind,
                sender=sender,
                now=now,
                latitude=latitude,
                longitude=longitude,
                zone=zone,
            )
        finally:
            lock.execute(
                text("SELECT pg_advisory_unlock(:booking_id, :kind)"),
                {"booking_id": int(booking_id), "kind": _LOCK_CODE[kind]},
            )
            lock.commit()


def _deliver_locked(
    engine: Engine,
    booking_id: int,
    kind: NotificationKind,
    *,
    sender: Outbound,
    now: datetime,
    latitude: float,
    longitude: float,
    zone: ZoneInfo,
) -> None:
    prepared = _prepare(engine, booking_id, kind, now, zone)
    if prepared is None:
        return
    try:
        if kind is NotificationKind.late:
            sender.send_late(prepared.chat_id, prepared.text)
        else:
            sender.send_reminder(
                prepared.chat_id, latitude, longitude, prepared.text
            )
    except DeliveryBlocked:
        _record(engine, booking_id, kind, success=False, reason="DeliveryBlocked", at=now)
        log.info("delivery blocked booking=%s kind=%s", booking_id, kind.value)
        return
    except DeliveryFailed as exc:
        reason = (exc.reason or "DeliveryFailed")[:200]
        _record(engine, booking_id, kind, success=False, reason=reason, at=now)
        log.info(
            "delivery failed booking=%s kind=%s error=%s",
            booking_id,
            kind.value,
            reason,
        )
        return
    _record(engine, booking_id, kind, success=True, reason=None, at=now)


@dataclass(frozen=True)
class _Prepared:
    chat_id: int
    text: str


def _prepare(
    engine: Engine,
    booking_id: int,
    kind: NotificationKind,
    now: datetime,
    zone: ZoneInfo,
) -> _Prepared | None:
    with Session(engine) as session:
        with session.begin():
            booking = session.get(Booking, booking_id, with_for_update=True)
            if booking is None or _attempted(session, booking, kind):
                return None
            anchor = booking.rescheduled_at or booking.created_at
            due = due_time(booking.slot_start, kind)
            if due <= anchor or due > now:
                return None
            gate = session.get(Gate, booking.gate_id)
            if gate is None:
                return None
            if kind is NotificationKind.late:
                if booking.status in _OPEN:
                    previous = booking.status
                    booking.status = BookingStatus.late
                    booking.late_at = now
                    session.add(
                        BookingStatusEvent(
                            booking_id=booking.id,
                            from_status=previous,
                            to_status=BookingStatus.late,
                            source=StatusSource.scheduler,
                            actor_telegram_id=None,
                        )
                    )
                elif booking.status != BookingStatus.late:
                    return None
                text = late_text(
                    booking.slot_start,
                    booking.slot_end,
                    gate.code,
                    booking.plate,
                    zone,
                )
            else:
                if booking.status not in _OPEN:
                    return None
                if now >= booking.slot_start + LATE_AFTER:
                    return None
                if (
                    kind == NotificationKind.h2
                    and booking.status == BookingStatus.booked
                ):
                    booking.status = BookingStatus.confirmed
                    booking.confirmed_at = now
                    session.add(
                        BookingStatusEvent(
                            booking_id=booking.id,
                            from_status=BookingStatus.booked,
                            to_status=BookingStatus.confirmed,
                            source=StatusSource.scheduler,
                            actor_telegram_id=None,
                        )
                    )
                text = reminder_text(
                    booking.slot_start,
                    booking.slot_end,
                    gate.code,
                    booking.plate,
                    zone,
                    kind,
                )
            return _Prepared(chat_id=booking.driver_chat_id, text=text)


def _attempted(session: Session, booking: Booking, kind: NotificationKind) -> bool:
    column = _STAMPS.get(kind)
    if column is not None and getattr(booking, column) is not None:
        return True
    anchor = booking.rescheduled_at or booking.created_at
    found = session.scalar(
        select(NotificationLog.id).where(
            NotificationLog.booking_id == booking.id,
            NotificationLog.kind == kind,
            NotificationLog.created_at >= anchor,
        )
    )
    return found is not None


def _record(
    engine: Engine,
    booking_id: int,
    kind: NotificationKind,
    *,
    success: bool,
    reason: str | None,
    at: datetime,
) -> None:
    with Session(engine) as session:
        with session.begin():
            if success:
                booking = session.get(Booking, booking_id)
                column = _STAMPS.get(kind)
                if booking is not None and column is not None:
                    setattr(booking, column, at)
            session.add(
                NotificationLog(
                    booking_id=booking_id,
                    kind=kind,
                    success=success,
                    error_text=None if success else reason,
                    created_at=at,
                )
            )
