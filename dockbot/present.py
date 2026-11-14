"""Тексты экранов водителя."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from dockbot.booking import Trip
from dockbot.models import BookingStatus

STATUS_LABELS: dict[BookingStatus, str] = {
    BookingStatus.booked: "🟡 Забронировано",
    BookingStatus.confirmed: "🟢 Подтверждено",
    BookingStatus.arrived: "🔵 Прибыл",
    BookingStatus.unloading: "🟠 На разгрузке",
    BookingStatus.completed: "✅ Завершено",
    BookingStatus.late: "🔴 Опоздал",
    BookingStatus.cancelled: "⚫ Отменено",
}

_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def local_parts(start: datetime, end: datetime, zone: ZoneInfo) -> tuple[str, str]:
    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)
    date_text = local_start.strftime("%d.%m.%Y")
    clock = f"{local_start.strftime('%H:%M')}–{local_end.strftime('%H:%M')}"
    return date_text, clock


def gate_label(code: str) -> str:
    return f"Ворота {code}"


def day_button(day) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.strftime('%d.%m')}"


def pass_caption(trip: Trip, zone: ZoneInfo) -> str:
    date_text, clock = local_parts(trip.slot_start, trip.slot_end, zone)
    return (
        "Пропуск\n"
        f"PIN: {trip.pass_pin}\n"
        f"Дата: {date_text}\n"
        f"Ворота: {gate_label(trip.gate_code)}\n"
        f"Время: {clock}"
    )


def summary_text(
    *,
    start: datetime,
    end: datetime,
    gate_code: str,
    plate: str,
    make: str,
    driver_name: str,
    cargo_type: str,
    zone: ZoneInfo,
    moving: bool,
    rules: str | None = None,
    vehicle: str | None = None,
    urgent: bool = False,
) -> str:
    date_text, clock = local_parts(start, end, zone)
    title = "Перенос брони." if moving else "Проверьте запись."
    lines = [
        title,
        f"Дата: {date_text}",
        f"Время: {clock}",
        f"Ворота: {gate_label(gate_code)}",
        f"Госномер: {plate}",
        f"Марка: {make}",
        f"ФИО: {driver_name}",
        f"Груз: {cargo_type}",
    ]
    if vehicle:
        lines.append("Транспорт: малотоннажный" if vehicle == "light" else "Транспорт: обычный")
    if urgent:
        lines.append("Рейс: срочный")
    if rules:
        lines.append(rules)
    return "\n".join(lines)


def card_text(trip: Trip, zone: ZoneInfo, block: str | None) -> str:
    date_text, clock = local_parts(trip.slot_start, trip.slot_end, zone)
    lines = [
        f"Дата: {date_text}",
        f"Время: {clock}",
        f"Ворота: {gate_label(trip.gate_code)}",
        f"Госномер: {trip.plate}",
        f"Марка: {trip.make}",
        f"ФИО: {trip.driver_name}",
        f"Груз: {trip.cargo_type}",
        f"Статус: {STATUS_LABELS[trip.status]}",
    ]
    if block:
        lines.append(block)
    return "\n".join(lines)


def trip_button(trip: Trip, zone: ZoneInfo) -> str:
    local_start = trip.slot_start.astimezone(zone)
    label = (
        f"{local_start.strftime('%d.%m %H:%M')} · В{trip.gate_code} · "
        f"{trip.plate} · {STATUS_LABELS[trip.status]}"
    )
    if len(label) <= 60:
        return label
    return label[:59] + "…"
