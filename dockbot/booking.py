"""Бронь, сетка слотов, отмена и перенос.

Черновик диалога сюда не пишется: строка появляется только в create_booking.
Запрет двух машин на одни ворота и время держит частичный уникальный индекс.
Горизонт списка дат в ТЗ не задан; стенд показывает 14 дней.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dockbot.models import (
    CARGO_MAX,
    DRIVER_NAME_MAX,
    MAKE_MAX,
    PLATE_MAX,
    OCCUPYING_STATUSES,
    Booking,
    BookingStatus,
    BookingStatusEvent,
    CargoNorm,
    ClosedSlot,
    Gate,
    GateHour,
    SiteSettings,
    StatusSource,
)
from dockbot.passes import new_pin, new_token

log = logging.getLogger(__name__)

CONFIRM_LEAD = timedelta(hours=2)
_change_listeners: list[Callable[[int], None]] = []
HORIZON_DAYS = 14
PIN_ATTEMPTS = 8
EDITABLE = frozenset({BookingStatus.booked, BookingStatus.confirmed})
SLOT_CONSTRAINT = "uq_bookings_gate_slot_occupying"
PIN_CONSTRAINT = "uq_bookings_active_pass_pin"
TOKEN_CONSTRAINT = "bookings_pass_token_key"


class BookingError(Exception):
    """Сообщение можно показать водителю."""


class FieldError(BookingError):
    pass


class SlotTaken(BookingError):
    pass


class WindowClosed(BookingError):
    pass


class NotEditable(BookingError):
    pass


class BookingNotFound(BookingError):
    pass


class SiteNotConfigured(BookingError):
    pass


@dataclass(frozen=True)
class FieldValue:
    value: str
    notice: str | None = None


@dataclass(frozen=True)
class GateChoice:
    id: int
    code: str
    name: str


@dataclass(frozen=True)
class SlotChoice:
    gate_id: int
    gate_code: str
    gate_name: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Trip:
    id: int
    gate_id: int
    gate_code: str
    gate_name: str
    slot_start: datetime
    slot_end: datetime
    plate: str
    make: str
    driver_name: str
    cargo_type: str
    status: BookingStatus
    pass_token: str
    pass_pin: str
    driver_telegram_id: int
    driver_chat_id: int
    vehicle_kind: str = "heavy"
    urgent: bool = False
    wait_minutes: int | None = None
    overrun: bool = False
    no_driver_chat: bool = False
    departed: bool = False


@dataclass(frozen=True)
class _Warehouse:
    zone: ZoneInfo
    cancel_window: timedelta
    buffer: timedelta
    late_after: timedelta
    gates: tuple[Gate, ...]
    hours: tuple[GateHour, ...]


_PLATE_LATIN = str.maketrans("ABEKMHOPCTYX", "АВЕКМНОРСТУХ")
_PLATE_RE = re.compile(r"^[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}$")


def normalize_plate(raw: str) -> str:
    """Госномер для мастера: кириллица и пример А123ВС178."""
    text = "".join(raw.upper().split()).translate(_PLATE_LATIN)
    if _PLATE_RE.fullmatch(text) is None:
        raise FieldError("Не похоже на госномер, пример А123ВС178")
    return text


def clean_plate(raw: str) -> FieldValue:
    return _clean(
        raw.strip().upper(),
        PLATE_MAX,
        "Госномер не может быть пустым.",
        "Госномер обрезан до",
    )


def clean_make(raw: str) -> FieldValue:
    return _clean(
        raw.strip(),
        MAKE_MAX,
        "Марка не может быть пустой.",
        "Марка обрезана до",
    )


def clean_driver_name(raw: str) -> FieldValue:
    return _clean(
        raw.strip(),
        DRIVER_NAME_MAX,
        "ФИО не может быть пустым.",
        "ФИО обрезано до",
    )


def clean_cargo(raw: str) -> FieldValue:
    return _clean(
        raw.strip(),
        CARGO_MAX,
        "Тип груза не может быть пустым.",
        "Тип груза обрезан до",
    )


def load_location(engine: Engine) -> tuple[float, float] | None:
    with Session(engine) as session:
        row = session.get(SiteSettings, 1)
        if row is None or row.latitude is None or row.longitude is None:
            return None
        return float(row.latitude), float(row.longitude)


def list_live(
    engine: Engine, telegram_id: int, chat_id: int
) -> list[Trip]:
    """Живые брони этого чата, включая уже начавшийся слот."""
    with Session(engine) as session:
        rows = session.scalars(
            select(Booking)
            .where(
                Booking.driver_telegram_id == telegram_id,
                Booking.driver_chat_id == chat_id,
                Booking.status.in_(OCCUPYING_STATUSES),
            )
            .order_by(Booking.slot_start, Booking.id)
        ).all()
        return [_trip(session, row) for row in rows]


def load_zone(engine: Engine) -> ZoneInfo:
    with Session(engine) as session:
        return _load_warehouse(session).zone


def available_dates(
    engine: Engine,
    *,
    now: datetime | None = None,
    ignore_booking_id: int | None = None,
    cargo: str | None = None,
    vehicle: str | None = None,
) -> list[date]:
    moment = _aware(now)
    with Session(engine) as session:
        warehouse = _load_warehouse(session)
        duration = _cargo_duration(session, cargo)
        today = moment.astimezone(warehouse.zone).date()
        span_start = _day_start(today, warehouse.zone)
        span_end = _day_start(today + timedelta(days=HORIZON_DAYS), warehouse.zone)
        ranges = _occupied_ranges(session, span_start, span_end, ignore_booking_id)
        closed = _closed_keys(session, span_start, span_end)
        found: list[date] = []
        for offset in range(HORIZON_DAYS):
            day = today + timedelta(days=offset)
            if _open_slots(
                warehouse, day, moment, ranges, closed, duration=duration, vehicle=vehicle
            ):
                found.append(day)
        return found


def gates_with_slots(
    engine: Engine,
    day: date,
    *,
    now: datetime | None = None,
    ignore_booking_id: int | None = None,
    cargo: str | None = None,
    vehicle: str | None = None,
) -> list[GateChoice]:
    moment = _aware(now)
    with Session(engine) as session:
        warehouse = _load_warehouse(session)
        duration = _cargo_duration(session, cargo)
        span_start = _day_start(day, warehouse.zone)
        span_end = _day_start(day + timedelta(days=1), warehouse.zone)
        ranges = _occupied_ranges(session, span_start, span_end, ignore_booking_id)
        closed = _closed_keys(session, span_start, span_end)
        seen: list[GateChoice] = []
        known: set[int] = set()
        for slot in _open_slots(
            warehouse, day, moment, ranges, closed, duration=duration, vehicle=vehicle
        ):
            if slot.gate_id in known:
                continue
            known.add(slot.gate_id)
            seen.append(GateChoice(slot.gate_id, slot.gate_code, slot.gate_name))
        return seen


def slots_for_gate(
    engine: Engine,
    day: date,
    gate_id: int,
    *,
    now: datetime | None = None,
    ignore_booking_id: int | None = None,
    cargo: str | None = None,
    vehicle: str | None = None,
) -> list[SlotChoice]:
    moment = _aware(now)
    with Session(engine) as session:
        warehouse = _load_warehouse(session)
        duration = _cargo_duration(session, cargo)
        span_start = _day_start(day, warehouse.zone)
        span_end = _day_start(day + timedelta(days=1), warehouse.zone)
        ranges = _occupied_ranges(session, span_start, span_end, ignore_booking_id)
        closed = _closed_keys(session, span_start, span_end)
        return [
            slot
            for slot in _open_slots(
                warehouse,
                day,
                moment,
                ranges,
                closed,
                duration=duration,
                vehicle=vehicle,
            )
            if slot.gate_id == gate_id
        ]


def create_booking(
    engine: Engine,
    *,
    gate_id: int,
    slot_start: datetime,
    plate: str,
    make: str,
    driver_name: str,
    cargo_type: str,
    telegram_id: int | None,
    chat_id: int | None,
    now: datetime | None = None,
    vehicle: str = "heavy",
    urgent: bool = False,
) -> Trip:
    moment = _aware(now)
    plate_value = clean_plate(plate).value
    make_value = clean_make(make).value
    name_value = clean_driver_name(driver_name).value
    cargo_value = clean_cargo(cargo_type).value
    kind = _vehicle(vehicle)
    for _attempt in range(PIN_ATTEMPTS):
        try:
            with Session(engine) as session:
                with session.begin():
                    trip = _insert_booking(
                        session,
                        gate_id=gate_id,
                        slot_start=slot_start,
                        plate=plate_value,
                        make=make_value,
                        driver_name=name_value,
                        cargo_type=cargo_value,
                        telegram_id=telegram_id,
                        chat_id=chat_id,
                        now=moment,
                        vehicle=kind,
                        urgent=urgent,
                    )
            _changed(trip.id)
            return trip
        except IntegrityError as exc:
            constraint = _constraint_name(exc)
            if constraint in {PIN_CONSTRAINT, TOKEN_CONSTRAINT}:
                continue
            if constraint == SLOT_CONSTRAINT:
                own = _find_own(engine, gate_id, slot_start, telegram_id, chat_id)
                if own is not None:
                    return own
                log.info("slot conflict gate=%s", gate_id)
                raise SlotTaken("Этот слот уже заняли. Выберите другой.") from None
            raise
    raise BookingError("Не удалось выдать уникальный PIN пропуска.")


def list_future(
    engine: Engine,
    telegram_id: int,
    chat_id: int,
    *,
    now: datetime | None = None,
) -> list[Trip]:
    moment = _aware(now)
    with Session(engine) as session:
        rows = session.scalars(
            select(Booking)
            .where(
                Booking.driver_telegram_id == telegram_id,
                Booking.driver_chat_id == chat_id,
                Booking.slot_start > moment,
                Booking.status.in_(tuple(EDITABLE)),
            )
            .order_by(Booking.slot_start, Booking.id)
        ).all()
        return [_trip(session, row) for row in rows]


def get_owned(engine: Engine, telegram_id: int, chat_id: int, booking_id: int) -> Trip:
    with Session(engine) as session:
        booking = _owned(session, telegram_id, chat_id, booking_id)
        return _trip(session, booking)


def change_block(engine: Engine, trip: Trip, *, now: datetime | None = None) -> str | None:
    moment = _aware(now)
    with Session(engine) as session:
        warehouse = _load_warehouse(session)
    return _block_reason(trip.status, trip.slot_start, warehouse, moment)


def cancel_booking(
    engine: Engine,
    *,
    telegram_id: int,
    chat_id: int,
    booking_id: int,
    now: datetime | None = None,
) -> None:
    moment = _aware(now)
    with Session(engine) as session:
        with session.begin():
            booking = _lock_owned(session, telegram_id, chat_id, booking_id)
            warehouse = _load_warehouse(session)
            reason = _block_reason(booking.status, booking.slot_start, warehouse, moment)
            if reason is not None and reason.startswith("Отмена и перенос уже закрыты"):
                raise WindowClosed(reason)
            if reason is not None:
                raise NotEditable(reason)
            previous = booking.status
            booking.status = BookingStatus.cancelled
            booking.cancelled_at = moment
            session.add(
                _event(booking.id, previous, BookingStatus.cancelled, telegram_id)
            )
    _changed(booking_id)


def reschedule_booking(
    engine: Engine,
    *,
    telegram_id: int,
    chat_id: int,
    booking_id: int,
    gate_id: int,
    slot_start: datetime,
    now: datetime | None = None,
) -> Trip:
    moment = _aware(now)
    try:
        with Session(engine) as session:
            with session.begin():
                booking = _lock_owned(session, telegram_id, chat_id, booking_id)
                session.get(Gate, gate_id, with_for_update=True)
                warehouse = _load_warehouse(session)
                reason = _block_reason(
                    booking.status, booking.slot_start, warehouse, moment
                )
                if reason is not None and reason.startswith("Отмена и перенос уже закрыты"):
                    raise WindowClosed(reason)
                if reason is not None:
                    raise NotEditable(reason)
                duration = _cargo_duration(session, booking.cargo_type)
                cell = _fit_cell(warehouse, gate_id, slot_start, duration)
                if cell is None or not cell.start > moment:
                    raise SlotTaken("Этот слот больше не доступен. Выберите другой.")
                _reject_vehicle(session, cell.gate_id, booking.vehicle_kind)
                if _same_slot(booking, cell):
                    trip = _trip(session, booking)
                else:
                    taken = _range_taken(
                        session,
                        cell.gate_id,
                        cell.start,
                        cell.end,
                        booking.id,
                        warehouse.buffer,
                    )
                    if taken is not None:
                        raise SlotTaken(taken)
                    previous = booking.status
                    booking.gate_id = cell.gate_id
                    booking.slot_start = cell.start
                    booking.slot_end = cell.end
                    booking.rescheduled_at = moment
                    booking.reminded_24_at = None
                    booking.reminded_2_at = None
                    booking.reminded_30_at = None
                    if (
                        previous is BookingStatus.booked
                        and cell.start - moment < CONFIRM_LEAD
                    ):
                        booking.status = BookingStatus.confirmed
                        booking.confirmed_at = moment
                        session.add(
                            _event(
                                booking.id,
                                previous,
                                BookingStatus.confirmed,
                                telegram_id,
                            )
                        )
                    session.flush()
                    trip = _trip(session, booking)
        _changed(trip.id)
        return trip
    except IntegrityError as exc:
        if _constraint_name(exc) == SLOT_CONSTRAINT:
            log.info("reschedule slot conflict booking=%s", booking_id)
            raise SlotTaken("Этот слот уже заняли. Выберите другой.") from None
        raise


def window_rules(engine: Engine) -> str:
    """Сроки отмены и снятия окна из настроек склада."""
    with Session(engine) as session:
        row = session.get(SiteSettings, 1)
        if row is None:
            return "Сроки окна откроются, когда склад будет настроен."
        cancel = int(row.cancel_window_hours) * 60
        late = int(row.late_after_minutes)
    return (
        f"Отмена и перенос — пока до слота больше {cancel} мин. "
        f"Если на КПП нет отметки через {late} мин после начала, окно снимается."
    )


def update_vehicle(
    engine: Engine,
    *,
    telegram_id: int,
    chat_id: int,
    booking_id: int,
    plate: str,
    make: str,
    driver_name: str,
    cargo_type: str,
    now: datetime | None = None,
) -> Trip:
    """Меняет машину, водителя и груз. QR и PIN пропуска остаются."""
    moment = _aware(now)
    plate_value = clean_plate(plate).value
    make_value = clean_make(make).value
    name_value = clean_driver_name(driver_name).value
    cargo_value = clean_cargo(cargo_type).value
    with Session(engine) as session:
        with session.begin():
            booking = _lock_owned(session, telegram_id, chat_id, booking_id)
            warehouse = _load_warehouse(session)
            reason = _block_reason(booking.status, booking.slot_start, warehouse, moment)
            if reason is not None and reason.startswith("Отмена и перенос уже закрыты"):
                raise WindowClosed(reason)
            if reason is not None:
                raise NotEditable(reason)
            token = booking.pass_token
            pin = booking.pass_pin
            booking.plate = plate_value
            booking.make = make_value
            booking.driver_name = name_value
            booking.cargo_type = cargo_value
            session.flush()
            trip = _trip(session, booking)
            if trip.pass_token != token or trip.pass_pin != pin:
                raise BookingError("Пропуск менять нельзя.")
    _changed(trip.id)
    return trip


def queue_note(engine: Engine, trip: Trip) -> str | None:
    """Место на воротах без чужих госномеров. Только для прибывших и разгрузки."""
    if trip.status not in {BookingStatus.arrived, BookingStatus.unloading}:
        return None
    with Session(engine) as session:
        warehouse = _load_warehouse(session)
        ahead = session.scalar(
            select(func.count()).select_from(Booking).where(
                Booking.gate_id == trip.gate_id,
                Booking.id != trip.id,
                Booking.status.in_(tuple(OCCUPYING_STATUSES)),
                Booking.slot_start <= trip.slot_start,
            )
        )
        actual = session.scalar(
            select(BookingStatusEvent.created_at)
            .where(
                BookingStatusEvent.booking_id == trip.id,
                BookingStatusEvent.to_status == BookingStatus.unloading,
                BookingStatusEvent.note.is_(None),
            )
            .order_by(BookingStatusEvent.id.desc())
        )
    planned = trip.slot_start.astimezone(warehouse.zone).strftime("%H:%M")
    lines = [
        f"План: {planned} мск",
        f"Впереди на этих воротах: {int(ahead or 0)}",
    ]
    if actual is None:
        lines.append("Разгрузка ещё не началась.")
    else:
        lines.append(f"Факт: {actual.astimezone(warehouse.zone).strftime('%H:%M')} мск")
    return "\n".join(lines)


def booking_profile(engine: Engine, booking_id: int) -> tuple[str, str] | None:
    with Session(engine) as session:
        row = session.get(Booking, booking_id)
        if row is None:
            return None
        return row.cargo_type, row.vehicle_kind


def cargo_norms(engine: Engine) -> list[tuple[str, str, int]]:
    with Session(engine) as session:
        rows = session.scalars(select(CargoNorm).order_by(CargoNorm.code)).all()
        return [(row.code, row.label, int(row.minutes)) for row in rows]


def listen_booking_changed(listener: Callable[[int], None]) -> Callable[[], None]:
    """После записи брони планировщик обновляет задания. Возврат снимает подписку."""
    _change_listeners.append(listener)

    def stop() -> None:
        try:
            _change_listeners.remove(listener)
        except ValueError:
            pass

    return stop


def _changed(booking_id: int) -> None:
    for listener in tuple(_change_listeners):
        try:
            listener(booking_id)
        except Exception:
            log.info("schedule update failed booking=%s", booking_id)


def _insert_booking(
    session: Session,
    *,
    gate_id: int,
    slot_start: datetime,
    plate: str,
    make: str,
    driver_name: str,
    cargo_type: str,
    telegram_id: int | None,
    chat_id: int | None,
    now: datetime,
    vehicle: str = "heavy",
    urgent: bool = False,
) -> Trip:
    warehouse = _load_warehouse(session)
    session.get(Gate, gate_id, with_for_update=True)
    if telegram_id is not None and chat_id is not None:
        own = _own_row(session, gate_id, slot_start, telegram_id, chat_id)
        if own is not None:
            return _trip(session, own)
    duration = _cargo_duration(session, cargo_type)
    cell = _fit_cell(warehouse, gate_id, slot_start, duration)
    if cell is None or not cell.start > now:
        raise SlotTaken("Этот слот больше не доступен. Выберите другой.")
    _reject_vehicle(session, cell.gate_id, vehicle)
    taken = _range_taken(
        session, cell.gate_id, cell.start, cell.end, None, warehouse.buffer
    )
    if taken is not None:
        raise SlotTaken(taken)
    status = (
        BookingStatus.confirmed
        if cell.start - now < CONFIRM_LEAD
        else BookingStatus.booked
    )
    booking = Booking(
        gate_id=cell.gate_id,
        slot_start=cell.start,
        slot_end=cell.end,
        plate=plate,
        make=make,
        driver_name=driver_name,
        cargo_type=cargo_type,
        vehicle_kind=vehicle,
        urgent=urgent,
        driver_telegram_id=telegram_id,
        driver_chat_id=chat_id,
        status=status,
        pass_token=new_token(),
        pass_pin=new_pin(),
        confirmed_at=now if status is BookingStatus.confirmed else None,
    )
    session.add(booking)
    session.flush()
    session.add(_event(booking.id, None, status, telegram_id))
    session.flush()
    log.info("booking %s created", booking.id)
    return _trip(session, booking)


def _clean(value: str, limit: int, empty: str, cut: str) -> FieldValue:
    if not value:
        raise FieldError(empty)
    if len(value) <= limit:
        return FieldValue(value)
    shortened = value[:limit]
    return FieldValue(shortened, f"{cut} {limit} символов.")


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise BookingError("Время без часового пояса.")
    return now


def _load_warehouse(session: Session) -> _Warehouse:
    row = session.get(SiteSettings, 1)
    if row is None or not row.time_zone or row.cancel_window_hours is None:
        raise SiteNotConfigured(
            "Склад не настроен: нужен часовой пояс, окно отмены и сетка ворот."
        )
    try:
        zone = ZoneInfo(row.time_zone)
    except Exception as exc:
        raise SiteNotConfigured("Часовой пояс склада в настройках не распознан.") from exc
    gates = tuple(
        session.scalars(
            select(Gate).where(Gate.is_active.is_(True)).order_by(Gate.code, Gate.id)
        ).all()
    )
    if not gates:
        raise SiteNotConfigured(
            "Склад не настроен: нужен часовой пояс, окно отмены и сетка ворот."
        )
    hours = tuple(
        session.scalars(
            select(GateHour).where(GateHour.gate_id.in_([gate.id for gate in gates]))
        ).all()
    )
    if not hours:
        raise SiteNotConfigured(
            "Склад не настроен: нужен часовой пояс, окно отмены и сетка ворот."
        )
    return _Warehouse(
        zone=zone,
        cancel_window=timedelta(hours=row.cancel_window_hours),
        buffer=timedelta(minutes=row.buffer_minutes),
        late_after=timedelta(minutes=row.late_after_minutes),
        gates=gates,
        hours=hours,
    )


def _cells(warehouse: _Warehouse, day: date) -> list[SlotChoice]:
    weekday = day.isoweekday()
    by_gate = {gate.id: gate for gate in warehouse.gates}
    slots: list[SlotChoice] = []
    for hour in warehouse.hours:
        if hour.weekday != weekday or hour.gate_id not in by_gate:
            continue
        gate = by_gate[hour.gate_id]
        cursor = datetime.combine(day, hour.start_time, tzinfo=warehouse.zone)
        closing = datetime.combine(day, hour.end_time, tzinfo=warehouse.zone)
        step = timedelta(minutes=hour.slot_minutes)
        while cursor + step <= closing:
            slots.append(
                SlotChoice(
                    gate_id=gate.id,
                    gate_code=gate.code,
                    gate_name=gate.name,
                    start=cursor,
                    end=cursor + step,
                )
            )
            cursor += step
    slots.sort(key=lambda slot: (slot.start, slot.gate_code))
    return slots


def _open_slots(
    warehouse: _Warehouse,
    day: date,
    now: datetime,
    ranges: list[tuple[int, datetime, datetime]],
    closed: set[tuple[int, int]] | None = None,
    *,
    duration: timedelta | None = None,
    vehicle: str | None = None,
) -> list[SlotChoice]:
    hidden = closed or set()
    opened: list[SlotChoice] = []
    for slot in _cells(warehouse, day):
        if slot.start <= now:
            continue
        if vehicle == "heavy" and _gate_light_only(warehouse, slot.gate_id):
            continue
        length = duration if duration is not None else slot.end - slot.start
        finish = slot.start + length
        window_end = _window_end(warehouse, slot.gate_id, slot.start)
        if window_end is None or finish > window_end:
            continue
        if _range_hits(slot.gate_id, slot.start, finish, ranges, warehouse.buffer, hidden):
            continue
        opened.append(
            SlotChoice(
                gate_id=slot.gate_id,
                gate_code=slot.gate_code,
                gate_name=slot.gate_name,
                start=slot.start,
                end=finish,
            )
        )
    return opened


def _occupied_keys(
    session: Session,
    start: datetime,
    end: datetime,
    ignore_booking_id: int | None,
) -> set[tuple[int, int]]:
    query = select(Booking.id, Booking.gate_id, Booking.slot_start).where(
        Booking.slot_start >= start,
        Booking.slot_start < end,
        Booking.status.in_(tuple(OCCUPYING_STATUSES)),
    )
    if ignore_booking_id is not None:
        query = query.where(Booking.id != ignore_booking_id)
    return {
        (gate_id, int(slot_start.timestamp()))
        for _booking_id, gate_id, slot_start in session.execute(query)
    }


def _occupied_ranges(
    session: Session,
    start: datetime,
    end: datetime,
    ignore_booking_id: int | None,
) -> list[tuple[int, datetime, datetime]]:
    query = select(Booking.gate_id, Booking.slot_start, Booking.slot_end).where(
        Booking.slot_start < end,
        Booking.slot_end > start,
        Booking.status.in_(tuple(OCCUPYING_STATUSES)),
    )
    if ignore_booking_id is not None:
        query = query.where(Booking.id != ignore_booking_id)
    return [
        (gate_id, slot_start, slot_end)
        for gate_id, slot_start, slot_end in session.execute(query)
    ]


def _cargo_duration(session: Session, cargo: str | None) -> timedelta | None:
    if not cargo:
        return None
    minutes = session.scalar(select(CargoNorm.minutes).where(CargoNorm.label == cargo))
    if minutes is None:
        return None
    return timedelta(minutes=int(minutes))


def _vehicle(raw: str) -> str:
    if raw == "light":
        return "light"
    return "heavy"


def _gate_light_only(warehouse: _Warehouse, gate_id: int) -> bool:
    for gate in warehouse.gates:
        if gate.id == gate_id:
            return bool(gate.light_only)
    return False


def _reject_vehicle(session: Session, gate_id: int, vehicle: str) -> None:
    gate = session.get(Gate, gate_id)
    if gate is not None and gate.light_only and vehicle != "light":
        raise SlotTaken("Эти ворота только для малотоннажных. Выберите другие.")


def _window_end(warehouse: _Warehouse, gate_id: int, start: datetime) -> datetime | None:
    local = start.astimezone(warehouse.zone)
    weekday = local.isoweekday()
    for hour in warehouse.hours:
        if hour.gate_id != gate_id or hour.weekday != weekday:
            continue
        open_at = datetime.combine(local.date(), hour.start_time, tzinfo=warehouse.zone)
        close_at = datetime.combine(local.date(), hour.end_time, tzinfo=warehouse.zone)
        if open_at <= local < close_at:
            return close_at
    return None


def _range_hits(
    gate_id: int,
    start: datetime,
    finish: datetime,
    ranges: list[tuple[int, datetime, datetime]],
    buffer: timedelta,
    closed: set[tuple[int, int]],
) -> bool:
    blocked_until = finish + buffer
    for other_gate, occ_start, occ_end in ranges:
        if other_gate != gate_id:
            continue
        if start < occ_end + buffer and occ_start < blocked_until:
            return True
    for other_gate, stamp in closed:
        if other_gate == gate_id and start.timestamp() <= stamp < finish.timestamp():
            return True
    return False


def _range_taken(
    session: Session,
    gate_id: int,
    start: datetime,
    finish: datetime,
    ignore_booking_id: int | None,
    buffer: timedelta,
) -> str | None:
    ranges = _occupied_ranges(
        session, start - buffer - timedelta(hours=12), finish + buffer + timedelta(hours=12), ignore_booking_id
    )
    closed = _closed_keys(
        session, start - timedelta(days=1), finish + timedelta(days=1)
    )
    if any(
        other == gate_id and start.timestamp() <= stamp < finish.timestamp()
        for other, stamp in closed
    ):
        return "Слот закрыт. Выберите другой."
    if _range_hits(gate_id, start, finish, ranges, buffer, set()):
        return "Этот слот уже заняли. Выберите другой."
    return None


def _fit_cell(
    warehouse: _Warehouse,
    gate_id: int,
    slot_start: datetime,
    duration: timedelta | None,
) -> SlotChoice | None:
    cell = _cell(warehouse, gate_id, slot_start)
    if cell is None:
        return None
    length = duration if duration is not None else cell.end - cell.start
    finish = cell.start + length
    window_end = _window_end(warehouse, gate_id, cell.start)
    if window_end is None or finish > window_end:
        return None
    return SlotChoice(
        gate_id=cell.gate_id,
        gate_code=cell.gate_code,
        gate_name=cell.gate_name,
        start=cell.start,
        end=finish,
    )


def _day_start(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=zone)


def _cell(warehouse: _Warehouse, gate_id: int, slot_start: datetime) -> SlotChoice | None:
    target = int(slot_start.timestamp())
    day = slot_start.astimezone(warehouse.zone).date()
    for slot in _cells(warehouse, day):
        if slot.gate_id == gate_id and int(slot.start.timestamp()) == target:
            return slot
    return None


def _closed_keys(
    session: Session, start: datetime, end: datetime
) -> set[tuple[int, int]]:
    rows = session.execute(
        select(ClosedSlot.gate_id, ClosedSlot.starts_at).where(
            ClosedSlot.starts_at >= start,
            ClosedSlot.starts_at < end,
        )
    )
    return {(gate_id, int(starts_at.timestamp())) for gate_id, starts_at in rows}


def _is_closed(session: Session, gate_id: int, start: datetime) -> bool:
    return (
        session.scalar(
            select(ClosedSlot.id).where(
                ClosedSlot.gate_id == gate_id,
                ClosedSlot.starts_at == start,
            )
        )
        is not None
    )


def _is_occupied(
    session: Session, gate_id: int, start: datetime, ignore_booking_id: int | None
) -> bool:
    query = select(Booking.id).where(
        Booking.gate_id == gate_id,
        Booking.slot_start == start,
        Booking.status.in_(tuple(OCCUPYING_STATUSES)),
    )
    if ignore_booking_id is not None:
        query = query.where(Booking.id != ignore_booking_id)
    return session.scalar(query) is not None


def _own_row(
    session: Session,
    gate_id: int,
    slot_start: datetime,
    telegram_id: int,
    chat_id: int,
) -> Booking | None:
    target = int(slot_start.timestamp())
    rows = session.scalars(
        select(Booking).where(
            Booking.gate_id == gate_id,
            Booking.driver_telegram_id == telegram_id,
            Booking.driver_chat_id == chat_id,
            Booking.status.in_(tuple(OCCUPYING_STATUSES)),
        )
    ).all()
    for row in rows:
        if int(row.slot_start.timestamp()) == target:
            return row
    return None


def _find_own(
    engine: Engine,
    gate_id: int,
    slot_start: datetime,
    telegram_id: int,
    chat_id: int,
) -> Trip | None:
    with Session(engine) as session:
        row = _own_row(session, gate_id, slot_start, telegram_id, chat_id)
        if row is None:
            return None
        return _trip(session, row)


def _owned(session: Session, telegram_id: int, chat_id: int, booking_id: int) -> Booking:
    booking = session.get(Booking, booking_id)
    if (
        booking is None
        or booking.driver_telegram_id != telegram_id
        or booking.driver_chat_id != chat_id
    ):
        raise BookingNotFound("Бронь не найдена.")
    return booking


def _lock_owned(
    session: Session, telegram_id: int, chat_id: int, booking_id: int
) -> Booking:
    booking = session.get(Booking, booking_id, with_for_update=True)
    if (
        booking is None
        or booking.driver_telegram_id != telegram_id
        or booking.driver_chat_id != chat_id
    ):
        raise BookingNotFound("Бронь не найдена.")
    return booking


def _block_reason(
    status: BookingStatus,
    slot_start: datetime,
    warehouse: _Warehouse,
    now: datetime,
) -> str | None:
    if status not in EDITABLE:
        return "Эту бронь уже нельзя изменить. Откройте «Мои брони»."
    if not slot_start - now > warehouse.cancel_window:
        hours = int(warehouse.cancel_window.total_seconds() // 3600)
        return (
            f"Отмена и перенос уже закрыты: до слота меньше {hours} ч. "
            "Откройте «Мои брони»."
        )
    return None


def _same_slot(booking: Booking, cell: SlotChoice) -> bool:
    return booking.gate_id == cell.gate_id and int(booking.slot_start.timestamp()) == int(
        cell.start.timestamp()
    )


def _event(
    booking_id: int,
    previous: BookingStatus | None,
    new_status: BookingStatus,
    telegram_id: int,
) -> BookingStatusEvent:
    return BookingStatusEvent(
        booking_id=booking_id,
        from_status=previous,
        to_status=new_status,
        source=StatusSource.driver,
        actor_telegram_id=telegram_id,
    )


def _trip(session: Session, booking: Booking) -> Trip:
    gate = session.get(Gate, booking.gate_id)
    if gate is None:
        raise BookingError("Ворота брони не найдены.")
    return Trip(
        id=booking.id,
        gate_id=gate.id,
        gate_code=gate.code,
        gate_name=gate.name,
        slot_start=booking.slot_start,
        slot_end=booking.slot_end,
        plate=booking.plate,
        make=booking.make,
        driver_name=booking.driver_name,
        cargo_type=booking.cargo_type,
        status=booking.status,
        pass_token=booking.pass_token,
        pass_pin=booking.pass_pin,
        driver_telegram_id=booking.driver_telegram_id or 0,
        driver_chat_id=booking.driver_chat_id or 0,
        vehicle_kind=booking.vehicle_kind,
        urgent=booking.urgent,
        no_driver_chat=booking.driver_chat_id is None,
        departed=booking.departed_at is not None,
    )


def _constraint_name(exc: IntegrityError) -> str | None:
    orig = exc.orig
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None) if diag is not None else None
    if name:
        return name
    text = str(orig)
    for known in (SLOT_CONSTRAINT, PIN_CONSTRAINT, TOKEN_CONSTRAINT):
        if known in text:
            return known
    return None
