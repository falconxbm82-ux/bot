"""Запись рейса администратором, если у водителя нет чата."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.engine import Engine

from dockbot.access import AccessDenied, active_staff
from dockbot.booking import (
    DAY_TAKEN,
    HEAVY_CARGOS,
    LIGHT_CARGOS,
    NOT_LISTED,
    BookingError,
    FieldError,
    SiteNotConfigured,
    SlotTaken,
    available_dates,
    clean_cargo,
    clean_driver_name,
    clean_places,
    clean_make,
    clean_weight_tons,
    create_booking,
    driver_known,
    gates_with_slots,
    load_zone,
    normalize_plate,
    offered_cargos,
    pair_booked_on,
    slots_for_gate,
    window_phrase,
)
from dockbot.bot.keyboard import keyboard_for
from dockbot.bot.ui import (
    CARGO_LABELS,
    CARGO_PREFIX,
    DATE_PREFIX,
    GATE_PREFIX,
    SLOT_PREFIX,
    actor_ids,
    cargo_markup,
    dates_markup,
    gates_markup,
    menu_markup,
    reply,
    slots_markup,
    vehicle_markup,
    weight_markup,
)
from dockbot.models import StaffRole
from dockbot.passes import qr_png
from dockbot.present import pass_caption

router = Router(name="desk")
WIZARD_DAYS = 7
_NOTE = "Напоминания не уйдут: у рейса нет чата водителя. Пропуск покажет охрана на КПП."


class DeskFlow(StatesGroup):
    vehicle = State()
    cargo = State()
    cargo_note = State()
    weight = State()
    weight_note = State()
    places = State()
    date = State()
    gate = State()
    slot = State()
    plate = State()
    make = State()
    driver_name = State()


@router.callback_query(F.data == "ad:desk")
async def begin_desk(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    if not await _admin(query, engine):
        return
    await state.clear()
    await state.set_state(DeskFlow.vehicle)
    await reply(query, "Запись без чата водителя.\nВид транспорта.", vehicle_markup())


@router.callback_query(DeskFlow.vehicle, F.data.startswith("bk:veh:"))
async def desk_vehicle(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    kind = (query.data or "").removeprefix("bk:veh:")
    if kind not in {"light", "heavy"}:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(vehicle=kind)
    rows = await asyncio.to_thread(offered_cargos, engine, kind)
    minutes = {code: shown for code, _label, shown in rows}
    await state.set_state(DeskFlow.cargo)
    text = "Тип груза. Минуты разгрузки указаны на кнопке."
    if kind == "light":
        text = "Тип груза для малотоннажного. Минуты разгрузки указаны на кнопке."
    await reply(query, text, cargo_markup(minutes, kind))


@router.callback_query(DeskFlow.cargo, F.data.startswith(CARGO_PREFIX))
async def desk_cargo(query: CallbackQuery, state: FSMContext) -> None:
    code = (query.data or "").removeprefix(CARGO_PREFIX)
    data = await state.get_data()
    vehicle = data.get("vehicle") or "heavy"
    if code == "other" and vehicle != "light":
        await state.set_state(DeskFlow.cargo_note)
        await reply(query, "Напишите тип груза.", menu_markup())
        return
    label = CARGO_LABELS.get(code) if _desk_cargo_ok(vehicle, code) else None
    if label is None:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(cargo=label)
    await state.set_state(DeskFlow.weight)
    text = "Вес груза в тоннах."
    if vehicle == "light":
        text = "Вес груза в тоннах. Для малотоннажного не больше 5."
    await reply(query, text, weight_markup(vehicle))


@router.message(DeskFlow.cargo_note, F.text)
async def desk_cargo_note(message: Message, state: FSMContext) -> None:
    try:
        field = clean_cargo(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(cargo=field.value)
    data = await state.get_data()
    vehicle = data.get("vehicle") or "heavy"
    await state.set_state(DeskFlow.weight)
    await message.answer("Вес груза в тоннах.", reply_markup=weight_markup(vehicle))


@router.callback_query(DeskFlow.weight, F.data.startswith("bk:weight:"))
async def desk_weight(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    raw = (query.data or "").removeprefix("bk:weight:")
    data = await state.get_data()
    vehicle = data.get("vehicle") or "heavy"
    if raw == "other":
        await state.set_state(DeskFlow.weight_note)
        limit = 5 if vehicle == "light" else 40
        await reply(query, f"Вес в тоннах, целое число от 1 до {limit}.", menu_markup())
        return
    limit_kg = 5000 if vehicle == "light" else 40000
    if not raw.isdigit() or not 1000 <= int(raw) <= limit_kg:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(weight_kg=int(raw))
    await _ask_places(query, state)


@router.message(DeskFlow.weight_note, F.text)
async def desk_weight_note(message: Message, state: FSMContext, engine: Engine) -> None:
    data = await state.get_data()
    try:
        weight_kg = clean_weight_tons(message.text or "", data.get("vehicle") or "heavy")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(weight_kg=weight_kg)
    await _ask_places(message, state)


@router.callback_query(DeskFlow.date, F.data.startswith(DATE_PREFIX))
async def desk_date(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    raw = (query.data or "").removeprefix(DATE_PREFIX)
    try:
        date.fromisoformat(raw)
    except ValueError:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(date=raw)
    await _gates(query, state, engine)


@router.callback_query(DeskFlow.gate, F.data.startswith(GATE_PREFIX))
async def desk_gate(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    raw = (query.data or "").removeprefix(GATE_PREFIX)
    if not raw.isdigit():
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(gate_id=int(raw))
    await _slots(query, state, engine)


@router.callback_query(DeskFlow.slot, F.data.startswith(SLOT_PREFIX))
async def desk_slot(query: CallbackQuery, state: FSMContext) -> None:
    raw = (query.data or "").removeprefix(SLOT_PREFIX)
    if not raw.isdigit():
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(slot_ts=int(raw))
    await state.set_state(DeskFlow.plate)
    await reply(query, "Госномер, например А123ВС178.", menu_markup())


@router.message(DeskFlow.plate, F.text)
async def desk_plate(message: Message, state: FSMContext) -> None:
    try:
        plate = normalize_plate(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(plate=plate)
    await state.set_state(DeskFlow.make)
    await message.answer("Марка машины.")


@router.message(DeskFlow.make, F.text)
async def desk_make(message: Message, state: FSMContext) -> None:
    try:
        field = clean_make(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(make=field.value)
    await state.set_state(DeskFlow.driver_name)
    await message.answer("ФИО водителя.")


@router.message(DeskFlow.driver_name, F.text)
async def desk_name(message: Message, state: FSMContext, engine: Engine) -> None:
    user = message.from_user
    if user is None:
        return
    try:
        field = clean_driver_name(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    plate = data.get("plate") or ""
    try:
        known = await asyncio.to_thread(driver_known, engine, plate, field.value)
    except FieldError as exc:
        await message.answer(str(exc))
        return
    if not known:
        await state.set_state(DeskFlow.plate)
        await message.answer(f"{NOT_LISTED}\nГосномер, например А123ВС178.")
        return
    if data.get("date"):
        taken = await asyncio.to_thread(
            pair_booked_on, engine, date.fromisoformat(data["date"]), plate, field.value
        )
        if taken:
            await state.set_state(DeskFlow.plate)
            await message.answer(
                f"{DAY_TAKEN}\nУкажите другой госномер, например А123ВС178."
            )
            return
    slot = await _selected(engine, data)
    if slot is None:
        await state.clear()
        await message.answer(
            "Слот уже недоступен. Откройте «Сводка дня» и нажмите «Записать без чата».",
            reply_markup=keyboard_for(StaffRole.admin),
        )
        return
    try:
        trip = await asyncio.to_thread(
            create_booking,
            engine,
            gate_id=slot.gate_id,
            slot_start=slot.start,
            plate=data.get("plate", ""),
            make=data.get("make", ""),
            driver_name=field.value,
            cargo_type=data.get("cargo", ""),
            telegram_id=None,
            chat_id=None,
            vehicle=data.get("vehicle") or "heavy",
            weight_kg=data.get("weight_kg"),
            place_count=data.get("places"),
        )
    except SlotTaken as exc:
        await message.answer(str(exc))
        await _slots(message, state, engine)
        return
    except FieldError as exc:
        await message.answer(str(exc))
        return
    except BookingError as exc:
        await message.answer(str(exc))
        if str(exc) == DAY_TAKEN:
            await state.set_state(DeskFlow.plate)
            await message.answer("Укажите другой госномер, например А123ВС178.")
        return
    except SiteNotConfigured as exc:
        await state.clear()
        await message.answer(str(exc), reply_markup=keyboard_for(StaffRole.admin))
        return
    await state.clear()
    zone = await asyncio.to_thread(load_zone, engine)
    await message.answer_photo(
        BufferedInputFile(qr_png(trip.pass_token), filename="pass.png"),
        caption=pass_caption(trip, zone) + "\n" + _NOTE,
    )
    await message.answer(_NOTE, reply_markup=keyboard_for(StaffRole.admin))


async def _ask_places(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DeskFlow.places)
    if isinstance(event, Message):
        await event.answer("Число мест.")
        return
    await reply(event, "Число мест.", menu_markup())


@router.message(DeskFlow.places, F.text)
async def desk_places(message: Message, state: FSMContext, engine: Engine) -> None:
    try:
        count = clean_places(message.text or "")
    except FieldError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(places=count)
    await _dates(message, state, engine)


async def _dates(event, state: FSMContext, engine: Engine) -> None:
    data = await state.get_data()
    try:
        days = await asyncio.to_thread(
            available_dates,
            engine,
            cargo=data.get("cargo"),
            vehicle=data.get("vehicle"),
            weight_kg=data.get("weight_kg"),
        )
    except SiteNotConfigured as exc:
        await state.clear()
        await reply(event, str(exc), keyboard_for(StaffRole.admin))
        return
    days = days[:WIZARD_DAYS]
    await state.set_state(DeskFlow.date)
    if not days:
        await reply(
            event,
            "Нет свободных окон. Откройте «Слоты» и нажмите «Сгенерировать 08:00–20:00».",
            menu_markup(),
        )
        return
    text = "Выберите дату."
    phrase = await _window(engine, data)
    if phrase:
        text = f"{phrase}\n{text}"
    await reply(event, text, dates_markup(days))


async def _gates(event, state: FSMContext, engine: Engine) -> None:
    data = await state.get_data()
    day = date.fromisoformat(data["date"])
    gates = await asyncio.to_thread(
        gates_with_slots,
        engine,
        day,
        cargo=data.get("cargo"),
        vehicle=data.get("vehicle"),
        weight_kg=data.get("weight_kg"),
    )
    await state.set_state(DeskFlow.gate)
    if not gates:
        await reply(event, "Свободных ворот нет. Вернитесь к дате.", menu_markup())
        return
    await reply(event, "Выберите ворота.", gates_markup(gates))


async def _slots(event, state: FSMContext, engine: Engine) -> None:
    data = await state.get_data()
    choices, zone = await _choices(engine, data)
    await state.set_state(DeskFlow.slot)
    if not choices:
        await reply(event, "Свободных окон нет. Выберите другие ворота.", menu_markup())
        return
    text = "Выберите окно."
    phrase = await _window(engine, data)
    if phrase:
        text = f"{phrase}\n{text}"
    await reply(event, text, slots_markup(choices, zone))


async def _selected(engine: Engine, data: dict):
    choices, _zone = await _choices(engine, data)
    stamp = data.get("slot_ts")
    return next((slot for slot in choices if int(slot.start.timestamp()) == stamp), None)


async def _choices(engine: Engine, data: dict):
    day = date.fromisoformat(data["date"])
    choices = await asyncio.to_thread(
        slots_for_gate,
        engine,
        day,
        int(data["gate_id"]),
        cargo=data.get("cargo"),
        vehicle=data.get("vehicle"),
        weight_kg=data.get("weight_kg"),
    )
    zone = await asyncio.to_thread(load_zone, engine)
    return choices, zone


async def _window(engine: Engine, data: dict) -> str:
    cargo = data.get("cargo")
    weight = data.get("weight_kg")
    if not cargo or not weight:
        return ""
    return await asyncio.to_thread(
        window_phrase, engine, cargo, int(weight), data.get("vehicle") or "heavy"
    )


def _desk_cargo_ok(vehicle: str, code: str) -> bool:
    allowed = LIGHT_CARGOS if vehicle == "light" else HEAVY_CARGOS
    return code in {item[0] for item in allowed}


async def _admin(query: CallbackQuery, engine: Engine) -> bool:
    user = query.from_user
    if user is None:
        await query.answer()
        return False
    person = await asyncio.to_thread(active_staff, engine, user.id)
    if person is None or person.role is not StaffRole.admin:
        await query.answer("Недостаточно прав.", show_alert=True)
        return False
    return True
