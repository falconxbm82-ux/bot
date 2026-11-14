"""Нижняя клавиатура. Три роли видны всегда.

Кнопки шага (дата, ворота, слот, карточка рейса) остаются в сообщении:
их набор зависит от рейса и в одну постоянную клавиатуру не входит.
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from dockbot.models import StaffRole

ROLE_DRIVER = "Водитель"
ROLE_GUARD = "Охрана"
ROLE_WAREHOUSE = "Кладовщик"
BOOK = "Забронировать"
MY_BOOKINGS = "Мои брони"
CHECK_PASS = "Проверить пропуск"
SHIFT_BOARD = "Экран смены"

_ROLES = (ROLE_DRIVER, ROLE_GUARD, ROLE_WAREHOUSE)


def persistent_keyboard(*actions: str) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=action) for action in actions]]
    rows.append([KeyboardButton(text=role) for role in _ROLES])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def keyboard_for(role: StaffRole | None) -> ReplyKeyboardMarkup:
    if role is StaffRole.security:
        return persistent_keyboard(CHECK_PASS)
    if role is StaffRole.warehouse:
        return persistent_keyboard(SHIFT_BOARD)
    return persistent_keyboard(BOOK, MY_BOOKINGS)


def button_labels(markup: ReplyKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]
