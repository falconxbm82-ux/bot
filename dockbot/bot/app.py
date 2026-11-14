"""Сборка диспетчера и long polling."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject
from sqlalchemy.engine import Engine

from dockbot.bot.admin import router as admin_router
from dockbot.bot.desk import router as desk_router
from dockbot.bot.driver import router as driver_router
from dockbot.bot.fallback import router as fallback_router
from dockbot.bot.mine import router as mine_router
from dockbot.bot.roles import router as roles_router
from dockbot.bot.staff import router as staff_router
from dockbot.bot.wizard import router as wizard_router
from dockbot.config import ConfigError, Settings
from dockbot.scheduler import shutdown_scheduler, start_scheduler

log = logging.getLogger(__name__)


class EngineMiddleware:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict], Awaitable[object]],
        event: TelegramObject,
        data: dict,
    ) -> object:
        data["engine"] = self.engine
        return await handler(event, data)


def build_dispatcher(engine: Engine) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.update.middleware(EngineMiddleware(engine))
    dispatcher.include_router(roles_router)
    dispatcher.include_router(driver_router)
    dispatcher.include_router(staff_router)
    dispatcher.include_router(wizard_router)
    dispatcher.include_router(mine_router)
    dispatcher.include_router(admin_router)
    dispatcher.include_router(desk_router)
    dispatcher.include_router(fallback_router)
    return dispatcher


async def run_bot(settings: Settings, engine: Engine) -> None:
    bot = Bot(token=settings.bot_token)
    dispatcher = build_dispatcher(engine)
    scheduler = None
    try:
        scheduler = _start_reminders(settings, engine, bot)
        me = await bot.get_me()
        log.info("polling Telegram as @%s", me.username)
        await dispatcher.start_polling(bot)
    finally:
        if scheduler is not None:
            shutdown_scheduler(scheduler)
        await bot.session.close()


def _start_reminders(settings: Settings, engine: Engine, bot: Bot):
    if not settings.site_is_complete:
        log.error(
            "reminders are not started: warehouse coordinates, timezone, "
            "or cancel window is missing"
        )
        return None
    try:
        return start_scheduler(settings, engine, bot)
    except ConfigError as exc:
        log.error("%s", exc)
        return None
    except Exception:
        log.error("reminders are not started")
        return None
