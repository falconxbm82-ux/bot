"""Справочники администратора: ворота, слоты, сотрудники, параметры, сводка."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from dockbot.access import hash_pin, require_role, verify_pin
from dockbot.booking import (
    SlotChoice,
    _cells,
    _closed_keys,
    _day_start,
    _is_occupied,
    _load_warehouse,
    _occupied_keys,
    load_zone,
)
from dockbot.models import (
    Booking,
    BookingStatus,
    BookingStatusEvent,
    CargoNorm,
    ClosedSlot,
    Gate,
    GateHour,
    SiteSettings,
    StaffRole,
    StaffUser,
)

ADMIN_HORIZON_DAYS = 14
WINDOW_OPEN = time(8, 0)
WINDOW_CLOSE = time(20, 0)
SLOT_MINUTES = 60
CANCEL_CHOICES = (60, 120, 180, 240)
LATE_CHOICES = (10, 15, 20, 30)
BUFFER_CHOICES = (0, 10, 15, 30)
NORM_CHOICES = (30, 60, 90, 120)


class AdminError(Exception):
    """Текст можно показать администратору."""


@dataclass(frozen=True)
class GateRow:
    id: int
    code: str
    name: str
    is_active: bool
    light_only: bool = False


@dataclass(frozen=True)
class SlotRow:
    start: datetime
    end: datetime
    state: str


@dataclass(frozen=True)
class StaffRow:
    id: int
    role: StaffRole
    display_name: str
    telegram_id: int | None
    is_active: bool


@dataclass(frozen=True)
class StandSettings:
    cancel_minutes: int
    late_minutes: int
    address: str
    latitude: float | None
    longitude: float | None
    buffer_minutes: int = 0


@dataclass(frozen=True)
class CargoAverage:
    cargo: str
    minutes: int


@dataclass(frozen=True)
class DayFacts:
    counts: list[tuple[BookingStatus, int]]
    late: int
    cancelled: int
    on_time: int
    averages: list[CargoAverage]


def list_gates(engine: Engine, telegram_id: int) -> list[GateRow]:
    require_role(engine, telegram_id, StaffRole.admin)
    with Session(engine) as session:
        rows = session.scalars(select(Gate).order_by(Gate.code, Gate.id)).all()
        return [
            GateRow(row.id, row.code, row.name, row.is_active, row.light_only) for row in rows
        ]


def toggle_gate(engine: Engine, telegram_id: int, gate_id: int) -> GateRow:
    require_role(engine, telegram_id, StaffRole.admin)
    with Session(engine) as session:
        with session.begin():
            gate = session.get(Gate, gate_id, with_for_update=True)
            if gate is None:
                raise AdminError("Ворота не найдены.")
            gate.is_active = not gate.is_active
            return GateRow(gate.id, gate.code, gate.name, gate.is_active, gate.light_only)


def add_gate(engine: Engine, telegram_id: int, code: str) -> GateRow:
    require_role(engine, telegram_id, StaffRole.admin)
    number = code.strip()
    if not number.isdigit() or int(number) <= 0:
        raise AdminError("Номер ворот — целое число, например 3.")
    with Session(engine) as session:
        with session.begin():
            existing = session.scalar(select(Gate).where(Gate.code == number))
            if existing is not None:
                raise AdminError("Такие ворота уже есть.")
            gate = Gate(code=number, name=f"Ворота {number}", is_active=True)
            session.add(gate)
            session.flush()
            return GateRow(gate.id, gate.code, gate.name, gate.is_active, gate.light_only)


def admin_dates(engine: Engine, telegram_id: int, now: datetime | None = None) -> list[date]:
    require_role(engine, telegram_id, StaffRole.admin)
    zone = load_zone(engine)
    moment = now or datetime.now(zone)
    today = moment.astimezone(zone).date()
    return [today + timedelta(days=offset) for offset in range(ADMIN_HORIZON_DAYS)]


def day_slots(
    engine: Engine, telegram_id: int, gate_id: int, day: date
) -> list[SlotRow]:
    require_role(engine, telegram_id, StaffRole.admin)
    with Session(engine) as session:
        warehouse = _load_warehouse(session)
        start = _day_start(day, warehouse.zone)
        end = _day_start(day + timedelta(days=1), warehouse.zone)
        occupied = _occupied_keys(session, start, end, None)
        closed = _closed_keys(session, start, end)
        rows: list[SlotRow] = []
        for slot in _cells(warehouse, day):
            if slot.gate_id != gate_id:
                continue
            key = (slot.gate_id, int(slot.start.timestamp()))
            if key in occupied:
                state = "busy"
            elif key in closed:
                state = "closed"
            else:
                state = "open"
            rows.append(SlotRow(slot.start, slot.end, state))
        return rows


def generate_window(
    engine: Engine, telegram_id: int, gate_id: int, day: date
) -> int:
    """Ставит окно 08:00–20:00 на этот день недели и снимает закрытия свободных слотов."""
    require_role(engine, telegram_id, StaffRole.admin)
    with Session(engine) as session:
        with session.begin():
            gate = session.get(Gate, gate_id)
            if gate is None or not gate.is_active:
                raise AdminError("Ворота выключены или не найдены.")
            weekday = day.isoweekday()
            session.execute(
                delete(GateHour).where(
                    GateHour.gate_id == gate_id,
                    GateHour.weekday == weekday,
                )
            )
            session.add(
                GateHour(
                    gate_id=gate_id,
                    weekday=weekday,
                    start_time=WINDOW_OPEN,
                    end_time=WINDOW_CLOSE,
                    slot_minutes=SLOT_MINUTES,
                )
            )
            session.flush()
            warehouse = _load_warehouse(session)
            start = _day_start(day, warehouse.zone)
            end = _day_start(day + timedelta(days=1), warehouse.zone)
            closed_rows = session.scalars(
                select(ClosedSlot).where(
                    ClosedSlot.gate_id == gate_id,
                    ClosedSlot.starts_at >= start,
                    ClosedSlot.starts_at < end,
                )
            ).all()
            removed = 0
            for row in closed_rows:
                if _is_occupied(session, gate_id, row.starts_at, None):
                    continue
                session.delete(row)
                removed += 1
            cells = [
                slot
                for slot in _cells(warehouse, day)
                if slot.gate_id == gate_id
            ]
            return len(cells)


def set_slot_closed(
    engine: Engine,
    telegram_id: int,
    gate_id: int,
    slot_start: datetime,
    *,
    closed: bool,
) -> str:
    require_role(engine, telegram_id, StaffRole.admin)
    with Session(engine) as session:
        with session.begin():
            if _is_occupied(session, gate_id, slot_start, None):
                return "busy"
            row = session.scalar(
                select(ClosedSlot).where(
                    ClosedSlot.gate_id == gate_id,
                    ClosedSlot.starts_at == slot_start,
                )
            )
            if closed:
                if row is None:
                    session.add(ClosedSlot(gate_id=gate_id, starts_at=slot_start))
                return "closed"
            if row is not None:
                session.delete(row)
            return "open"


def list_staff(engine: Engine, telegram_id: int) -> list[StaffRow]:
    require_role(engine, telegram_id, StaffRole.admin)
    with Session(engine) as session:
        rows = session.scalars(
            select(StaffUser).order_by(StaffUser.role, StaffUser.id)
        ).all()
        return [
            StaffRow(row.id, row.role, row.display_name, row.telegram_id, row.is_active)
            for row in rows
        ]


def create_staff(
    engine: Engine,
    telegram_id: int,
    role: StaffRole,
    member_telegram_id: int | None,
    display_name: str,
) -> tuple[StaffRow, str]:
    require_role(engine, telegram_id, StaffRole.admin)
    name = display_name.strip()
    if not name:
        raise AdminError("Укажите имя сотрудника.")
    pin = _fresh_pin(engine)
    with Session(engine) as session:
        with session.begin():
            bound = member_telegram_id
            if bound is not None:
                taken = session.scalar(
                    select(StaffUser.id).where(StaffUser.telegram_id == bound)
                )
                if taken is not None:
                    bound = None
            row = StaffUser(
                role=role,
                display_name=name,
                telegram_id=bound,
                pin_hash=hash_pin(pin),
                is_active=True,
            )
            session.add(row)
            session.flush()
            return (
                StaffRow(row.id, row.role, row.display_name, row.telegram_id, True),
                pin,
            )


def replace_pin(engine: Engine, telegram_id: int, staff_id: int) -> str:
    require_role(engine, telegram_id, StaffRole.admin)
    pin = _fresh_pin(engine)
    with Session(engine) as session:
        with session.begin():
            row = session.get(StaffUser, staff_id, with_for_update=True)
            if row is None:
                raise AdminError("Сотрудник не найден.")
            row.pin_hash = hash_pin(pin)
            row.is_active = True
            return pin


def revoke_staff(engine: Engine, telegram_id: int, staff_id: int) -> None:
    require_role(engine, telegram_id, StaffRole.admin)
    with Session(engine) as session:
        with session.begin():
            row = session.get(StaffUser, staff_id, with_for_update=True)
            if row is None:
                raise AdminError("Сотрудник не найден.")
            row.is_active = False
            row.telegram_id = None


def read_settings(engine: Engine, telegram_id: int) -> StandSettings:
    require_role(engine, telegram_id, StaffRole.admin)
    with Session(engine) as session:
        row = session.get(SiteSettings, 1)
        if row is None:
            raise AdminError("Склад не настроен.")
        return StandSettings(
            cancel_minutes=row.cancel_window_hours * 60,
            late_minutes=row.late_after_minutes,
            address=(row.address or "").strip(),
            latitude=None if row.latitude is None else float(row.latitude),
            longitude=None if row.longitude is None else float(row.longitude),
            buffer_minutes=row.buffer_minutes,
        )


def set_cancel_minutes(engine: Engine, telegram_id: int, minutes: int) -> None:
    require_role(engine, telegram_id, StaffRole.admin)
    if minutes not in CANCEL_CHOICES:
        raise AdminError("Окно отмены: 60, 120, 180 или 240 минут.")
    _update_settings(engine, cancel_window_hours=minutes // 60)


def set_buffer_minutes(engine: Engine, telegram_id: int, minutes: int) -> None:
    require_role(engine, telegram_id, StaffRole.admin)
    if minutes not in BUFFER_CHOICES:
        raise AdminError("Зазор: 0, 10, 15 или 30 минут.")
    _update_settings(engine, buffer_minutes=minutes)


def set_cargo_minutes(engine: Engine, telegram_id: int, code: str, minutes: int) -> None:
    require_role(engine, telegram_id, StaffRole.admin)
    if minutes not in NORM_CHOICES:
        raise AdminError("Норматив: 30, 60, 90 или 120 минут.")
    with Session(engine) as session:
        with session.begin():
            row = session.get(CargoNorm, code)
            if row is None:
                raise AdminError("Такого типа груза нет.")
            row.minutes = minutes


def toggle_light_only(engine: Engine, telegram_id: int, gate_id: int) -> GateRow:
    require_role(engine, telegram_id, StaffRole.admin)
    with Session(engine) as session:
        with session.begin():
            gate = session.get(Gate, gate_id, with_for_update=True)
            if gate is None:
                raise AdminError("Ворота не найдены.")
            gate.light_only = not gate.light_only
            return GateRow(gate.id, gate.code, gate.name, gate.is_active, gate.light_only)


def set_late_minutes(engine: Engine, telegram_id: int, minutes: int) -> None:
    require_role(engine, telegram_id, StaffRole.admin)
    if minutes not in LATE_CHOICES:
        raise AdminError("Порог опоздания: 10, 15, 20 или 30 минут.")
    _update_settings(engine, late_after_minutes=minutes)


def set_address(engine: Engine, telegram_id: int, address: str) -> None:
    require_role(engine, telegram_id, StaffRole.admin)
    text = address.strip()
    if not text:
        raise AdminError("Адрес не может быть пустым.")
    _update_settings(engine, address=text[:300])


def set_point(engine: Engine, telegram_id: int, latitude: float, longitude: float) -> None:
    require_role(engine, telegram_id, StaffRole.admin)
    _update_settings(engine, latitude=latitude, longitude=longitude)


def day_summary(
    engine: Engine, telegram_id: int, now: datetime | None = None
) -> DayFacts:
    require_role(engine, telegram_id, StaffRole.admin)
    zone = load_zone(engine)
    moment = now or datetime.now(zone)
    day = moment.astimezone(zone).date()
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = start + timedelta(days=1)
    with Session(engine) as session:
        site = session.get(SiteSettings, 1)
        late_after = timedelta(minutes=15 if site is None else site.late_after_minutes)
        rows = session.scalars(
            select(Booking).where(Booking.slot_start >= start, Booking.slot_start < end)
        ).all()
        ids = [row.id for row in rows]
        events = []
        if ids:
            events = session.scalars(
                select(BookingStatusEvent)
                .where(
                    BookingStatusEvent.booking_id.in_(ids),
                    BookingStatusEvent.note.is_(None),
                )
                .order_by(BookingStatusEvent.id)
            ).all()
    counts_map = {status: 0 for status in BookingStatus}
    for row in rows:
        counts_map[row.status] += 1
    moments: dict[tuple[int, BookingStatus], datetime] = {}
    for event in events:
        moments[(event.booking_id, event.to_status)] = event.created_at
    on_time = 0
    buckets: dict[str, list[int]] = {}
    for row in rows:
        arrived = moments.get((row.id, BookingStatus.arrived))
        if arrived is not None and arrived <= row.slot_start + late_after:
            on_time += 1
        done = moments.get((row.id, BookingStatus.completed))
        if arrived is not None and done is not None and done >= arrived:
            buckets.setdefault(row.cargo_type, []).append(
                int((done - arrived).total_seconds() // 60)
            )
    averages = [
        CargoAverage(cargo, int(sum(values) / len(values)))
        for cargo, values in sorted(buckets.items())
    ]
    return DayFacts(
        counts=[(status, counts_map[status]) for status in BookingStatus],
        late=counts_map[BookingStatus.late],
        cancelled=counts_map[BookingStatus.cancelled],
        on_time=on_time,
        averages=averages,
    )


def ensure_stand_staff(engine: Engine) -> None:
    """Стендовые PIN: 1111 охрана, 2222 склад, 3333 администратор.

    Старый шестизначный PIN той же записи заменяется. Свой четырёхзначный PIN остаётся.
    Если роли ещё нет, создаётся стендовая карточка.
    """
    replacements = (("111111", "1111"), ("222222", "2222"), ("333333", "3333"))
    defaults = (
        (StaffRole.security, "Охрана", "1111"),
        (StaffRole.warehouse, "Склад", "2222"),
        (StaffRole.admin, "Администратор", "3333"),
    )
    with Session(engine) as session:
        with session.begin():
            rows = session.scalars(select(StaffUser)).all()
            for row in rows:
                for old, new in replacements:
                    if verify_pin(old, row.pin_hash):
                        row.pin_hash = hash_pin(new)
                        row.is_active = True
                        break
            session.flush()
            for role, name, pin in defaults:
                exists = session.scalar(
                    select(StaffUser.id).where(StaffUser.role == role)
                )
                if exists is None:
                    session.add(
                        StaffUser(
                            role=role,
                            display_name=name,
                            pin_hash=hash_pin(pin),
                            is_active=True,
                        )
                    )


def _fresh_pin(engine: Engine) -> str:
    with Session(engine) as session:
        rows = session.scalars(select(StaffUser.pin_hash)).all()
    for _ in range(20):
        pin = f"{secrets.randbelow(10000):04d}"
        if all(not verify_pin(pin, stored) for stored in rows):
            return pin
    raise AdminError("Не удалось выдать PIN. Повторите.")


def _update_settings(engine: Engine, **values: object) -> None:
    with Session(engine) as session:
        with session.begin():
            row = session.get(SiteSettings, 1)
            if row is None:
                raise AdminError("Склад не настроен.")
            for key, value in values.items():
                setattr(row, key, value)


def slot_label(slot: SlotChoice | SlotRow, zone) -> str:
    start = slot.start.astimezone(zone)
    end = slot.end.astimezone(zone)
    return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
