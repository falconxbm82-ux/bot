"""Экран смены: сутки склада, группировка по часу начала слота."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from dockbot.access import require_staff
from dockbot.booking import Trip, _trip, load_zone
from dockbot.checkpoint import allowed_next
from dockbot.models import Booking, BookingStatus, Gate
from dockbot.present import STATUS_LABELS, gate_label, local_parts

MESSAGE_LIMIT = 3500


@dataclass(frozen=True)
class ShiftAction:
    target: BookingStatus
    label: str
    callback: str


@dataclass(frozen=True)
class ShiftPage:
    text: str
    trips: tuple[Trip, ...]


def checkpoint_actions(trip: Trip) -> tuple[ShiftAction, ...]:
    if allowed_next(trip.status) != BookingStatus.arrived:
        return ()
    return (
        ShiftAction(BookingStatus.arrived, "Отметить прибытие", f"st:arrived:{trip.id}"),
    )


def row_actions(trip: Trip) -> tuple[ShiftAction, ...]:
    target = allowed_next(trip.status)
    if target == BookingStatus.unloading:
        label = f"На воротах {trip.gate_code}"
        return (ShiftAction(target, label, f"st:gate:{trip.id}:{trip.gate_id}"),)
    if target == BookingStatus.completed:
        return (ShiftAction(target, "Разгружен", f"st:done:{trip.id}"),)
    return ()


def trip_line(trip: Trip, zone: ZoneInfo) -> str:
    _date_text, clock = local_parts(trip.slot_start, trip.slot_end, zone)
    return (
        f"{clock} {gate_label(trip.gate_code)} {trip.plate} "
        f"{trip.driver_name} — {STATUS_LABELS[trip.status]}"
    )


def filter_shift(
    trips: list[Trip],
    zone: ZoneInfo,
    mode: str,
    moment: datetime,
) -> list[Trip]:
    """Фильтры монитора: час, ожидание, территория, завершённые."""
    if mode == "hour":
        hour = moment.astimezone(zone).hour
        return [
            trip
            for trip in trips
            if trip.slot_start.astimezone(zone).hour == hour
        ]
    statuses = {
        "wait": {BookingStatus.booked, BookingStatus.confirmed},
        "onsite": {BookingStatus.arrived, BookingStatus.unloading},
        "done": {BookingStatus.completed, BookingStatus.late},
    }.get(mode)
    if statuses is None:
        return list(trips)
    return [trip for trip in trips if trip.status in statuses]


def load_shift(
    engine: Engine, telegram_id: int, now: datetime | None = None
) -> tuple[ZoneInfo, date, list[Trip]]:
    require_staff(engine, telegram_id)
    moment = now or datetime.now(ZoneInfo("UTC"))
    zone = load_zone(engine)
    local_day = moment.astimezone(zone).date()
    start = datetime.combine(local_day, time.min, tzinfo=zone)
    end = start + timedelta(days=1)
    with Session(engine) as session:
        rows = session.scalars(
            select(Booking)
            .join(Gate, Gate.id == Booking.gate_id)
            .where(Booking.slot_start >= start, Booking.slot_start < end)
            .order_by(Booking.slot_start, Gate.code, Booking.id)
        ).all()
        trips = [_trip(session, row) for row in rows]
    return zone, local_day, trips


def shift_pages(
    trips: list[Trip], zone: ZoneInfo, day: date, limit: int = MESSAGE_LIMIT
) -> list[ShiftPage]:
    title = f"Смена {day.strftime('%d.%m.%Y')}"
    if not trips:
        return [ShiftPage(text=f"{title}\nЗаписей нет.", trips=())]
    groups: list[tuple[str, list[Trip]]] = []
    for trip in trips:
        header = f"{trip.slot_start.astimezone(zone).hour:02d}:00"
        if groups and groups[-1][0] == header:
            groups[-1][1].append(trip)
        else:
            groups.append((header, [trip]))
    pages: list[ShiftPage] = []
    lines: list[str] = [title]
    chosen: list[Trip] = []

    def flush() -> None:
        nonlocal lines, chosen
        if chosen:
            pages.append(ShiftPage("\n".join(lines), tuple(chosen)))
        lines = [title]
        chosen = []

    for header, members in groups:
        block = [header, *[trip_line(item, zone) for item in members]]
        if len("\n".join(lines + block)) <= limit:
            lines.extend(block)
            chosen.extend(members)
            continue
        if chosen:
            flush()
        if len("\n".join(lines + block)) <= limit:
            lines.extend(block)
            chosen.extend(members)
            continue
        for item in members:
            piece = [header, trip_line(item, zone)]
            if chosen and len("\n".join(lines + piece)) > limit:
                flush()
            lines.extend(piece)
            chosen.append(item)
    if chosen:
        pages.append(ShiftPage("\n".join(lines), tuple(chosen)))
    return pages
