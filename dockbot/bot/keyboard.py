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
DRIVERS = "Водители"
DAY_SUMMARY = "Сводка дня"

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


def keyboard_for(role: StaffRole | None) -> ReplyKeyboardMarkup:
    if role is StaffRole.security:
        return _rows((SCAN, ENTER_PIN), (SHIFT_TODAY, HELP))
    if role is StaffRole.warehouse:
        return _rows((MONITOR, WAITING), (ON_SITE, FINISHED), (HELP,))
    if role is StaffRole.admin:
        return _rows(
            (GATES, SLOTS),
            (STAFF_LIST, SETTINGS),
            (DRIVERS, DAY_SUMMARY),
            (HELP,),
        )
    return _rows(
        (BOOK, MY_BOOKINGS),
        (MY_PASS, CANCEL_TRIP),
        (MOVE_TRIP, STAFF_ENTRY),
        (HELP,),
    )


def button_labels(markup: ReplyKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


def help_text(role: StaffRole | None) -> str:
    if role is StaffRole.security:
        return (
            "Охрана\n"
            "Сканировать QR — фото пропуска.\n"
            "Ввести PIN — шесть цифр пропуска.\n"
            "Смена сегодня — машины этих суток.\n"
            "На пропуске есть результат сверки документа и фото. "
            "При расхождении нажмите Не пустить.\n"
            "Чтобы снова открыть меню водителя, отправьте /logout."
        )
    if role is StaffRole.warehouse:
        return (
            "Кладовщик\n"
            "Монитор смены — все машины суток.\n"
            "Ожидаются, На территории и Завершены — тот же список с фильтром.\n"
            "На карточке те же отметки, что у охраны.\n"
            "Кладовщик не сканирует QR и открывает машину из списка.\n"
            "Кладовщик не запрашивает фото документа.\n"
            "Очередь показывает, кто следующий, сколько ждёт и какой груз.\n"
            "Пауза ставится, если ворота заняты или груз не готов. С паузы можно продолжить.\n"
            "Расхождение, фото разгрузки и повреждение паллет пишутся в бронь.\n"
            "Разгружен закрывает рейс после сверки госномера, типа, веса и числа мест.\n"
            "Чтобы снова открыть меню водителя, отправьте /logout."
        )
    if role is StaffRole.admin:
        return (
            "Администратор\n"
            "Ворота — включить, выключить или добавить.\n"
            "Слоты — окно 08:00–20:00 и закрытие свободного слота.\n"
            "Сотрудники — PIN из 4 цифр и отзыв доступа.\n"
            "Сводка дня — статусы, опоздания, прибытия в срок и среднее время.\n"
            "Там же кнопка Записать без чата, если водитель сам не открыл бота.\n"
            "Водители — десять человек, которым открыта запись на разгрузку.\n"
            "Параметры — окно отмены, опоздание, зазор, норма груза, шаг веса и адрес."
        )
    return (
        "Водитель\n"
        "Записаться — новое окно разгрузки.\n"
        "Мои брони — список и карточка.\n"
        "Мой пропуск — QR и PIN, пока время слота не вышло.\n"
        "Отменить, Перенести и Заменить машину доступны, "
        "пока до слота больше окна отмены.\n"
        "Запись возможна только если ФИО и госномер есть в списке склада.\n"
        "Вес добавляет минуты к норме типа груза, потолок 240 мин. "
        "У малотоннажного время короче.\n"
        "После веса бот спрашивает число мест.\n"
        "После госномера и марки бот просит фото документа.\n"
        "При замене машины QR и PIN не меняются.\n"
        "Я сотрудник — PIN из 4 цифр."
    )
