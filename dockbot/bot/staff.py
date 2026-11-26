"""КПП и экран смены. Обычный чат этих кнопок не видит."""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.engine import Engine

from dockbot.access import (
    AccessDenied,
    active_staff,
    enter_pin,
    enter_role,
    logout_staff,
)
from dockbot.bot.driver import driver_menu
from dockbot.bot.keyboard import keyboard_for
from dockbot.bot.ui import MENU
from dockbot.booking import (
    DAY_TAKEN,
    BookingError,
    FieldError,
    SiteNotConfigured,
    Trip,
    change_block,
    clean_driver_name,
    clean_make,
    normalize_plate,
    slot_has_passed,
    staff_update_party,
)
from dockbot.checkpoint import (
    PassExpired,
    PassNotFound,
    TransitionRejected,
    UnloadPaused,
    apply_mark,
    find_pass,
    load_trip,
    mark_departed,
    read_pass_image,
    refuse_entry,
)
from dockbot.documents import document_path
from dockbot.models import BookingStatus, StaffRole
from dockbot.present import PASS_EXPIRED, card_text
from dockbot.shiftboard import (
    ShiftPage,
    actions_for,
    filter_shift,
    load_shift,
    shift_pages,
    trip_line,
)

log = logging.getLogger(__name__)

router = Router(name="staff")

CHECK = "sh:scan"
SHIFT = "sh:day"
LOGOUT = "staff:out"
ARRIVED_PREFIX = "st:arrived:"
UNLOADING_PREFIX = "st:gate:"
COMPLETED_PREFIX = "st:done:"
OPEN_PREFIX = "sh:open:"

DENIED = "Доступ не предоставлен."
PIN_BAD = "Неверный PIN."
PIN_LOCKED = "Вход сотрудника заблокирован. Отправьте /start."
RIGHTS = "Недостаточно прав."
PIN_PROMPT = "Введите PIN сотрудника: 4 цифры."
PASS_PROMPT = "Пришлите фото QR-пропуска."
PASS_PIN_PROMPT = "Введите 6 цифр PIN пропуска."
UNREADABLE = (
    "Код на фото не читается. Пришлите другое фото или введите PIN пропуска."
)
NOT_FOUND = "Пропуск не найден."
REJECTED = "Этот переход недоступен."
SIGNED_IN = "Вход выполнен."
LOGGED_OUT = "Вы вышли из роли."


class StaffFlow(StatesGroup):
    pin = State()
    checkpoint = State()
    edit_plate = State()
    edit_make = State()
    edit_name = State()


def staff_menu(role: StaffRole):
    return keyboard_for(role)


@router.message(Command("logout"))
async def logout_command(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None:
        await message.answer(DENIED)
        return
    await _leave(message, state, engine, user.id)


@router.callback_query(F.data == LOGOUT)
async def logout_button(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    user = query.from_user
    if user is None:
        await _reply(query, DENIED)
        return
    await state.clear()
    await asyncio.to_thread(logout_staff, engine, user.id)
    await _reply(query, LOGGED_OUT, driver_menu())


@router.message(Command("staff"))
async def staff_entry(message: Message, state: FSMContext, engine: Engine) -> None:
    await state.clear()
    user = message.from_user
    if user is None:
        await message.answer(DENIED)
        return
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None:
        await state.set_state(StaffFlow.pin)
        await message.answer(PIN_PROMPT)
        return
    await message.answer(SIGNED_IN, reply_markup=staff_menu(person.role))


@router.message(StaffFlow.pin, F.photo)
async def staff_pin_photo(message: Message) -> None:
    await message.answer(PIN_PROMPT)


@router.message(StaffFlow.pin, F.text)
async def staff_pin(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    text = (message.text or "").strip()
    if user is None or text.startswith("/"):
        return
    data = await state.get_data()
    wanted = data.get("wanted_role")
    try:
        if wanted in (StaffRole.security.value, StaffRole.warehouse.value):
            person = await asyncio.to_thread(
                enter_role, engine, user.id, text, StaffRole(wanted)
            )
        else:
            person = await asyncio.to_thread(enter_pin, engine, user.id, text)
    except AccessDenied:
        fails = int(data.get("pin_fails") or 0) + 1
        if fails >= 3:
            await state.clear()
            await message.answer(PIN_LOCKED, reply_markup=keyboard_for(None))
            return
        await state.update_data(pin_fails=fails)
        await message.answer(PIN_BAD)
        return
    await state.clear()
    await message.answer(SIGNED_IN, reply_markup=staff_menu(person.role))


@router.callback_query(F.data == CHECK)
async def ask_pass(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    if person.role is not StaffRole.security:
        await _reply(query, RIGHTS + " Кладовщик открывает машину из монитора смены.")
        return
    await state.set_state(StaffFlow.checkpoint)
    await _reply(query, PASS_PROMPT)


@router.callback_query(F.data == SHIFT)
async def show_shift(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    await state.set_state(None)
    await _send_shift(query, engine, "day")


@router.callback_query(F.data == "sh:pin")
async def ask_pass_pin(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    if person.role is not StaffRole.security:
        await _reply(query, RIGHTS)
        return
    await state.set_state(StaffFlow.checkpoint)
    await _reply(query, PASS_PIN_PROMPT)


@router.callback_query(F.data.startswith("sh:"), ~F.data.startswith("sh:open:"))
async def shift_filter(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    raw = query.data or ""
    if raw.startswith("sh:refresh:"):
        mode = raw.removeprefix("sh:refresh:")
    else:
        mode = {
            "sh:hour": "hour",
            "sh:day": "day",
            "sh:wait": "wait",
            "sh:onsite": "onsite",
            "sh:done": "done",
        }.get(raw, "day")
    await state.set_state(None)
    await _send_shift(query, engine, mode)


@router.message(StaffFlow.checkpoint, F.photo)
async def pass_photo(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None or not message.photo:
        return
    person = await _guard_message(message, state, engine)
    if person is None:
        return
    if person.role is not StaffRole.security:
        await state.clear()
        await message.answer(DENIED)
        return
    image = await _photo_bytes(message)
    if image is None:
        await message.answer(UNREADABLE)
        return
    try:
        trip = await asyncio.to_thread(read_pass_image, engine, user.id, image)
    except AccessDenied:
        await state.clear()
        await message.answer(DENIED)
        return
    except PassNotFound:
        await message.answer(NOT_FOUND)
        return
    except PassExpired:
        await message.answer(PASS_EXPIRED)
        return
    if trip is None:
        log.info("pass qr was not recognized")
        await message.answer(UNREADABLE, reply_markup=_scan_fallback())
        return
    await _show_checkpoint(message, engine, trip)


@router.message(StaffFlow.checkpoint, F.text)
async def pass_pin(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    text = (message.text or "").strip()
    if user is None or text.startswith("/"):
        return
    person = await _guard_message(message, state, engine)
    if person is None:
        return
    if person.role is not StaffRole.security:
        await state.clear()
        await message.answer(DENIED)
        return
    try:
        trip = await asyncio.to_thread(find_pass, engine, user.id, pin=text)
    except AccessDenied:
        await state.clear()
        await message.answer(DENIED)
        return
    except PassNotFound:
        await message.answer(
            "Пропуск не найден. Проверьте цифры или пришлите фото QR ещё раз.",
            reply_markup=_scan_fallback(),
        )
        return
    except PassExpired:
        await message.answer(PASS_EXPIRED)
        return
    await _show_checkpoint(message, engine, trip)


@router.callback_query(F.data.startswith(OPEN_PREFIX))
async def open_row(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    booking_id = _id(query.data, OPEN_PREFIX)
    user = query.from_user
    if booking_id is None or user is None:
        await _reply(query, NOT_FOUND)
        return
    try:
        trip = await asyncio.to_thread(load_trip, engine, user.id, booking_id)
    except AccessDenied:
        await state.clear()
        await _reply(query, DENIED)
        return
    if trip is None:
        await _reply(query, NOT_FOUND)
        return
    await state.update_data(staff_edit_id=trip.id)
    await _show_row(query, engine, trip)


@router.callback_query(F.data.startswith("st:edit:"))
async def edit_party(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    person = await _guard(query, state, engine)
    if person is None:
        return
    booking_id = _id(query.data, "st:edit:")
    user = query.from_user
    if booking_id is None or user is None:
        await _reply(query, NOT_FOUND)
        return
    try:
        trip = await asyncio.to_thread(load_trip, engine, user.id, booking_id)
    except AccessDenied:
        await _reply(query, DENIED)
        return
    if trip is None:
        await _reply(query, NOT_FOUND)
        return
    block = await asyncio.to_thread(change_block, engine, trip)
    if block is not None:
        await _reply(query, block)
        return
    await state.set_state(StaffFlow.edit_plate)
    await state.update_data(staff_edit_id=trip.id)
    await _reply(query, "Новый госномер, например А123ВС178.")


@router.message(StaffFlow.edit_plate, F.text)
async def staff_edit_plate(message: Message, state: FSMContext) -> None:
    try:
        plate = normalize_plate(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(staff_plate=plate)
    await state.set_state(StaffFlow.edit_make)
    await message.answer("Марка машины.")


@router.message(StaffFlow.edit_make, F.text)
async def staff_edit_make(message: Message, state: FSMContext) -> None:
    try:
        field = clean_make(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(staff_make=field.value)
    await state.set_state(StaffFlow.edit_name)
    await message.answer("ФИО: три слова, только буквы.")


@router.message(StaffFlow.edit_name, F.text)
async def staff_edit_name(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None:
        return
    try:
        field = clean_driver_name(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    booking_id = data.get("staff_edit_id")
    if not booking_id:
        await state.clear()
        await message.answer("Кнопка устарела. Откройте карточку рейса снова.")
        return
    try:
        trip = await asyncio.to_thread(
            staff_update_party,
            engine,
            telegram_id=user.id,
            booking_id=int(booking_id),
            plate=data.get("staff_plate", ""),
            make=data.get("staff_make", ""),
            driver_name=field.value,
        )
    except BookingError as exc:
        await message.answer(str(exc))
        if str(exc) == DAY_TAKEN:
            await state.set_state(StaffFlow.edit_plate)
            await message.answer("Укажите другой госномер, например А123ВС178.")
        return
    await state.set_state(None)
    role = _staff_role(engine, user.id)
    text = await asyncio.to_thread(_card, engine, trip, role)
    await message.answer(f"Данные обновлены.\n\n{text}", reply_markup=_markup(engine, trip, role))


@router.callback_query(F.data.startswith(ARRIVED_PREFIX))
async def mark_arrived(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    await _mark(query, state, engine, ARRIVED_PREFIX, BookingStatus.arrived, card=True)


@router.callback_query(F.data.startswith(UNLOADING_PREFIX))
async def mark_unloading(
    query: CallbackQuery, state: FSMContext, engine: Engine
) -> None:
    await _mark(
        query, state, engine, UNLOADING_PREFIX, BookingStatus.unloading, card=False
    )


@router.callback_query(F.data.startswith(COMPLETED_PREFIX))
async def mark_completed(
    query: CallbackQuery, state: FSMContext, engine: Engine
) -> None:
    from dockbot.bot.yard import begin_close

    await begin_close(query, state, engine)


async def _mark(
    query: CallbackQuery,
    state: FSMContext,
    engine: Engine,
    prefix: str,
    target: BookingStatus,
    *,
    card: bool,
) -> None:
    if not await _guard(query, state, engine):
        return
    booking_id = _id(query.data, prefix)
    user = query.from_user
    if booking_id is None or user is None:
        await _reply(query, NOT_FOUND)
        return
    try:
        gate_id = _second_id(query.data or "", prefix)
        trip = await asyncio.to_thread(
            apply_mark,
            engine,
            user.id,
            booking_id,
            target,
            None,
            gate_id,
        )
    except AccessDenied:
        await state.clear()
        await _reply(query, DENIED)
        return
    except PassNotFound:
        await _reply(query, NOT_FOUND)
        return
    except PassExpired:
        await _reply(query, PASS_EXPIRED)
        return
    except UnloadPaused:
        await _reply(query, "Сначала продолжите разгрузку.")
        return
    except TransitionRejected:
        trip = await _current(engine, user.id, booking_id)
        if trip is None:
            await _reply(query, REJECTED)
            return
        text = await asyncio.to_thread(_body, engine, trip, card, _staff_role(engine, user.id))
        note = (
            "Статус уже установлен."
            if trip.status == target
            else "Этот переход недоступен. Выберите следующий шаг на карточке."
        )
        await _reply(
            query, f"{note}\n\n{text}", _markup(engine, trip, _staff_role(engine, user.id))
        )
        return
    text = await asyncio.to_thread(_body, engine, trip, card, _staff_role(engine, user.id))
    await _reply(query, text, _markup(engine, trip, _staff_role(engine, user.id)))


async def _show_checkpoint(message: Message, engine: Engine, trip: Trip) -> None:
    user = message.from_user
    role = _staff_role(engine, None if user is None else user.id)
    zone_text = await asyncio.to_thread(_card, engine, trip, role)
    await message.answer(zone_text, reply_markup=_markup(engine, trip, role))
    await _send_document(message, trip, role)


async def _show_row(
    query: CallbackQuery, engine: Engine, trip: Trip
) -> None:
    user = query.from_user
    role = _staff_role(engine, None if user is None else user.id)
    text = await asyncio.to_thread(_card, engine, trip, role)
    await _reply(query, text, _markup(engine, trip, role))
    if query.message is not None:
        await _send_document(query.message, trip, role)


async def _send_shift(query: CallbackQuery, engine: Engine, mode: str) -> None:
    user = query.from_user
    if user is None:
        await _reply(query, DENIED)
        return
    try:
        zone, day, trips = await asyncio.to_thread(load_shift, engine, user.id)
    except AccessDenied:
        await _reply(query, RIGHTS)
        return
    except SiteNotConfigured:
        await _reply(query, "Склад не настроен. Обратитесь к администратору стенда.")
        return
    moment = datetime.now(timezone.utc)
    chosen = filter_shift(trips, zone, mode, moment)
    message = query.message
    await query.answer()
    target = message if message is not None and hasattr(message, "answer") else None
    if not chosen and mode != "day":
        text = "В этом списке пусто. Нажмите «Весь день» или «Обновить»."
        markup = _controls(mode)
        if target is None and user is not None:
            await query.bot.send_message(user.id, text, reply_markup=markup)
        elif target is not None:
            await target.answer(text, reply_markup=markup)
        return
    pages = shift_pages(chosen, zone, day)
    await _deliver(query, pages, zone, mode, answered=True)


def _body(engine: Engine, trip: Trip, card: bool, role: StaffRole) -> str:
    if card:
        return _card(engine, trip, role)
    from dockbot.booking import load_zone

    return trip_line(trip, load_zone(engine))


def _card(engine: Engine, trip: Trip, role: StaffRole) -> str:
    from dockbot.booking import load_zone

    documents = "never" if role is StaffRole.warehouse else "always"
    text = "Пропуск\n" + card_text(trip, load_zone(engine), None, documents)
    if trip.no_driver_chat:
        text += "\nНапоминания не уйдут: у рейса нет чата водителя."
    if trip.departed:
        text += "\nВыезд с территории отмечен."
    if (
        trip.status == BookingStatus.arrived
        and not actions_for(engine, trip, StaffRole.warehouse)
        and not actions_for(engine, trip, StaffRole.admin)
    ):
        text += "\nНет ворот для этого транспорта."
    return text


def _markup(engine: Engine, trip: Trip, role: StaffRole) -> InlineKeyboardMarkup | None:
    actions = actions_for(engine, trip, role)
    rows = [
        [InlineKeyboardButton(text=action.label, callback_data=action.callback)]
        for action in actions
    ]
    if (
        trip.no_driver_chat
        and role in {StaffRole.security, StaffRole.admin}
        and not slot_has_passed(trip.slot_end)
    ):
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    text="Показать пропуск",
                    callback_data=f"st:pass:{trip.id}",
                )
            ],
        )
    if change_block(engine, trip) is None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Изменить данные", callback_data=f"st:edit:{trip.id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="К списку", callback_data=SHIFT),
            InlineKeyboardButton(text="В меню", callback_data=MENU),
        ]
    )
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _page_markup(page: ShiftPage, zone: ZoneInfo) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    for trip in page.trips:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_clip(trip_line(trip, zone)),
                    callback_data=f"{OPEN_PREFIX}{trip.id}",
                )
            ]
        )
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _clip(text: str) -> str:
    if len(text) <= 64:
        return text
    return text[:63] + "…"


def _controls(mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Текущий час", callback_data="sh:hour"),
                InlineKeyboardButton(text="Весь день", callback_data="sh:day"),
            ],
            [
                InlineKeyboardButton(text="Обновить", callback_data=f"sh:refresh:{mode}"),
                InlineKeyboardButton(text="В меню", callback_data=MENU),
            ],
        ]
    )


def _scan_fallback() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ввести PIN пропуска", callback_data="sh:pin"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=MENU)],
        ]
    )


def _with_controls(
    markup: InlineKeyboardMarkup | None, mode: str, first: bool
) -> InlineKeyboardMarkup:
    if not first:
        return markup or _controls(mode)
    extra = list(_controls(mode).inline_keyboard)
    if markup is None:
        return InlineKeyboardMarkup(inline_keyboard=extra)
    return InlineKeyboardMarkup(inline_keyboard=[*markup.inline_keyboard, *extra])


async def _deliver(
    query: CallbackQuery,
    pages: list[ShiftPage],
    zone: ZoneInfo,
    mode: str = "day",
    *,
    answered: bool = False,
) -> None:
    message = query.message
    if not answered:
        await query.answer()
    if message is None or not hasattr(message, "answer"):
        if query.from_user is not None:
            for index, page in enumerate(pages):
                await query.bot.send_message(
                    query.from_user.id,
                    page.text,
                    reply_markup=_with_controls(
                        _page_markup(page, zone), mode, index == 0
                    ),
                )
        return
    for index, page in enumerate(pages):
        await message.answer(
            page.text,
            reply_markup=_with_controls(_page_markup(page, zone), mode, index == 0),
        )


async def open_checkpoint(
    message: Message, state: FSMContext, engine: Engine
) -> None:
    user = message.from_user
    if user is None:
        await message.answer(DENIED)
        return
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None or person.role is not StaffRole.security:
        await message.answer(DENIED)
        return
    await state.set_state(StaffFlow.checkpoint)
    await message.answer(PASS_PROMPT)


async def open_pass_pin(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None:
        await message.answer(RIGHTS)
        return
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None or person.role is not StaffRole.security:
        await message.answer(
            RIGHTS + " PIN пропуска вводит охрана. Кладовщик берёт машину из списка."
        )
        return
    await state.set_state(StaffFlow.checkpoint)
    await message.answer(PASS_PIN_PROMPT)


async def open_shift(
    message: Message, state: FSMContext, engine: Engine, mode: str = "day"
) -> None:
    user = message.from_user
    if user is None:
        await message.answer(RIGHTS)
        return
    try:
        zone, day, trips = await asyncio.to_thread(load_shift, engine, user.id)
    except AccessDenied:
        await message.answer(RIGHTS + " Сначала войдите как охрана или кладовщик.")
        return
    except SiteNotConfigured:
        await message.answer("Склад не настроен. Обратитесь к администратору стенда.")
        return
    await state.set_state(None)
    moment = datetime.now(timezone.utc)
    chosen = filter_shift(trips, zone, mode, moment)
    if not chosen and mode != "day":
        await message.answer(
            "В этом списке пусто. Нажмите «Весь день» или «Обновить».",
            reply_markup=_controls(mode),
        )
        return
    pages = shift_pages(chosen, zone, day)
    for index, page in enumerate(pages):
        await message.answer(
            page.text,
            reply_markup=_with_controls(_page_markup(page, zone), mode, index == 0),
        )


async def _leave(
    message: Message, state: FSMContext, engine: Engine, telegram_id: int
) -> None:
    await state.clear()
    await asyncio.to_thread(logout_staff, engine, telegram_id)
    await message.answer(LOGGED_OUT, reply_markup=driver_menu())


async def _guard(query: CallbackQuery, state: FSMContext, engine: Engine):
    user = query.from_user
    if user is None:
        await _reply(query, DENIED)
        return None
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None:
        await state.clear()
        await _reply(query, DENIED)
        return None
    return person


async def _guard_message(message: Message, state: FSMContext, engine: Engine):
    user = message.from_user
    if user is None:
        return None
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None:
        await state.clear()
        await message.answer(DENIED)
        return None
    return person


async def _current(engine: Engine, telegram_id: int, booking_id: int) -> Trip | None:
    try:
        return await asyncio.to_thread(load_trip, engine, telegram_id, booking_id)
    except AccessDenied:
        return None


async def _photo_bytes(message: Message) -> bytes | None:
    if not message.photo:
        return None
    buffer = io.BytesIO()
    try:
        await message.bot.download(message.photo[-1], destination=buffer)
    except Exception:
        log.info("pass photo was not read")
        return None
    return buffer.getvalue()


async def _reply(
    query: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    message = query.message
    await query.answer()
    if message is not None and hasattr(message, "answer"):
        await message.answer(text, reply_markup=markup)
        return
    if query.from_user is not None:
        await query.bot.send_message(query.from_user.id, text, reply_markup=markup)


@router.callback_query(F.data.startswith("st:deny:"))
async def deny_entry(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    if not await _guard(query, state, engine):
        return
    booking_id = _id(query.data, "st:deny:")
    user = query.from_user
    if booking_id is None or user is None:
        await _reply(query, NOT_FOUND)
        return
    try:
        trip = await asyncio.to_thread(refuse_entry, engine, user.id, booking_id)
    except AccessDenied:
        await _reply(query, DENIED)
        return
    except PassNotFound:
        await _reply(query, NOT_FOUND)
        return
    except TransitionRejected:
        await _reply(query, "Не пустить можно только при расхождении документа, пока машина не на территории.")
        return
    role = _staff_role(engine, user.id)
    text = await asyncio.to_thread(_card, engine, trip, role)
    await _reply(query, f"Рейс не пустили. Бронь отменена.\n\n{text}", _markup(engine, trip, role))


@router.callback_query(F.data.startswith("st:out:"))
async def mark_exit(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    if not await _guard(query, state, engine):
        return
    booking_id = _id(query.data, "st:out:")
    user = query.from_user
    if booking_id is None or user is None:
        await _reply(query, NOT_FOUND)
        return
    try:
        trip = await asyncio.to_thread(mark_departed, engine, user.id, booking_id)
    except AccessDenied:
        await _reply(query, DENIED)
        return
    except PassNotFound:
        await _reply(query, NOT_FOUND)
        return
    except TransitionRejected:
        await _reply(query, "Выезд уже отмечен или рейс ещё не завершён.")
        return
    role = _staff_role(engine, user.id)
    text = await asyncio.to_thread(_card, engine, trip, role)
    await _reply(query, text, _markup(engine, trip, role))


@router.callback_query(F.data.startswith("st:pass:"))
async def show_desk_pass(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    if not await _guard(query, state, engine):
        return
    booking_id = _id(query.data, "st:pass:")
    user = query.from_user
    if booking_id is None or user is None:
        await _reply(query, NOT_FOUND)
        return
    trip = await asyncio.to_thread(load_trip, engine, user.id, booking_id)
    if trip is None or not trip.no_driver_chat:
        await _reply(query, NOT_FOUND)
        return
    if slot_has_passed(trip.slot_end):
        await _reply(query, PASS_EXPIRED)
        return
    from aiogram.types import BufferedInputFile

    from dockbot.booking import load_zone
    from dockbot.passes import qr_png
    from dockbot.present import pass_caption

    zone = await asyncio.to_thread(load_zone, engine)
    message = query.message
    await query.answer()
    if message is None or not hasattr(message, "answer_photo"):
        return
    await message.answer_photo(
        BufferedInputFile(qr_png(trip.pass_token), filename="pass.png"),
        caption=pass_caption(trip, zone) + "\nНапоминания не уйдут: у рейса нет чата водителя.",
    )


async def _send_document(message: Message, trip: Trip, role: StaffRole) -> None:
    if role is StaffRole.warehouse or not trip.doc_name:
        return
    path = document_path(trip.doc_name)
    if path is None or not path.is_file() or path.stat().st_size == 0:
        if hasattr(message, "answer"):
            await message.answer("Фото документа не найдено. Результат сверки есть в тексте карточки.")
        return
    if not hasattr(message, "answer_photo"):
        return
    caption = "Документ: сходится" if trip.doc_result == "match" else "Документ: расхождение"
    if trip.doc_result not in {"match", "mismatch"}:
        caption = "Документ: фото есть, сверка не закрыта"
    await message.answer_photo(FSInputFile(path), caption=caption)


def _staff_role(engine: Engine, telegram_id: int | None) -> StaffRole:
    if telegram_id is None:
        return StaffRole.security
    person = active_staff(engine, telegram_id)
    if person is None:
        return StaffRole.security
    return person.role


def _second_id(data: str, prefix: str) -> int | None:
    if not data.startswith(prefix):
        return None
    parts = data[len(prefix) :].split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1])


def _id(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    raw = data[len(prefix) :].split(":", 1)[0]
    if not raw.isdigit():
        return None
    return int(raw)
