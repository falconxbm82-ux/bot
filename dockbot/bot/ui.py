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

from dockbot.booking import HEAVY_CARGOS, LIGHT_CARGOS, GateChoice, SlotChoice, Trip
from dockbot.bot.keyboard import CALLBACK_BOOK, CALLBACK_LIST, NAV_MENU
from dockbot.models import BookingStatus
from dockbot.present import day_button, trip_button

DATE_PREFIX = "bk:date:"
GATE_PREFIX = "bk:gate:"
SLOT_PREFIX = "bk:slot:"
CONFIRM = "bk:ok"
RESTART = "bk:re"
ABORT = "bk:abort"
MV_OK = "mv:ok"

OPEN_PREFIX = "my:open:"
PASS_PREFIX = "my:pass:"
CANCEL_PREFIX = "cx:ask:"
CANCEL_YES_PREFIX = "cx:yes:"
CANCEL_NO_PREFIX = "cx:no:"
MOVE_PREFIX = "mv:pick:"
PARTY_PREFIX = "my:party:"
DOC_PREFIX = "my:doc:"
PAGE_PREFIX = "my:page:"
PAGE_SIZE = 8
MENU = NAV_MENU
BACK = "nav:back"
EDIT = "bk:edit"
CURRENT_SLOT = "bk:current"
CARGO_PREFIX = "bk:cargo:"
BOOK_INLINE = CALLBACK_BOOK

_PASS_STATUSES = {
    BookingStatus.booked,
    BookingStatus.confirmed,
    BookingStatus.arrived,
}
CARGO_LABELS = dict(HEAVY_CARGOS + LIGHT_CARGOS)


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
            [InlineKeyboardButton(text="Назад", callback_data=BACK)],
            [InlineKeyboardButton(text="В меню", callback_data=MENU)],
        ]
    )


def _step_row() -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text="Отмена", callback_data=ABORT),
        InlineKeyboardButton(text="В меню", callback_data=MENU),
    ]


def cancel_step_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_step_row()])


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
    rows.append(_step_row())
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
            InlineKeyboardButton(text="Назад", callback_data=BACK),
            InlineKeyboardButton(text="В меню", callback_data=MENU),
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
            label = f"{label} текущий"
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
            InlineKeyboardButton(text="Назад", callback_data=BACK),
            InlineKeyboardButton(text="В меню", callback_data=MENU),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def vehicle_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Малотоннажный", callback_data="bk:veh:light")],
            [InlineKeyboardButton(text="Обычный", callback_data="bk:veh:heavy")],
            _step_row(),
        ]
    )


def weight_markup(vehicle: str = "heavy") -> InlineKeyboardMarkup:
    tons_list = (1, 2, 3) if vehicle == "light" else (1, 5, 10, 15, 20, 30)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for tons in tons_list:
        row.append(
            InlineKeyboardButton(
                text=f"{tons} т",
                callback_data=f"bk:weight:{tons * 1000}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="Другой вес", callback_data="bk:weight:other")]
    )
    rows.append(_step_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def document_confirm_markup(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Сходится",
                    callback_data=f"my:doc-yes:{booking_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Расхождение",
                    callback_data=f"my:doc-no:{booking_id}",
                )
            ],
        ]
    )


def document_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сходится", callback_data="bk:doc:match")],
            [InlineKeyboardButton(text="Расхождение", callback_data="bk:doc:mismatch")],
            _step_row(),
        ]
    )


def cargo_markup(
    minutes: dict[str, int] | None = None, vehicle: str = "heavy"
) -> InlineKeyboardMarkup:
    rows = []
    source = LIGHT_CARGOS if vehicle == "light" else HEAVY_CARGOS
    for code, label in source:
        title = label
        if minutes and code in minutes:
            title = f"{label} {minutes[code]} мин"
        rows.append(
            [InlineKeyboardButton(text=title, callback_data=f"{CARGO_PREFIX}{code}")]
        )
    rows.append(_step_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def summary_markup(moving: bool) -> InlineKeyboardMarkup:
    if moving:
        rows = [
            [InlineKeyboardButton(text="Подтвердить перенос", callback_data=MV_OK)],
            _step_row(),
        ]
    else:
        rows = [
            [InlineKeyboardButton(text="Подтвердить запись", callback_data=CONFIRM)],
            [InlineKeyboardButton(text="Изменить данные", callback_data=EDIT)],
            _step_row(),
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
            InlineKeyboardButton(text="Ранее", callback_data=f"{PAGE_PREFIX}{prev}")
        )
    if offset + PAGE_SIZE < len(trips):
        nav.append(
            InlineKeyboardButton(
                text="Дальше",
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
    if can_change and not trip.doc_name:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Фото документа",
                    callback_data=f"{DOC_PREFIX}{trip.id}",
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
                    text="Заменить машину",
                    callback_data=f"{PARTY_PREFIX}{trip.id}",
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
            InlineKeyboardButton(text="Назад", callback_data=CALLBACK_LIST),
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
                    callback_data=f"{CANCEL_NO_PREFIX}{booking_id}",
                )
            ],
        ]
    )


def _clip(text: str) -> str:
    if len(text) <= 60:
        return text
    return text[:59] + "…"
