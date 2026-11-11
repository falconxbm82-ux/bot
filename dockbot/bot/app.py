"""Сборка диспетчера и long polling."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher

from dockbot.bot.driver import router as driver_router
from dockbot.config import Settings

log = logging.getLogger(__name__)


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(driver_router)
    return dispatcher


async def run_bot(settings: Settings) -> None:
    bot = Bot(token=settings.bot_token)
    dispatcher = build_dispatcher()
    try:
        me = await bot.get_me()
        log.info("polling Telegram as @%s", me.username)
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
