"""Экраны администратора: ворота, слоты, сотрудники, параметры, сводка."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy.engine import Engine

from dockbot.access import AccessDenied
from dockbot.adminboard import (
    CANCEL_CHOICES,
    LATE_CHOICES,
    AdminError,
    add_gate,
    admin_dates,
    create_staff,
    day_slots,
    day_summary,
    generate_window,
    list_gates,
    list_staff,
    read_settings,
    replace_pin,
    revoke_staff,
    set_address,
    set_cancel_minutes,
    set_late_minutes,
    set_point,
    set_slot_closed,
    toggle_gate,
)
from dockbot.bot.keyboard import keyboard_for
from dockbot.bot.ui import MENU, menu_markup
from dockbot.models import StaffRole
from dockbot.present import STATUS_LABELS, day_button

router = Router(name="admin")

RIGHTS = "Недостаточно прав."
ROLE_LABEL = {
    StaffRole.security: "Охрана",
    StaffRole.warehouse: "Кладовщик",
    StaffRole.admin: "Администратор",
}


class AdminFlow(StatesGroup):
    gate_code = State()
    staff_name = State()
    staff_telegram = State()
    address = State()
    point = State()


def _user_id(event: Message | CallbackQuery) -> int | None:
    user = event.from_user
    return None if user is None else user.id


async def open_gates(message: Message, state: FSMContext, engine: Engine) -> None:
    await state.clear()
    user_id = _user_id(message)
    if user_id is None:
        return
    try:
        gates = await asyncio.to_thread(list_gates, engine, user_id)
    except AccessDenied:
        await message.answer(RIGHTS)
        return
    await message.answer("Ворота.", reply_markup=_gates_markup(gates))


async def open_slots(message: Message, state: FSMContext, engine: Engine) -> None:
    await state.clear()
    await _show_slot_dates(message, engine)


async def open_staff(message: Message, state: FSMContext, engine: Engine) -> None:
    await state.clear()
    await _show_staff(message, engine)


async def open_settings(message: Message, state: FSMContext, engine: Engine) -> None:
    await state.clear()
    await _show_settings(message, engine)


async def open_day(message: Message, state: FSMContext, engine: Engine) -> None:
    await state.clear()
    await _show_day(message, engine)


@router.callback_query(F.data == "ad:gates")
async def gates_again(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    await state.clear()
    user_id = _user_id(query)
    if user_id is None:
        await query.answer()
        return
    try:
        gates = await asyncio.to_thread(list_gates, engine, user_id)
    except AccessDenied:
        await query.answer(RIGHTS, show_alert=True)
        return
    await _answer(query, "Ворота.", _gates_markup(gates))


@router.callback_query(F.data == "ad:gate-add")
async def ask_gate(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    if not await _admin(query, engine):
        return
    await state.set_state(AdminFlow.gate_code)
    await _answer(query, "Введите номер ворот числом.", menu_markup())


@router.message(AdminFlow.gate_code, F.text)
async def save_gate(message: Message, state: FSMContext, engine: Engine) -> None:
    user_id = _user_id(message)
    if user_id is None:
        return
    try:
        gate = await asyncio.to_thread(add_gate, engine, user_id, message.text or "")
    except AccessDenied:
        await state.clear()
        await message.answer(RIGHTS)
        return
    except AdminError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer(
        f"Ворота {gate.code} добавлены. Слоты появятся после «Сгенерировать 08:00–20:00».",
        reply_markup=keyboard_for(StaffRole.admin),
    )


@router.callback_query(F.data.startswith("ad:gate-toggle:"))
async def flip_gate(query: CallbackQuery, engine: Engine) -> None:
    user_id = _user_id(query)
    raw = (query.data or "").removeprefix("ad:gate-toggle:")
    if user_id is None or not raw.isdigit():
        await query.answer("Кнопка устарела", show_alert=True)
        return
    try:
        gate = await asyncio.to_thread(toggle_gate, engine, user_id, int(raw))
    except AccessDenied:
        await query.answer(RIGHTS, show_alert=True)
        return
    except AdminError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    state_text = "вкл" if gate.is_active else "выкл"
    gates = await asyncio.to_thread(list_gates, engine, user_id)
    await _answer(query, f"Ворота {gate.code} · {state_text}.", _gates_markup(gates))


@router.callback_query(F.data == "ad:slots")
async def slots_again(query: CallbackQuery, engine: Engine) -> None:
    await _show_slot_dates(query, engine)


@router.callback_query(F.data.startswith("ad:slot-day:"))
async def slot_gates(query: CallbackQuery, engine: Engine) -> None:
    user_id = _user_id(query)
    raw = (query.data or "").removeprefix("ad:slot-day:")
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    if user_id is None:
        await query.answer()
        return
    try:
        gates = await asyncio.to_thread(list_gates, engine, user_id)
    except AccessDenied:
        await query.answer(RIGHTS, show_alert=True)
        return
    active = [gate for gate in gates if gate.is_active]
    if not active:
        await _answer(query, "Нет включённых ворот. Откройте «Ворота».", menu_markup())
        return
    rows = [
        [
            InlineKeyboardButton(
                text=f"Ворота {gate.code}",
                callback_data=f"ad:slot-gate:{gate.id}:{day.isoformat()}",
            )
        ]
        for gate in active
    ]
    rows.append([InlineKeyboardButton(text="«« Назад", callback_data="ad:slots")])
    rows.append([InlineKeyboardButton(text="В меню", callback_data=MENU)])
    await _answer(
        query,
        f"Ворота на {day_button(day)}.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("ad:slot-gate:"))
async def slot_board(query: CallbackQuery, engine: Engine) -> None:
    parsed = _gate_day(query.data or "", "ad:slot-gate:")
    if parsed is None:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await _show_board(query, engine, *parsed)


@router.callback_query(F.data.startswith("ad:slot-gen:"))
async def slot_generate(query: CallbackQuery, engine: Engine) -> None:
    parsed = _gate_day(query.data or "", "ad:slot-gen:")
    user_id = _user_id(query)
    if parsed is None or user_id is None:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    gate_id, day = parsed
    try:
        count = await asyncio.to_thread(
            generate_window, engine, user_id, gate_id, day
        )
    except AccessDenied:
        await query.answer(RIGHTS, show_alert=True)
        return
    except AdminError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer(f"Окно 08:00–20:00, слотов: {count}")
    await _show_board(query, engine, gate_id, day, answered=True)


@router.callback_query(F.data.startswith("ad:slot-close:"))
async def slot_close(query: CallbackQuery, engine: Engine) -> None:
    await _flip_slot(query, engine, "ad:slot-close:", closed=True)


@router.callback_query(F.data.startswith("ad:slot-open:"))
async def slot_open(query: CallbackQuery, engine: Engine) -> None:
    await _flip_slot(query, engine, "ad:slot-open:", closed=False)


@router.callback_query(F.data == "ad:staff")
async def staff_again(query: CallbackQuery, engine: Engine) -> None:
    await _show_staff(query, engine)


@router.callback_query(F.data == "ad:staff-add")
async def ask_staff_role(query: CallbackQuery, engine: Engine) -> None:
    if not await _admin(query, engine):
        return
    rows = [
        [
            InlineKeyboardButton(
                text=ROLE_LABEL[role], callback_data=f"ad:staff-role:{role.value}"
            )
        ]
        for role in (StaffRole.security, StaffRole.warehouse, StaffRole.admin)
    ]
    rows.append([InlineKeyboardButton(text="В меню", callback_data=MENU)])
    await _answer(query, "Роль нового сотрудника.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("ad:staff-role:"))
async def ask_staff_name(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    if not await _admin(query, engine):
        return
    role = (query.data or "").removeprefix("ad:staff-role:")
    if role not in ROLE_LABEL:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    await state.set_state(AdminFlow.staff_name)
    await state.update_data(staff_role=role)
    await _answer(query, "Имя сотрудника одним сообщением.", menu_markup())


@router.message(AdminFlow.staff_name, F.text)
async def ask_staff_telegram(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым.")
        return
    await state.update_data(staff_name=name)
    await state.set_state(AdminFlow.staff_telegram)
    await message.answer("Введите Telegram ID числом. 0 — пока без привязки.")


@router.message(AdminFlow.staff_telegram, F.text)
async def save_staff(message: Message, state: FSMContext, engine: Engine) -> None:
    user_id = _user_id(message)
    raw = (message.text or "").strip()
    data = await state.get_data()
    if user_id is None or not raw.isdigit():
        await message.answer("Telegram ID — число. 0, если привязки ещё нет.")
        return
    member_id = int(raw)
    try:
        row, pin = await asyncio.to_thread(
            create_staff,
            engine,
            user_id,
            StaffRole(data["staff_role"]),
            None if member_id == 0 else member_id,
            data.get("staff_name", ""),
        )
    except AccessDenied:
        await state.clear()
        await message.answer(RIGHTS)
        return
    except AdminError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    bound = "привязан" if row.telegram_id else "не привязан"
    await message.answer(
        f"{ROLE_LABEL[row.role]} · {row.display_name} · {bound}\n"
        f"PIN один раз: {pin}\n"
        "Сохраните его. Повторно бот его не покажет.",
        reply_markup=keyboard_for(StaffRole.admin),
    )


@router.callback_query(F.data.startswith("ad:staff-open:"))
async def staff_card(query: CallbackQuery, engine: Engine) -> None:
    raw = (query.data or "").removeprefix("ad:staff-open:")
    if not raw.isdigit() or not await _admin(query, engine):
        return
    staff_id = int(raw)
    user_id = _user_id(query)
    if user_id is None:
        return
    people = await asyncio.to_thread(list_staff, engine, user_id)
    person = next((item for item in people if item.id == staff_id), None)
    if person is None:
        await query.answer("Кнопка устарела", show_alert=True)
        return
    bound = "привязан" if person.telegram_id else "не привязан"
    active = "доступ открыт" if person.is_active else "доступ отозван"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Новый PIN", callback_data=f"ad:pin:{person.id}")],
            [
                InlineKeyboardButton(
                    text="Отозвать доступ", callback_data=f"ad:revoke:{person.id}"
                )
            ],
            [InlineKeyboardButton(text="«« Назад", callback_data="ad:staff")],
            [InlineKeyboardButton(text="В меню", callback_data=MENU)],
        ]
    )
    await _answer(
        query,
        f"{ROLE_LABEL[person.role]} · {person.display_name} · {bound}\n{active}",
        markup,
    )


@router.callback_query(F.data.startswith("ad:pin:"))
async def staff_pin(query: CallbackQuery, engine: Engine) -> None:
    user_id = _user_id(query)
    raw = (query.data or "").removeprefix("ad:pin:")
    if user_id is None or not raw.isdigit():
        await query.answer("Кнопка устарела", show_alert=True)
        return
    try:
        pin = await asyncio.to_thread(replace_pin, engine, user_id, int(raw))
    except AccessDenied:
        await query.answer(RIGHTS, show_alert=True)
        return
    except AdminError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await _answer(
        query,
        f"Новый PIN один раз: {pin}\nСтарый PIN больше не работает.",
        menu_markup(),
    )


@router.callback_query(F.data.startswith("ad:revoke:"))
async def staff_revoke(query: CallbackQuery, engine: Engine) -> None:
    user_id = _user_id(query)
    raw = (query.data or "").removeprefix("ad:revoke:")
    if user_id is None or not raw.isdigit():
        await query.answer("Кнопка устарела", show_alert=True)
        return
    try:
        await asyncio.to_thread(revoke_staff, engine, user_id, int(raw))
    except AccessDenied:
        await query.answer(RIGHTS, show_alert=True)
        return
    except AdminError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await _answer(query, "Доступ отозван. Этот PIN больше не пускает.", menu_markup())


@router.callback_query(F.data == "ad:settings")
async def settings_again(query: CallbackQuery, engine: Engine) -> None:
    await _show_settings(query, engine)


@router.callback_query(F.data.startswith("ad:cancel-window:"))
async def save_cancel(query: CallbackQuery, engine: Engine) -> None:
    await _save_number(
        query, engine, "ad:cancel-window:", set_cancel_minutes, "Окно отмены сохранено."
    )


@router.callback_query(F.data.startswith("ad:late:"))
async def save_late(query: CallbackQuery, engine: Engine) -> None:
    await _save_number(query, engine, "ad:late:", set_late_minutes, "Порог опоздания сохранён.")


@router.callback_query(F.data == "ad:address")
async def ask_address(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    if not await _admin(query, engine):
        return
    await state.set_state(AdminFlow.address)
    await _answer(query, "Новый адрес склада одним сообщением.", menu_markup())


@router.message(AdminFlow.address, F.text)
async def save_address(message: Message, state: FSMContext, engine: Engine) -> None:
    user_id = _user_id(message)
    if user_id is None:
        return
    try:
        await asyncio.to_thread(set_address, engine, user_id, message.text or "")
    except AccessDenied:
        await state.clear()
        await message.answer(RIGHTS)
        return
    except AdminError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer("Адрес сохранён.", reply_markup=keyboard_for(StaffRole.admin))


@router.callback_query(F.data == "ad:point")
async def ask_point(query: CallbackQuery, state: FSMContext, engine: Engine) -> None:
    if not await _admin(query, engine):
        return
    await state.set_state(AdminFlow.point)
    markup = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить геопозицию", request_location=True)]],
        resize_keyboard=True,
    )
    await _answer(query, "Отправьте геопозицию склада один раз.")
    message = query.message
    if message is not None and hasattr(message, "answer"):
        await message.answer("Кнопка ниже запросит точку.", reply_markup=markup)


@router.message(AdminFlow.point, F.location)
async def save_point(message: Message, state: FSMContext, engine: Engine) -> None:
    user_id = _user_id(message)
    location = message.location
    if user_id is None or location is None:
        return
    try:
        await asyncio.to_thread(
            set_point, engine, user_id, location.latitude, location.longitude
        )
    except AccessDenied:
        await state.clear()
        await message.answer(RIGHTS, reply_markup=keyboard_for(None))
        return
    except AdminError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer(
        "Точка склада сохранена.", reply_markup=keyboard_for(StaffRole.admin)
    )


@router.callback_query(F.data == "ad:day")
async def day_again(query: CallbackQuery, engine: Engine) -> None:
    await _show_day(query, engine)


def _gates_markup(gates: list) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"Ворота {gate.code} · {'вкл' if gate.is_active else 'выкл'}",
                callback_data=f"ad:gate-toggle:{gate.id}",
            )
        ]
        for gate in gates
    ]
    rows.append(
        [InlineKeyboardButton(text="Добавить ворота", callback_data="ad:gate-add")]
    )
    rows.append([InlineKeyboardButton(text="В меню", callback_data=MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_slot_dates(event: Message | CallbackQuery, engine: Engine) -> None:
    user_id = _user_id(event)
    if user_id is None:
        return
    try:
        days = await asyncio.to_thread(admin_dates, engine, user_id)
    except AccessDenied:
        await _tell(event, RIGHTS)
        return
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for day in days:
        row.append(
            InlineKeyboardButton(
                text=day_button(day),
                callback_data=f"ad:slot-day:{day.isoformat()}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="В меню", callback_data=MENU)])
    await _tell(
        event,
        "Слоты на 14 дней. Сначала дата, затем ворота.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _show_board(
    query: CallbackQuery,
    engine: Engine,
    gate_id: int,
    day: date,
    *,
    answered: bool = False,
) -> None:
    user_id = _user_id(query)
    if user_id is None:
        return
    try:
        slots = await asyncio.to_thread(day_slots, engine, user_id, gate_id, day)
    except AccessDenied:
        await query.answer(RIGHTS, show_alert=True)
        return
    except Exception:
        await query.answer("Склад не настроен.", show_alert=True)
        return
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="Сгенерировать 08:00–20:00",
                callback_data=f"ad:slot-gen:{gate_id}:{day.isoformat()}",
            )
        ]
    ]
    lines = [f"Слоты {day_button(day)}."]
    for slot in slots:
        clock = slot.start.strftime("%H:%M")
        stamp = int(slot.start.timestamp())
        if slot.state == "busy":
            lines.append(f"{clock} занят")
            continue
        action = "Открыть" if slot.state == "closed" else "Закрыть"
        prefix = "ad:slot-open" if slot.state == "closed" else "ad:slot-close"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{action} {clock}",
                    callback_data=f"{prefix}:{gate_id}:{stamp}",
                )
            ]
        )
    if len(lines) == 1:
        lines.append("Слотов нет. Нажмите «Сгенерировать 08:00–20:00».")
    rows.append(
        [
            InlineKeyboardButton(
                text="«« Назад",
                callback_data=f"ad:slot-day:{day.isoformat()}",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="В меню", callback_data=MENU)])
    await _answer(
        query,
        "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=rows),
        answered=answered,
    )


async def _flip_slot(
    query: CallbackQuery, engine: Engine, prefix: str, *, closed: bool
) -> None:
    user_id = _user_id(query)
    rest = (query.data or "").removeprefix(prefix)
    parts = rest.split(":")
    if user_id is None or len(parts) != 2 or not all(part.isdigit() for part in parts):
        await query.answer("Кнопка устарела", show_alert=True)
        return
    gate_id = int(parts[0])
    moment = datetime.fromtimestamp(int(parts[1]), datetime.now().astimezone().tzinfo)
    try:
        state = await asyncio.to_thread(
            set_slot_closed, engine, user_id, gate_id, moment, closed=closed
        )
    except AccessDenied:
        await query.answer(RIGHTS, show_alert=True)
        return
    if state == "busy":
        await query.answer("Слот занят. Закрыть его нельзя.", show_alert=True)
        return
    day = moment.astimezone(moment.tzinfo).date()
    await _show_board(query, engine, gate_id, day)


async def _show_staff(event: Message | CallbackQuery, engine: Engine) -> None:
    user_id = _user_id(event)
    if user_id is None:
        return
    try:
        people = await asyncio.to_thread(list_staff, engine, user_id)
    except AccessDenied:
        await _tell(event, RIGHTS)
        return
    rows = [
        [InlineKeyboardButton(text="Добавить сотрудника", callback_data="ad:staff-add")]
    ]
    for person in people:
        bound = "привязан" if person.telegram_id else "не привязан"
        if not person.is_active:
            bound = "отозван"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{ROLE_LABEL[person.role]} · {person.display_name} · {bound}",
                    callback_data=f"ad:staff-open:{person.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="В меню", callback_data=MENU)])
    await _tell(event, "Сотрудники.", InlineKeyboardMarkup(inline_keyboard=rows))


async def _show_settings(event: Message | CallbackQuery, engine: Engine) -> None:
    user_id = _user_id(event)
    if user_id is None:
        return
    try:
        current = await asyncio.to_thread(read_settings, engine, user_id)
    except AccessDenied:
        await _tell(event, RIGHTS)
        return
    except AdminError as exc:
        await _tell(event, str(exc))
        return
    point = "не задана"
    if current.latitude is not None and current.longitude is not None:
        point = f"{current.latitude:.5f}, {current.longitude:.5f}"
    address = current.address or "не задан"
    text = (
        f"Окно отмены: {current.cancel_minutes} мин\n"
        f"Опоздание: {current.late_minutes} мин\n"
        f"Адрес: {address}\n"
        f"Точка: {point}\n"
        "Напоминания 24 ч, 2 ч и 30 мин не меняются кнопками."
    )
    rows = [
        [
            InlineKeyboardButton(text=f"{minutes} мин", callback_data=f"ad:cancel-window:{minutes}")
            for minutes in CANCEL_CHOICES[:2]
        ],
        [
            InlineKeyboardButton(text=f"{minutes} мин", callback_data=f"ad:cancel-window:{minutes}")
            for minutes in CANCEL_CHOICES[2:]
        ],
        [
            InlineKeyboardButton(text=f"опоздание {minutes}", callback_data=f"ad:late:{minutes}")
            for minutes in LATE_CHOICES[:2]
        ],
        [
            InlineKeyboardButton(text=f"опоздание {minutes}", callback_data=f"ad:late:{minutes}")
            for minutes in LATE_CHOICES[2:]
        ],
        [InlineKeyboardButton(text="Изменить адрес", callback_data="ad:address")],
        [InlineKeyboardButton(text="Отправить геопозицию", callback_data="ad:point")],
        [InlineKeyboardButton(text="В меню", callback_data=MENU)],
    ]
    await _tell(event, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def _show_day(event: Message | CallbackQuery, engine: Engine) -> None:
    user_id = _user_id(event)
    if user_id is None:
        return
    try:
        counts = await asyncio.to_thread(day_summary, engine, user_id)
    except AccessDenied:
        await _tell(event, RIGHTS)
        return
    except AdminError as exc:
        await _tell(event, str(exc))
        return
    lines = ["Сводка дня."]
    lines.extend(f"{STATUS_LABELS[status]}: {count}" for status, count in counts)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Обновить", callback_data="ad:day")],
            [InlineKeyboardButton(text="В меню", callback_data=MENU)],
        ]
    )
    await _tell(event, "\n".join(lines), markup)


async def _save_number(query, engine, prefix, saver, done: str) -> None:
    user_id = _user_id(query)
    raw = (query.data or "").removeprefix(prefix)
    if user_id is None or not raw.isdigit():
        await query.answer("Кнопка устарела", show_alert=True)
        return
    try:
        await asyncio.to_thread(saver, engine, user_id, int(raw))
    except AccessDenied:
        await query.answer(RIGHTS, show_alert=True)
        return
    except AdminError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer(done)
    await _show_settings(query, engine)


async def _admin(query: CallbackQuery, engine: Engine) -> bool:
    user_id = _user_id(query)
    if user_id is None:
        await query.answer()
        return False
    try:
        await asyncio.to_thread(list_gates, engine, user_id)
    except AccessDenied:
        await query.answer(RIGHTS, show_alert=True)
        return False
    return True


def _gate_day(data: str, prefix: str) -> tuple[int, date] | None:
    rest = data.removeprefix(prefix)
    gate_raw, _, day_raw = rest.partition(":")
    if not gate_raw.isdigit():
        return None
    try:
        return int(gate_raw), date.fromisoformat(day_raw)
    except ValueError:
        return None


async def _answer(
    query: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    answered: bool = False,
) -> None:
    if not answered:
        await query.answer()
    message = query.message
    if message is not None and hasattr(message, "answer"):
        await message.answer(text, reply_markup=markup)


async def _tell(
    event: Message | CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup)
        return
    await _answer(event, text, markup)
