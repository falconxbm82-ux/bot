"""Очередь, пауза, расхождение, фото разгрузки и сверка перед закрытием.

Кнопки только у кладовщика и администратора. Охрана эти действия не ставит.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from dockbot.access import AccessDenied, require_staff
from dockbot.booking import FieldError, Trip, _trip, clean_weight_tons, normalize_plate
from dockbot.checkpoint import apply_mark
from dockbot.documents import discard_document, save_document
from dockbot.models import (
    Booking,
    BookingStatus,
    BookingStatusEvent,
    StaffRole,
    StatusSource,
)

PAUSE_GATE = "gate"
PAUSE_CARGO = "cargo"
PAUSE_LABEL = {
    PAUSE_GATE: "ворота заняты",
    PAUSE_CARGO: "груз не готов",
}
FACT_WEIGHT = "weight"
FACT_CARGO = "cargo"
FACT_PLACES = "places"
FACT_LABEL = {
    FACT_WEIGHT: "другой вес",
    FACT_CARGO: "другой тип груза",
    FACT_PLACES: "нехватка мест",
}
_YARD = {StaffRole.warehouse, StaffRole.admin}
_LIVE = {BookingStatus.arrived, BookingStatus.unloading}


class YardError(Exception):
    """Текст можно показать кладовщику."""


class CloseMismatch(YardError):
    """Сверка не прошла. Статус рейса не менялся."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        self.fields = fields
        shown = ", ".join(fields)
        super().__init__(f"Рейс не закрыт. Не совпало: {shown}.")


@dataclass(frozen=True)
class QueueRow:
    plate: str
    cargo: str
    wait_minutes: int
    status: BookingStatus
    paused: bool


def queue_rows(
    engine: Engine, gate_id: int, now: datetime | None = None
) -> list[QueueRow]:
    moment = _aware(now)
    with Session(engine) as session:
        rows = session.scalars(
            select(Booking)
            .where(
                Booking.gate_id == gate_id,
                Booking.status.in_(tuple(_LIVE)),
            )
            .order_by(Booking.slot_start, Booking.id)
        ).all()
        return [
            QueueRow(
                plate=row.plate,
                cargo=row.cargo_type,
                wait_minutes=_wait(session, row, moment),
                status=row.status,
                paused=row.pause_reason is not None,
            )
            for row in rows
        ]


def queue_text(rows: list[QueueRow], *, show_plate: bool) -> str:
    """Общая очередь. Чужой госномер попадает в текст только у склада."""
    working = [
        row
        for row in rows
        if row.status is BookingStatus.unloading and not row.paused
    ]
    working_ids = {id(row) for row in working}
    waiting = [row for row in rows if id(row) not in working_ids]
    if not working and not waiting:
        return "Очередь пуста."
    lines = ["Очередь у ворот"]
    if working:
        lines.append("Сейчас: " + _who(working[0], show_plate, wait=False))
    for index, row in enumerate(waiting):
        title = "Следующий" if index == 0 else str(index + 1)
        lines.append(f"{title}: " + _who(row, show_plate, wait=True))
    return "\n".join(lines)


def queue_for_trip(
    engine: Engine, telegram_id: int, booking_id: int, now: datetime | None = None
) -> str:
    _yard(engine, telegram_id)
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if booking is None:
            raise YardError("Бронь не найдена.")
        gate_id = booking.gate_id
    return queue_text(queue_rows(engine, gate_id, now), show_plate=True)


def set_pause(
    engine: Engine,
    telegram_id: int,
    booking_id: int,
    reason: str,
    now: datetime | None = None,
) -> Trip:
    if reason not in PAUSE_LABEL:
        raise YardError("Выберите причину паузы.")
    moment = _aware(now)
    with Session(engine) as session:
        with session.begin():
            booking = _locked(session, engine, telegram_id, booking_id)
            booking.pause_reason = reason
            _stamp(session, booking, telegram_id, f"пауза: {PAUSE_LABEL[reason]}", moment)
            trip = _trip(session, booking)
    return trip


def resume_unload(
    engine: Engine,
    telegram_id: int,
    booking_id: int,
    now: datetime | None = None,
) -> Trip:
    moment = _aware(now)
    with Session(engine) as session:
        with session.begin():
            booking = _locked(session, engine, telegram_id, booking_id)
            if booking.pause_reason is None:
                raise YardError("Паузы нет.")
            booking.pause_reason = None
            _stamp(session, booking, telegram_id, "разгрузка продолжена", moment)
            trip = _trip(session, booking)
    return trip


def start_unload(
    engine: Engine,
    telegram_id: int,
    booking_id: int,
    now: datetime | None = None,
) -> Trip:
    _yard(engine, telegram_id)
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if booking is None:
            raise YardError("Бронь не найдена.")
        if booking.pause_reason:
            raise YardError("Сначала продолжите разгрузку.")
        if booking.status is not BookingStatus.arrived:
            raise YardError("Разгрузку начинают после прибытия.")
        gate_id = booking.gate_id
    return apply_mark(
        engine,
        telegram_id,
        booking_id,
        BookingStatus.unloading,
        now=now,
        gate_id=gate_id,
    )


def record_fact(
    engine: Engine,
    telegram_id: int,
    booking_id: int,
    kind: str,
    value: str,
    now: datetime | None = None,
) -> Trip:
    if kind not in FACT_LABEL:
        raise YardError("Выберите причину расхождения.")
    fact = " ".join(value.split())
    if not fact:
        raise YardError("Введите факт.")
    if len(fact) > 200:
        raise YardError("Факт длиннее 200 символов.")
    moment = _aware(now)
    with Session(engine) as session:
        with session.begin():
            booking = _locked(session, engine, telegram_id, booking_id)
            booking.fact_kind = kind
            booking.fact_value = fact
            _stamp(session, booking, telegram_id, "расхождение", moment)
            trip = _trip(session, booking)
    return trip


def save_unload_photo(
    engine: Engine, telegram_id: int, booking_id: int, image: bytes
) -> Trip:
    _yard(engine, telegram_id)
    name = save_document(image)
    previous = None
    try:
        with Session(engine) as session:
            with session.begin():
                booking = _locked(session, engine, telegram_id, booking_id)
                previous = booking.unload_photo
                booking.unload_photo = name
                trip = _trip(session, booking)
    except Exception:
        discard_document(name)
        raise
    if previous and previous != name:
        discard_document(previous)
    return trip


def mark_damage(
    engine: Engine,
    telegram_id: int,
    booking_id: int,
    now: datetime | None = None,
) -> Trip:
    moment = _aware(now)
    with Session(engine) as session:
        with session.begin():
            booking = _locked(session, engine, telegram_id, booking_id)
            if not booking.unload_photo:
                raise YardError("Сначала пришлите фото разгрузки.")
            booking.pallet_damage = True
            _stamp(session, booking, telegram_id, "паллеты повреждены", moment)
            trip = _trip(session, booking)
    return trip


def close_unload(
    engine: Engine,
    telegram_id: int,
    booking_id: int,
    *,
    plate: str,
    cargo: str,
    weight_text: str,
    places_text: str,
    now: datetime | None = None,
) -> Trip:
    """Закрывает рейс только если госномер, тип, вес и число мест совпали."""
    _yard(engine, telegram_id)
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if booking is None:
            raise YardError("Бронь не найдена.")
        if booking.pause_reason:
            raise YardError("Сначала продолжите разгрузку.")
        if booking.status is not BookingStatus.unloading:
            raise YardError("Сначала начните разгрузку.")
        vehicle = booking.vehicle_kind
        expected_plate = booking.plate
        expected_cargo = booking.cargo_type
        expected_weight = booking.weight_kg
        expected_places = booking.place_count
    missed: list[str] = []
    try:
        got_plate = normalize_plate(plate)
    except FieldError:
        got_plate = ""
    if got_plate != expected_plate:
        missed.append("госномер")
    got_cargo = " ".join(cargo.split())
    if got_cargo.casefold() != expected_cargo.casefold():
        missed.append("тип груза")
    try:
        got_weight = clean_weight_tons(weight_text, vehicle)
    except FieldError:
        got_weight = None
    if expected_weight is None or got_weight != expected_weight:
        missed.append("вес")
    places = places_text.strip()
    got_places = int(places) if places.isdigit() else None
    if expected_places is None or got_places != expected_places:
        missed.append("число мест")
    if missed:
        raise CloseMismatch(tuple(missed))
    return apply_mark(
        engine,
        telegram_id,
        booking_id,
        BookingStatus.completed,
        now=now,
    )


def _who(row: QueueRow, show_plate: bool, *, wait: bool) -> str:
    parts: list[str] = []
    if show_plate:
        parts.append(row.plate)
    parts.append(row.cargo)
    text = ", ".join(parts)
    if wait:
        if row.wait_minutes == 0:
            text += ", ждёт меньше минуты"
        else:
            text += f", ждёт {row.wait_minutes} мин"
    return text


def _wait(session: Session, booking: Booking, now: datetime) -> int:
    moment = booking.arrived_at
    if moment is None:
        moment = session.scalar(
            select(BookingStatusEvent.created_at)
            .where(
                BookingStatusEvent.booking_id == booking.id,
                BookingStatusEvent.to_status == BookingStatus.arrived,
                BookingStatusEvent.note.is_(None),
            )
            .order_by(BookingStatusEvent.id.desc())
        )
    if moment is None:
        return 0
    return max(0, int((now - moment).total_seconds() // 60))


def _yard(engine: Engine, telegram_id: int) -> None:
    person = require_staff(engine, telegram_id)
    if person.role not in _YARD:
        raise AccessDenied()


def _locked(
    session: Session, engine: Engine, telegram_id: int, booking_id: int
) -> Booking:
    _yard(engine, telegram_id)
    booking = session.get(Booking, booking_id, with_for_update=True)
    if booking is None:
        raise YardError("Бронь не найдена.")
    if booking.status not in _LIVE:
        raise YardError("Рейс уже не на территории.")
    return booking


def _stamp(
    session: Session,
    booking: Booking,
    telegram_id: int,
    note: str,
    moment: datetime,
) -> None:
    session.add(
        BookingStatusEvent(
            booking_id=booking.id,
            from_status=booking.status,
            to_status=booking.status,
            source=StatusSource.staff,
            actor_telegram_id=telegram_id,
            note=note[:40],
            created_at=moment,
        )
    )


def _aware(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise YardError("Время без часового пояса.")
    return moment
