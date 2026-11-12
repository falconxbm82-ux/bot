"""Клавиатуры водителя и ответ в чат."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)

from dockbot.booking import GateChoice, SlotChoice, Trip
from dockbot.bot.keyboard import CALLBACK_BOOK, CALLBACK_LIST, NAV_MENU
from dockbot.models import BookingStatus
from dockbot.present import day_button, trip_button

DATE_PREFIX = "wiz:d:"
GATE_PREFIX = "wiz:g:"
SLOT_PREFIX = "wiz:s:"
CONFIRM = "wiz:ok"
RESTART = "wiz:re"
ABORT = "wiz:x"

OPEN_PREFIX = "my:o:"
PASS_PREFIX = "my:p:"
CANCEL_PREFIX = "my:c:"
CANCEL_YES_PREFIX = "my:y:"
MOVE_PREFIX = "my:m:"
PAGE_PREFIX = "my:n:"
PAGE_SIZE = 8
MENU = NAV_MENU
BACK = "wiz:back"
EDIT = "wiz:edit"
CURRENT_SLOT = "wiz:now"
CARGO_PREFIX = "wiz:cargo:"
GEO = "my:geo"
BOOK_INLINE = CALLBACK_BOOK

_PASS_STATUSES = {
    BookingStatus.booked,
    BookingStatus.confirmed,
    BookingStatus.arrived,
}
_CARGOS = (
    ("pallets", "Паллеты"),
    ("bulk", "Навал"),
    ("liquid", "Жидкость"),
    ("reefer", "Рефрижератор"),
    ("other", "Другое"),
)


def actor_ids(event: Message | CallbackQuery) -> tuple[int, int] | None:
    user = event.from_user
    if user is None:
        return None
    if isinstance(event, CallbackQuery):
        if event.message is None:
            return None
        return user.id, event.message.chat.id
    return user.id, event.chat.id


async def reply(
    event: Message | CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
) -> None:
    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup)
        return
    message = event.message
    if message is not None and hasattr(message, "answer"):
        await event.answer()
        await message.answer(text, reply_markup=markup)
        return
    await event.answer(text, show_alert=True)


def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="В меню", callback_data=MENU)]]
    )


def back_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="«« Назад", callback_data=BACK)],
            [InlineKeyboardButton(text="В меню", callback_data=MENU)],
        ]
    )


def cancel_step_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=ABORT)]]
    )


def dates_markup(dates: list[date]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for day in dates:
        row.append(
            InlineKeyboardButton(
                text=day_button(day),
                callback_data=f"{DATE_PREFIX}{day.isoformat()}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=ABORT)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def gates_markup(gates: list[GateChoice]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=_clip(f"Ворота {gate.code}"),
                callback_data=f"{GATE_PREFIX}{gate.id}",
            )
        ]
        for gate in gates
    ]
    rows.append(
        [
            InlineKeyboardButton(text="«« Назад", callback_data=BACK),
            InlineKeyboardButton(text="Отмена", callback_data=ABORT),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slots_markup(
    slots: list[SlotChoice],
    zone: ZoneInfo,
    current: datetime | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    current_ts = int(current.timestamp()) if current is not None else None
    for slot in slots:
        start = slot.start.astimezone(zone)
        end = slot.end.astimezone(zone)
        label = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        token = int(slot.start.timestamp())
        if current_ts is not None and token == current_ts:
            label = f"{label} · текущий"
            callback = CURRENT_SLOT
        else:
            callback = f"{SLOT_PREFIX}{token}"
        row.append(InlineKeyboardButton(text=label, callback_data=callback))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text="«« Назад", callback_data=BACK),
            InlineKeyboardButton(text="Отмена", callback_data=ABORT),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cargo_markup() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"{CARGO_PREFIX}{code}")]
        for code, label in _CARGOS
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=ABORT)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def summary_markup(moving: bool) -> InlineKeyboardMarkup:
    if moving:
        rows = [
            [InlineKeyboardButton(text="Подтвердить перенос", callback_data=CONFIRM)],
            [InlineKeyboardButton(text="Отмена", callback_data=ABORT)],
        ]
    else:
        rows = [
            [InlineKeyboardButton(text="Подтвердить запись", callback_data=CONFIRM)],
            [InlineKeyboardButton(text="Изменить данные", callback_data=EDIT)],
            [InlineKeyboardButton(text="Отмена", callback_data=ABORT)],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pass_done_markup(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Показать пропуск",
                    callback_data=f"{PASS_PREFIX}{booking_id}",
                )
            ],
            [InlineKeyboardButton(text="Геометка склада", callback_data=GEO)],
            [InlineKeyboardButton(text="В меню", callback_data=MENU)],
        ]
    )


def trips_markup(
    trips: list[Trip], zone: ZoneInfo, offset: int
) -> InlineKeyboardMarkup:
    chunk = trips[offset : offset + PAGE_SIZE]
    rows = [
        [
            InlineKeyboardButton(
                text=trip_button(trip, zone),
                callback_data=f"{OPEN_PREFIX}{trip.id}",
            )
        ]
        for trip in chunk
    ]
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        prev = max(0, offset - PAGE_SIZE)
        nav.append(
            InlineKeyboardButton(text="◀", callback_data=f"{PAGE_PREFIX}{prev}")
        )
    if offset + PAGE_SIZE < len(trips):
        nav.append(
            InlineKeyboardButton(
                text="▶",
                callback_data=f"{PAGE_PREFIX}{offset + PAGE_SIZE}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="В меню", callback_data=MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def empty_bookings_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Записаться", callback_data=BOOK_INLINE)],
            [InlineKeyboardButton(text="В меню", callback_data=MENU)],
        ]
    )


def card_markup(trip: Trip, can_change: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if trip.status in _PASS_STATUSES:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Показать пропуск",
                    callback_data=f"{PASS_PREFIX}{trip.id}",
                )
            ]
        )
    if can_change:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Перенести",
                    callback_data=f"{MOVE_PREFIX}{trip.id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Отменить бронь",
                    callback_data=f"{CANCEL_PREFIX}{trip.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="«« Назад", callback_data=CALLBACK_LIST),
            InlineKeyboardButton(text="В меню", callback_data=MENU),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_markup(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отменить бронь",
                    callback_data=f"{CANCEL_YES_PREFIX}{booking_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Нет, оставить",
                    callback_data=f"{OPEN_PREFIX}{booking_id}",
                )
            ],
        ]
    )


def _clip(text: str) -> str:
    if len(text) <= 60:
        return text
    return text[:59] + "…"
