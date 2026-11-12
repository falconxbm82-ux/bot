"""Клавиатуры водителя и ответ в чат."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)

from dockbot.booking import GateChoice, SlotChoice, Trip
from dockbot.present import gate_label, trip_button

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
PAGE_SIZE = 30


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


def dates_markup(dates: list[date]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for day in dates:
        row.append(
            InlineKeyboardButton(
                text=day.strftime("%d.%m"),
                callback_data=f"{DATE_PREFIX}{day.isoformat()}",
            )
        )
        if len(row) == 4:
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
                text=_clip(f"{gate_label(gate.code)} {gate.name}"),
                callback_data=f"{GATE_PREFIX}{gate.id}",
            )
        ]
        for gate in gates
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=ABORT)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slots_markup(slots: list[SlotChoice], zone: ZoneInfo) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for slot in slots:
        start = slot.start.astimezone(zone)
        end = slot.end.astimezone(zone)
        row.append(
            InlineKeyboardButton(
                text=f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}",
                callback_data=f"{SLOT_PREFIX}{int(slot.start.timestamp())}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=ABORT)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def summary_markup(moving: bool) -> InlineKeyboardMarkup:
    action = "Перенести" if moving else "Записать"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=action, callback_data=CONFIRM)],
            [InlineKeyboardButton(text="Начать заново", callback_data=RESTART)],
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
    if offset + PAGE_SIZE < len(trips):
        rows.append(
            [
                InlineKeyboardButton(
                    text="Дальше",
                    callback_data=f"{PAGE_PREFIX}{offset + PAGE_SIZE}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_markup(booking_id: int, can_change: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="Показать пропуск",
                callback_data=f"{PASS_PREFIX}{booking_id}",
            )
        ]
    ]
    if can_change:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Перенести",
                    callback_data=f"{MOVE_PREFIX}{booking_id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Отменить",
                    callback_data=f"{CANCEL_PREFIX}{booking_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_markup(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отменить",
                    callback_data=f"{CANCEL_YES_PREFIX}{booking_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Нет",
                    callback_data=f"{OPEN_PREFIX}{booking_id}",
                )
            ],
        ]
    )


def _clip(text: str) -> str:
    if len(text) <= 60:
        return text
    return text[:59] + "…"
