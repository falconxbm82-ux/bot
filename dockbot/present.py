"""Тексты экранов водителя."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from dockbot.booking import DURATION_CAP_MINUTES, Trip
from dockbot.models import BookingStatus

STATUS_LABELS: dict[BookingStatus, str] = {
    BookingStatus.booked: "Забронировано",
    BookingStatus.confirmed: "Подтверждено",
    BookingStatus.arrived: "Прибыл",
    BookingStatus.unloading: "На разгрузке",
    BookingStatus.completed: "Завершено",
    BookingStatus.late: "Опоздал",
    BookingStatus.cancelled: "Отменено",
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


def cargo_detail(
    cargo: str, weight_kg: int | None, start: datetime, end: datetime
) -> str:
    minutes = max(1, int((end - start).total_seconds() // 60))
    parts = [cargo]
    if weight_kg:
        parts.append(f"{weight_kg // 1000} т")
    parts.append(f"{minutes} мин")
    return ", ".join(parts)


def unload_formula(
    base: int,
    addon: int,
    total: int,
    *,
    cut: int = 0,
    cap: int = DURATION_CAP_MINUTES,
) -> str:
    """Короткая формула окна: база типа, добавка массы, итог и потолок."""
    if cut:
        return (
            f"Разгрузка: база {base} мин, масса +{addon} мин, "
            f"малотоннажный минус {cut} мин, итого {total} мин, потолок {cap}"
        )
    return (
        f"Разгрузка: база {base} мин, масса +{addon} мин, "
        f"итого {total} мин, потолок {cap}"
    )


def _formula_line(trip: Trip) -> str | None:
    if trip.unload_base is None or trip.unload_addon is None or trip.unload_total is None:
        return None
    return unload_formula(
        trip.unload_base,
        trip.unload_addon,
        trip.unload_total,
        cut=trip.unload_cut or 0,
        cap=trip.unload_cap or DURATION_CAP_MINUTES,
    )


def _light_line(vehicle: str | None) -> str | None:
    if vehicle == "light":
        return "Транспорт: малотоннажный"
    return None


def document_line(trip: Trip) -> str:
    if trip.doc_result == "match":
        return "Документ: сходится"
    if trip.doc_result == "mismatch":
        return "Документ: расхождение"
    if trip.doc_name:
        return "Документ: фото есть, сверка не закрыта"
    return "Документ: нет фото"


def pass_caption(trip: Trip, zone: ZoneInfo) -> str:
    date_text, clock = local_parts(trip.slot_start, trip.slot_end, zone)
    lines = [
        "Пропуск",
        f"PIN: {trip.pass_pin}",
        f"Дата: {date_text}",
        f"Ворота: {gate_label(trip.gate_code)}",
        f"Время: {clock}",
        f"Груз: {cargo_detail(trip.cargo_type, trip.weight_kg, trip.slot_start, trip.slot_end)}",
    ]
    light = _light_line(trip.vehicle_kind)
    if light:
        lines.append(light)
    formula = _formula_line(trip)
    if formula:
        lines.append(formula)
    if trip.doc_result or trip.doc_name:
        lines.append(document_line(trip))
    return "\n".join(lines)


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
    weight_kg: int | None = None,
    document: str | None = None,
    formula: str | None = None,
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
        f"Груз: {cargo_detail(cargo_type, weight_kg, start, end)}",
    ]
    if formula:
        lines.append(formula)
    if document:
        lines.append(document)
    if vehicle:
        lines.append("Транспорт: малотоннажный" if vehicle == "light" else "Транспорт: обычный")
    if rules:
        lines.append(rules)
    return "\n".join(lines)


def card_text(
    trip: Trip,
    zone: ZoneInfo,
    block: str | None,
    documents: str = "if_set",
) -> str:
    date_text, clock = local_parts(trip.slot_start, trip.slot_end, zone)
    lines = [
        f"Дата: {date_text}",
        f"Время: {clock}",
        f"Ворота: {gate_label(trip.gate_code)}",
        f"Госномер: {trip.plate}",
        f"Марка: {trip.make}",
        f"ФИО: {trip.driver_name}",
        f"Груз: {cargo_detail(trip.cargo_type, trip.weight_kg, trip.slot_start, trip.slot_end)}",
        f"Статус: {STATUS_LABELS[trip.status]}",
    ]
    light = _light_line(trip.vehicle_kind)
    if light:
        lines.append(light)
    formula = _formula_line(trip)
    if formula:
        lines.append(formula)
    show_document = documents == "always" or (
        documents == "if_set" and (trip.doc_result or trip.doc_name)
    )
    if show_document:
        lines.append(document_line(trip))
    if block:
        lines.append(block)
    return "\n".join(lines)


def trip_button(trip: Trip, zone: ZoneInfo) -> str:
    local_start = trip.slot_start.astimezone(zone)
    label = (
        f"{local_start.strftime('%d.%m %H:%M')} В{trip.gate_code} "
        f"{trip.plate} {STATUS_LABELS[trip.status]}"
    )
    if len(label) <= 60:
        return label
    return label[:59] + "…"
