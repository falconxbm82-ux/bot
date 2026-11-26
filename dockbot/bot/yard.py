"""Экраны кладовщика: очередь, пауза, расхождение, фото и сверка."""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.engine import Engine

from dockbot.access import AccessDenied, active_staff
from dockbot.documents import DocumentError
from dockbot.models import StaffRole
from dockbot.yard import (
    FACT_CARGO,
    FACT_PLACES,
    FACT_WEIGHT,
    PAUSE_CARGO,
    PAUSE_GATE,
    CloseMismatch,
    YardError,
    assert_can_close,
    close_unload,
    mark_damage,
    queue_for_trip,
    record_fact,
    resume_unload,
    save_unload_photo,
    set_pause,
    start_unload,
)

router = Router(name="yard")

RIGHTS = "Недостаточно прав."


class YardFlow(StatesGroup):
    fact = State()
    photo = State()
    plate = State()
    cargo = State()
    weight = State()
    places = State()


def _id(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    raw = data.removeprefix(prefix)
    return int(raw) if raw.isdigit() else None


async def _role(query: CallbackQuery, engine: Engine) -> StaffRole | None:
    user = query.from_user
    if user is None:
        return None
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None or person.role not in {StaffRole.warehouse, StaffRole.admin}:
        return None
    return person.role


async def _say(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    message = query.message
    await query.answer()
    if message is not None and hasattr(message, "answer"):
        await message.answer(text, reply_markup=markup)
        return
    if query.from_user is not None:
        await query.bot.send_message(query.from_user.id, text, reply_markup=markup)


async def _paint(event: Message | CallbackQuery, engine: Engine, trip) -> None:
    from dockbot.bot.staff import _card, _markup, _staff_role

    user = event.from_user
    role = _staff_role(engine, None if user is None else user.id)
    text = await asyncio.to_thread(_card, engine, trip, role)
    markup = _markup(engine, trip, role)
    if isinstance(event, CallbackQuery):
        await _say(event, text, markup)
        return
    await event.answer(text, reply_markup=markup)


async def begin_close(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    if await _role(query, engine) is None:
        await _say(query, RIGHTS)
        return
    booking_id = _id(query.data, "st:done:")
    user = query.from_user
    if booking_id is None or user is None:
        await _say(query, "Бронь не найдена.")
        return
    try:
        await asyncio.to_thread(assert_can_close, engine, user.id, booking_id)
    except AccessDenied:
        await _say(query, RIGHTS)
        return
    except YardError as exc:
        await _say(query, str(exc))
        return
    await state.set_state(YardFlow.plate)
    await state.update_data(yard_id=booking_id)
    await _say(query, "Госномер для сверки.")


@router.callback_query(F.data.startswith("wh:queue:"))
async def show_queue(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    await state.set_state(None)
    if await _role(query, engine) is None:
        await _say(query, RIGHTS)
        return
    booking_id = _id(query.data, "wh:queue:")
    user = query.from_user
    if booking_id is None or user is None:
        await _say(query, "Бронь не найдена.")
        return
    try:
        text = await asyncio.to_thread(queue_for_trip, engine, user.id, booking_id)
    except AccessDenied:
        await _say(query, RIGHTS)
        return
    except YardError as exc:
        await _say(query, str(exc))
        return
    await _say(query, text)


@router.callback_query(F.data.startswith("wh:start:"))
async def begin_unload(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    await state.set_state(None)
    await _run(query, engine, "wh:start:", start_unload)


@router.callback_query(F.data.startswith("wh:pause:"))
async def ask_pause(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    await state.set_state(None)
    if await _role(query, engine) is None:
        await _say(query, RIGHTS)
        return
    booking_id = _id(query.data, "wh:pause:")
    if booking_id is None:
        await _say(query, "Бронь не найдена.")
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ворота заняты", callback_data=f"wh:preason:{PAUSE_GATE}:{booking_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Груз не готов", callback_data=f"wh:preason:{PAUSE_CARGO}:{booking_id}"
                )
            ],
        ]
    )
    await _say(query, "Почему пауза?", markup)


@router.callback_query(F.data.startswith("wh:preason:"))
async def save_pause(query: CallbackQuery, engine: Engine) -> None:
    if await _role(query, engine) is None:
        await _say(query, RIGHTS)
        return
    raw = (query.data or "").removeprefix("wh:preason:")
    reason, _, tail = raw.partition(":")
    if not tail.isdigit():
        await _say(query, "Кнопка устарела")
        return
    user = query.from_user
    if user is None:
        return
    try:
        trip = await asyncio.to_thread(set_pause, engine, user.id, int(tail), reason)
    except AccessDenied:
        await _say(query, RIGHTS)
        return
    except YardError as exc:
        await _say(query, str(exc))
        return
    await _paint(query, engine, trip)


@router.callback_query(F.data.startswith("wh:resume:"))
async def continue_unload(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    await state.set_state(None)
    await _run(query, engine, "wh:resume:", resume_unload)


@router.callback_query(F.data.startswith("wh:fact:"))
async def ask_fact(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    await state.set_state(None)
    if await _role(query, engine) is None:
        await _say(query, RIGHTS)
        return
    booking_id = _id(query.data, "wh:fact:")
    if booking_id is None:
        await _say(query, "Бронь не найдена.")
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Другой вес", callback_data=f"wh:fkind:{FACT_WEIGHT}:{booking_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Другой тип", callback_data=f"wh:fkind:{FACT_CARGO}:{booking_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Нехватка мест", callback_data=f"wh:fkind:{FACT_PLACES}:{booking_id}"
                )
            ],
        ]
    )
    await _say(query, "Какое расхождение?", markup)


@router.callback_query(F.data.startswith("wh:fkind:"))
async def ask_fact_value(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    if await _role(query, engine) is None:
        await _say(query, RIGHTS)
        return
    raw = (query.data or "").removeprefix("wh:fkind:")
    kind, _, tail = raw.partition(":")
    if kind not in {FACT_WEIGHT, FACT_CARGO, FACT_PLACES} or not tail.isdigit():
        await _say(query, "Кнопка устарела")
        return
    await state.set_state(YardFlow.fact)
    await state.update_data(yard_id=int(tail), fact_kind=kind)
    prompt = {
        FACT_WEIGHT: "Введите фактический вес.",
        FACT_CARGO: "Введите фактический тип груза.",
        FACT_PLACES: "Введите фактическое число мест.",
    }[kind]
    await _say(query, prompt)


@router.message(YardFlow.fact, F.text)
async def save_fact(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None or (message.text or "").startswith("/"):
        return
    data = await state.get_data()
    booking_id = data.get("yard_id")
    kind = data.get("fact_kind")
    if not isinstance(booking_id, int) or not isinstance(kind, str):
        await state.clear()
        await message.answer("Расхождение устарело. Откройте карточку снова.")
        return
    try:
        trip = await asyncio.to_thread(
            record_fact, engine, user.id, booking_id, kind, message.text or ""
        )
    except AccessDenied:
        await state.clear()
        await message.answer(RIGHTS)
        return
    except YardError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await _paint(message, engine, trip)


@router.callback_query(F.data.startswith("wh:photo:"))
async def ask_photo(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    if await _role(query, engine) is None:
        await _say(query, RIGHTS)
        return
    booking_id = _id(query.data, "wh:photo:")
    if booking_id is None:
        await _say(query, "Бронь не найдена.")
        return
    await state.set_state(YardFlow.photo)
    await state.update_data(yard_id=booking_id)
    await _say(query, "Пришлите фото разгрузки.")


@router.message(YardFlow.photo, F.photo)
async def save_photo(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None:
        return
    data = await state.get_data()
    booking_id = data.get("yard_id")
    if not isinstance(booking_id, int):
        await state.clear()
        await message.answer("Фото устарело. Откройте карточку снова.")
        return
    image = await _photo_bytes(message)
    if image is None:
        await message.answer("Фото пустое. Пришлите снимок разгрузки.")
        return
    try:
        trip = await asyncio.to_thread(save_unload_photo, engine, user.id, booking_id, image)
    except AccessDenied:
        await state.clear()
        await message.answer(RIGHTS)
        return
    except (YardError, DocumentError) as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await _paint(message, engine, trip)


@router.callback_query(F.data.startswith("wh:damage:"))
async def save_damage(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    await state.set_state(None)
    await _run(query, engine, "wh:damage:", mark_damage)


@router.message(YardFlow.photo, ~F.photo)
async def photo_needed(message: Message) -> None:
    await message.answer("Пришлите фото разгрузки.")


@router.message(YardFlow.fact, ~F.text)
async def fact_needed(message: Message) -> None:
    await message.answer("Введите факт текстом.")


@router.message(
    StateFilter(YardFlow.plate, YardFlow.cargo, YardFlow.weight, YardFlow.places),
    ~F.text,
)
async def check_needed(message: Message) -> None:
    await message.answer("Для сверки нужен текст.")


@router.message(YardFlow.plate, F.text)
async def check_plate(message: Message, state: FSMContext) -> None:
    if message.from_user is None or (message.text or "").startswith("/"):
        return
    await state.update_data(check_plate=message.text or "")
    await state.set_state(YardFlow.cargo)
    await message.answer("Тип груза для сверки.")


@router.message(YardFlow.cargo, F.text)
async def check_cargo(message: Message, state: FSMContext) -> None:
    if message.from_user is None or (message.text or "").startswith("/"):
        return
    await state.update_data(check_cargo=message.text or "")
    await state.set_state(YardFlow.weight)
    await message.answer("Вес в тоннах для сверки.")


@router.message(YardFlow.weight, F.text)
async def check_weight(message: Message, state: FSMContext) -> None:
    if message.from_user is None or (message.text or "").startswith("/"):
        return
    await state.update_data(check_weight=message.text or "")
    await state.set_state(YardFlow.places)
    await message.answer("Число мест для сверки.")


@router.message(YardFlow.places, F.text)
async def check_places(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None or (message.text or "").startswith("/"):
        return
    data = await state.get_data()
    booking_id = data.get("yard_id")
    if not isinstance(booking_id, int):
        await state.clear()
        await message.answer("Сверка устарела. Откройте карточку снова.")
        return
    try:
        trip = await asyncio.to_thread(
            close_unload,
            engine,
            user.id,
            booking_id,
            plate=str(data.get("check_plate") or ""),
            cargo=str(data.get("check_cargo") or ""),
            weight_text=str(data.get("check_weight") or ""),
            places_text=message.text or "",
        )
    except AccessDenied:
        await state.clear()
        await message.answer(RIGHTS)
        return
    except CloseMismatch as exc:
        await state.clear()
        await message.answer(str(exc))
        return
    except YardError as exc:
        await state.clear()
        await message.answer(str(exc))
        return
    await state.clear()
    await _paint(message, engine, trip)


async def _run(query: CallbackQuery, engine: Engine, prefix: str, action) -> None:
    if await _role(query, engine) is None:
        await _say(query, RIGHTS)
        return
    booking_id = _id(query.data, prefix)
    user = query.from_user
    if booking_id is None or user is None:
        await _say(query, "Бронь не найдена.")
        return
    try:
        trip = await asyncio.to_thread(action, engine, user.id, booking_id)
    except AccessDenied:
        await _say(query, RIGHTS)
        return
    except YardError as exc:
        await _say(query, str(exc))
        return
    await _paint(query, engine, trip)


async def _photo_bytes(message: Message) -> bytes | None:
    if not message.photo:
        return None
    import io

    buffer = io.BytesIO()
    try:
        await message.bot.download(message.photo[-1], destination=buffer)
    except Exception:
        return None
    return buffer.getvalue()
