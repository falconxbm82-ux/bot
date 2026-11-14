"""Нижняя клавиатура роли. На время мастера её снимают и возвращают в меню."""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

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
STAFF_ENTRY = "Я сотрудник"

SCAN = "Сканировать QR"
ENTER_PIN = "Ввести PIN"
SHIFT_TODAY = "Смена сегодня"

MONITOR = "Монитор смены"
WAITING = "Ожидаются"
ON_SITE = "На территории"
FINISHED = "Завершены"

GATES = "Ворота"
SLOTS = "Слоты"
STAFF_LIST = "Сотрудники"
SETTINGS = "Параметры"
DAY_SUMMARY = "Сводка дня"
SEND_POINT = "Отправить геопозицию"

CALLBACK_BOOK = "bk:book"
CALLBACK_LIST = "my:list"
NAV_MENU = "nav:menu"


def hide_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def _rows(*lines: tuple[str, ...]) -> ReplyKeyboardMarkup:
    keyboard = [[KeyboardButton(text=label) for label in line] for line in lines]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        is_persistent=True,
    )


def location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=SEND_POINT, request_location=True)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def keyboard_for(role: StaffRole | None) -> ReplyKeyboardMarkup:
    if role is StaffRole.security:
        return _rows((SCAN, ENTER_PIN), (SHIFT_TODAY, HELP))
    if role is StaffRole.warehouse:
        return _rows((MONITOR, WAITING), (ON_SITE, FINISHED), (HELP,))
    if role is StaffRole.admin:
        return _rows((GATES, SLOTS), (STAFF_LIST, SETTINGS), (DAY_SUMMARY, HELP))
    return _rows(
        (BOOK, MY_BOOKINGS),
        (MY_PASS, DIRECTIONS),
        (CANCEL_TRIP, MOVE_TRIP),
        (STAFF_ENTRY, HELP),
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
            "Вернуться водителем: /logout."
        )
    if role is StaffRole.warehouse:
        return (
            "Кладовщик\n"
            "«Монитор смены» — все машины суток.\n"
            "«Ожидаются», «На территории», «Завершены» — тот же список с фильтром.\n"
            "На карточке те же отметки, что у охраны.\n"
            "QR кладовщик не сканирует: машина берётся из списка.\n"
            "Вернуться водителем: /logout."
        )
    if role is StaffRole.admin:
        return (
            "Администратор\n"
            "«Ворота» — включить, выключить, добавить.\n"
            "«Слоты» — окно 08:00–20:00, закрыть свободный слот.\n"
            "«Сотрудники» — PIN на 4 цифры, отзыв доступа.\n"
            "«Сводка дня» — статусы, опоздания, прибытия в срок и среднее время.\n"
            "Там же «Записать без чата», если водитель сам не открыл бота.\n"
            "«Параметры» — окно отмены, опоздание, зазор, норматив груза, адрес."
        )
    return (
        "Водитель\n"
        "«Записаться» — новое окно разгрузки.\n"
        "«Мои брони» — список и карточка.\n"
        "«Мой пропуск» — QR и PIN.\n"
        "«Как проехать» — адрес и точка склада.\n"
        "«Отменить», «Перенести» и «Заменить машину» — пока до слота больше окна отмены.\n"
        "QR и PIN при замене не меняются.\n"
        "«Я сотрудник» — PIN из 4 цифр."
    )
