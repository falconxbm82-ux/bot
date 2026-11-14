"""Экран смены: сутки склада, группировка по часу начала слота."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from dockbot.access import require_staff
from dockbot.booking import Trip, _trip, load_zone
from dockbot.checkpoint import allowed_next
from dockbot.models import Booking, BookingStatus, BookingStatusEvent, Gate, StaffRole
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
    line = (
        f"{clock} {gate_label(trip.gate_code)} {trip.plate} "
        f"{trip.driver_name} · {trip.cargo_type} — {STATUS_LABELS[trip.status]}"
    )
    marks: list[str] = []
    if trip.urgent:
        marks.append("срочно")
    if trip.wait_minutes is not None:
        marks.append("меньше минуты" if trip.wait_minutes == 0 else f"{trip.wait_minutes} мин")
    if trip.overrun:
        marks.append("дольше окна")
    if marks:
        line = f"{line} · {' · '.join(marks)}"
    return line


def actions_for(
    engine: Engine,
    trip: Trip,
    role: StaffRole,
) -> tuple[ShiftAction, ...]:
    """Кнопки перехода: охрана — прибытие и выезд, склад — ворота и разгрузка."""
    actions: list[ShiftAction] = []
    if role in {StaffRole.security, StaffRole.admin}:
        actions.extend(checkpoint_actions(trip))
    if role in {StaffRole.warehouse, StaffRole.admin}:
        actions.extend(_gate_actions(engine, trip))
        actions.extend(row_actions(trip) if trip.status == BookingStatus.unloading else ())
    if (
        role in {StaffRole.security, StaffRole.admin}
        and trip.status == BookingStatus.completed
        and not trip.departed
    ):
        actions.append(ShiftAction(BookingStatus.completed, "Выехал", f"st:out:{trip.id}"))
    return tuple(actions)


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
        trips = [_annotate(session, _trip(session, row), moment) for row in rows]
    trips.sort(key=lambda trip: _rank(trip, moment, zone))
    return zone, local_day, trips


def _gate_actions(engine: Engine, trip: Trip) -> tuple[ShiftAction, ...]:
    if allowed_next(trip.status) != BookingStatus.unloading:
        return ()
    with Session(engine) as session:
        gates = session.scalars(
            select(Gate).where(Gate.is_active.is_(True)).order_by(Gate.code, Gate.id)
        ).all()
        counts = dict(
            session.execute(
                select(Booking.gate_id, func.count())
                .where(Booking.status == BookingStatus.unloading)
                .group_by(Booking.gate_id)
            ).all()
        )
    chosen = [
        gate
        for gate in gates
        if not (gate.light_only and trip.vehicle_kind != "light")
    ]
    return tuple(
        ShiftAction(
            BookingStatus.unloading,
            f"На воротах {gate.code} · {int(counts.get(gate.id, 0))}",
            f"st:gate:{trip.id}:{gate.id}",
        )
        for gate in chosen
    )


def _annotate(session: Session, trip: Trip, now: datetime) -> Trip:
    wait = _wait_minutes(session, trip, now)
    overrun = (
        trip.status == BookingStatus.unloading and now > trip.slot_end
    )
    if wait is None and not overrun:
        return trip
    return replace(trip, wait_minutes=wait, overrun=overrun)


def _wait_minutes(session: Session, trip: Trip, now: datetime) -> int | None:
    status = None
    if trip.status == BookingStatus.arrived:
        status = BookingStatus.arrived
    elif trip.status == BookingStatus.unloading:
        status = BookingStatus.unloading
    if status is None:
        return None
    moment = session.scalar(
        select(BookingStatusEvent.created_at)
        .where(
            BookingStatusEvent.booking_id == trip.id,
            BookingStatusEvent.to_status == status,
            BookingStatusEvent.note.is_(None),
        )
        .order_by(BookingStatusEvent.id.desc())
    )
    if moment is None:
        return None
    return max(0, int((now - moment).total_seconds() // 60))


def _rank(trip: Trip, now: datetime, zone: ZoneInfo) -> tuple:
    peak = 0 if trip.slot_start.astimezone(zone).hour == now.astimezone(zone).hour else 1
    urgent = 0 if trip.urgent else 1
    reefer = 0 if trip.cargo_type == "Рефрижератор" else 1
    return (urgent, reefer, peak, trip.slot_start, trip.gate_code, trip.id)


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
