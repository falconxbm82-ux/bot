"""Нижняя клавиатура. Три роли видны всегда, рядом действия этой роли.

Кнопки шага (дата, ворота, слот, карточка) остаются в сообщении.
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from dockbot.models import StaffRole

ROLE_DRIVER = "Водитель"
ROLE_GUARD = "Охрана"
ROLE_WAREHOUSE = "Кладовщик"

BOOK = "Записаться"
MY_BOOKINGS = "Мои брони"
MY_PASS = "Мой пропуск"
DIRECTIONS = "Как проехать"
CANCEL_TRIP = "Отменить"
MOVE_TRIP = "Перенести"
HELP = "Помощь"

SCAN = "Сканировать QR"
ENTER_PIN = "Ввести PIN"
SHIFT_TODAY = "Смена сегодня"

MONITOR = "Монитор смены"
WAITING = "Ожидаются"
ON_SITE = "На территории"
FINISHED = "Завершены"

_ROLES = (ROLE_DRIVER, ROLE_GUARD, ROLE_WAREHOUSE)
CALLBACK_BOOK = "driver:book"
CALLBACK_LIST = "driver:my_bookings"
NAV_MENU = "nav:menu"


def _rows(*lines: tuple[str, ...]) -> ReplyKeyboardMarkup:
    keyboard = [[KeyboardButton(text=label) for label in line] for line in lines]
    keyboard.append([KeyboardButton(text=role) for role in _ROLES])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,
    )


def keyboard_for(role: StaffRole | None) -> ReplyKeyboardMarkup:
    if role is StaffRole.security:
        return _rows((SCAN, ENTER_PIN), (SHIFT_TODAY, HELP))
    if role is StaffRole.warehouse:
        return _rows((MONITOR, WAITING), (ON_SITE, FINISHED), (HELP,))
    return _rows(
        (BOOK, MY_BOOKINGS),
        (MY_PASS, DIRECTIONS),
        (CANCEL_TRIP, MOVE_TRIP),
        (HELP,),
    )


def button_labels(markup: ReplyKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


def help_text(role: StaffRole | None) -> str:
    if role is StaffRole.security:
        return (
            "Охрана\n"
            "«Сканировать QR» — фото пропуска.\n"
            "«Ввести PIN» — шесть цифр пропуска.\n"
            "«Смена сегодня» — машины этих суток.\n"
            "На карточке: «Отметить прибытие», «На воротах N», «Разгружен».\n"
            "«Водитель» снимает эту роль."
        )
    if role is StaffRole.warehouse:
        return (
            "Кладовщик\n"
            "«Монитор смены» — все машины суток.\n"
            "«Ожидаются», «На территории», «Завершены» — тот же список с фильтром.\n"
            "На карточке те же отметки, что у охраны.\n"
            "QR кладовщик не сканирует: машина берётся из списка.\n"
            "«Водитель» снимает эту роль."
        )
    return (
        "Водитель\n"
        "«Записаться» — новое окно разгрузки.\n"
        "«Мои брони» — список и карточка.\n"
        "«Мой пропуск» — QR и PIN.\n"
        "«Как проехать» — точка склада, если она задана.\n"
        "«Отменить» и «Перенести» — пока до слота больше окна отмены.\n"
        "«Охрана» и «Кладовщик» просят PIN персонала."
    )
