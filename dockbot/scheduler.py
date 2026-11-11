"""APScheduler в процессе бота. Задания лежат в PostgreSQL.

Сверка при старте и раз в минуту досылает то, что наступило за простой.
Повторная отправка уже принятого Telegram сообщения не делается.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from dockbot.booking import listen_booking_changed
from dockbot.config import Settings
from dockbot.models import Booking, BookingStatus, NotificationKind
from dockbot.reminders import (
    KINDS,
    LATE_AFTER,
    DeliveryBlocked,
    DeliveryFailed,
    Outbound,
    catch_up,
    deliver,
    due_time,
)

log = logging.getLogger(__name__)

JOB_TABLE = "apscheduler_jobs"
RECONCILE_ID = "dockbot-reconcile"
CATCHUP_ID = "dockbot-catchup"
_MISFIRE_GRACE = 7 * 24 * 3600


@dataclass
class _Runtime:
    engine: Engine
    sender: Outbound
    latitude: float
    longitude: float
    zone: ZoneInfo


_runtime: _Runtime | None = None
_scheduler: BackgroundScheduler | None = None
_stop_listening: object | None = None


class TelegramSender(Outbound):
    def __init__(self, bot: Bot, loop: asyncio.AbstractEventLoop) -> None:
        self._bot = bot
        self._loop = loop

    def send_reminder(
        self, chat_id: int, latitude: float, longitude: float, text: str
    ) -> None:
        self._run(self._reminder(chat_id, latitude, longitude, text))

    def send_late(self, chat_id: int, text: str) -> None:
        self._run(self._bot.send_message(chat_id, text))

    async def _reminder(
        self, chat_id: int, latitude: float, longitude: float, text: str
    ) -> None:
        await self._bot.send_message(chat_id, text)
        await self._bot.send_location(
            chat_id, latitude=latitude, longitude=longitude
        )

    def _run(self, coro: object) -> None:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        try:
            future.result(timeout=60)
        except TelegramForbiddenError:
            raise DeliveryBlocked() from None
        except Exception as exc:
            raise DeliveryFailed(type(exc).__name__) from None


def make_scheduler(engine: Engine) -> BackgroundScheduler:
    store = SQLAlchemyJobStore(engine=engine, tablename=JOB_TABLE)
    return BackgroundScheduler(
        jobstores={"default": store},
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": _MISFIRE_GRACE,
        },
        timezone=timezone.utc,
    )


def start_scheduler(settings: Settings, engine: Engine, bot: Bot) -> BackgroundScheduler:
    """Пустая геометка сюда не попадает: require_site отказывает до старта."""
    global _runtime, _scheduler, _stop_listening
    settings.require_site()
    assert settings.latitude is not None
    assert settings.longitude is not None
    assert settings.time_zone is not None
    _runtime = _Runtime(
        engine=engine,
        sender=TelegramSender(bot, asyncio.get_running_loop()),
        latitude=settings.latitude,
        longitude=settings.longitude,
        zone=ZoneInfo(settings.time_zone),
    )
    scheduler = make_scheduler(engine)
    _scheduler = scheduler
    _stop_listening = listen_booking_changed(_on_booking_changed)
    scheduler.add_job(
        reconcile,
        "interval",
        seconds=60,
        id=RECONCILE_ID,
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    scheduler.start()
    scheduler.add_job(
        reconcile,
        "date",
        run_date=datetime.now(timezone.utc),
        id=CATCHUP_ID,
        replace_existing=True,
    )
    log.info("reminders started")
    return scheduler


def shutdown_scheduler(scheduler: BackgroundScheduler) -> None:
    global _runtime, _scheduler, _stop_listening
    stop = _stop_listening
    _stop_listening = None
    _scheduler = None
    _runtime = None
    if callable(stop):
        stop()
    scheduler.shutdown(wait=False)


def reconcile() -> None:
    runtime = _runtime
    scheduler = _scheduler
    if runtime is None or scheduler is None:
        return
    connection = runtime.engine.connect()
    acquired = connection.execute(
        text("SELECT pg_try_advisory_lock(2041001, 1)")
    ).scalar()
    connection.commit()
    if not acquired:
        connection.close()
        return
    try:
        now = datetime.now(timezone.utc)
        sync_open_bookings(scheduler, runtime.engine, now)
        catch_up(
            runtime.engine,
            sender=runtime.sender,
            now=now,
            latitude=runtime.latitude,
            longitude=runtime.longitude,
            zone=runtime.zone,
        )
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(2041001, 1)"))
        connection.commit()
        connection.close()


def run_point(booking_id: int, kind: str) -> None:
    runtime = _runtime
    if runtime is None:
        return
    deliver(
        runtime.engine,
        booking_id,
        NotificationKind(kind),
        sender=runtime.sender,
        now=datetime.now(timezone.utc),
        latitude=runtime.latitude,
        longitude=runtime.longitude,
        zone=runtime.zone,
    )


def sync_open_bookings(
    scheduler: BackgroundScheduler, engine: Engine, now: datetime
) -> None:
    with Session(engine) as session:
        rows = session.scalars(
            select(Booking).where(
                Booking.status.in_(_OPEN_STATUSES),
                Booking.slot_start > now - LATE_AFTER,
            )
        ).all()
        wanted: set[str] = set()
        pending: list[tuple[int, NotificationKind, datetime]] = []
        for booking in rows:
            anchor = booking.rescheduled_at or booking.created_at
            for kind in KINDS:
                due = due_time(booking.slot_start, kind)
                column = {
                    NotificationKind.h24: booking.reminded_24_at,
                    NotificationKind.h2: booking.reminded_2_at,
                    NotificationKind.m30: booking.reminded_30_at,
                }.get(kind)
                if column is not None or due <= now or due <= anchor:
                    continue
                if kind is not NotificationKind.late and booking.status not in _OPEN_STATUSES:
                    continue
                wanted.add(_job_id(booking.id, kind))
                pending.append((booking.id, kind, due))
    for job in list(scheduler.get_jobs()):
        if _parse_job_id(job.id) is None:
            continue
        if job.id not in wanted:
            scheduler.remove_job(job.id)
    for booking_id, kind, due in pending:
        _add_point(scheduler, booking_id, kind, due)


def sync_one(
    scheduler: BackgroundScheduler, engine: Engine, booking_id: int, now: datetime
) -> None:
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if booking is None or booking.status not in _OPEN_STATUSES:
            _drop_booking_jobs(scheduler, booking_id)
            return
        anchor = booking.rescheduled_at or booking.created_at
        stamps = {
            NotificationKind.h24: booking.reminded_24_at,
            NotificationKind.h2: booking.reminded_2_at,
            NotificationKind.m30: booking.reminded_30_at,
            NotificationKind.late: None,
        }
        slot_start = booking.slot_start
        status = booking.status
    for kind in KINDS:
        due = due_time(slot_start, kind)
        job_id = _job_id(booking_id, kind)
        if (
            status in _OPEN_STATUSES
            and stamps[kind] is None
            and due > now
            and due > anchor
        ):
            _add_point(scheduler, booking_id, kind, due)
        elif scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)


def _on_booking_changed(booking_id: int) -> None:
    scheduler = _scheduler
    runtime = _runtime
    if scheduler is None or runtime is None:
        return
    sync_one(scheduler, runtime.engine, booking_id, datetime.now(timezone.utc))


def _add_point(
    scheduler: BackgroundScheduler,
    booking_id: int,
    kind: NotificationKind,
    due: datetime,
) -> None:
    scheduler.add_job(
        run_point,
        "date",
        run_date=due,
        args=[booking_id, kind.value],
        id=_job_id(booking_id, kind),
        replace_existing=True,
        misfire_grace_time=_MISFIRE_GRACE,
    )


def _drop_booking_jobs(scheduler: BackgroundScheduler, booking_id: int) -> None:
    for kind in KINDS:
        job_id = _job_id(booking_id, kind)
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)


def _job_id(booking_id: int, kind: NotificationKind) -> str:
    return f"dockbot-{booking_id}-{kind.value}"


def _parse_job_id(job_id: str) -> tuple[int, str] | None:
    prefix = "dockbot-"
    if not job_id.startswith(prefix):
        return None
    booking_part, separator, kind = job_id[len(prefix) :].rpartition("-")
    if separator != "-" or not booking_part.isdigit():
        return None
    if kind not in {item.value for item in KINDS}:
        return None
    return int(booking_part), kind


_OPEN_STATUSES = (BookingStatus.booked, BookingStatus.confirmed)
