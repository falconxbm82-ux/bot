"""Доступ охраны и кладовщика.

PIN персонала хранится только как хэш. В журнал пишется отказ, не сам PIN.
Один Telegram ID — одна запись сотрудника.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import NoReturn

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from dockbot.config import STAFF_PIN_LENGTH
from dockbot.models import StaffRole, StaffUser

log = logging.getLogger(__name__)

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class AccessDenied(Exception):
    """Отказ без указания, какой PIN существует."""


@dataclass(frozen=True)
class StaffMember:
    id: int
    telegram_id: int
    role: StaffRole
    display_name: str


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        pin.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def unusable_pin_hash() -> str:
    """Запись только по Telegram ID: PIN для входа не выдаётся."""
    return hash_pin(secrets.token_urlsafe(32))


def verify_pin(pin: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            pin.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=32,
        )
        expected = bytes.fromhex(digest_hex)
    except Exception:
        return False
    return hmac.compare_digest(digest, expected)


def active_staff(engine: Engine, telegram_id: int) -> StaffMember | None:
    with Session(engine) as session:
        row = session.scalar(
            select(StaffUser).where(
                StaffUser.telegram_id == telegram_id,
                StaffUser.is_active.is_(True),
            )
        )
        if row is None or row.telegram_id is None:
            return None
        return _member(row)


def require_staff(engine: Engine, telegram_id: int) -> StaffMember:
    person = active_staff(engine, telegram_id)
    if person is None:
        log.info("staff access denied telegram_id=%s", telegram_id)
        raise AccessDenied()
    return person


def require_role(engine: Engine, telegram_id: int, role: StaffRole) -> StaffMember:
    """Охрана проверяет пропуск и ставит «Прибыл». Склад ведёт смену и ворота."""
    person = require_staff(engine, telegram_id)
    if person.role is not role:
        log.info("staff role denied telegram_id=%s", telegram_id)
        raise AccessDenied()
    return person


def enter_pin(engine: Engine, telegram_id: int, pin: str) -> StaffMember:
    """Первый успешный PIN записывает пустой Telegram ID и не затирает чужой."""
    secret = pin.strip()
    # На стенде PIN персонала — ровно 6 цифр. Отказ общий, без подсказки длины.
    if not (secret.isdigit() and len(secret) == STAFF_PIN_LENGTH):
        _deny(telegram_id)
    with Session(engine) as session:
        with session.begin():
            rows = session.scalars(
                select(StaffUser).where(StaffUser.is_active.is_(True))
            ).all()
            matched = [row for row in rows if verify_pin(secret, row.pin_hash)]
            if len(matched) != 1:
                _deny(telegram_id)
            row = session.get(StaffUser, matched[0].id, with_for_update=True)
            if row is None or not row.is_active:
                _deny(telegram_id)
            if row.telegram_id is None:
                taken = session.scalar(
                    select(StaffUser.id).where(StaffUser.telegram_id == telegram_id)
                )
                if taken is not None and taken != row.id:
                    _deny(telegram_id)
                row.telegram_id = telegram_id
                session.flush()
                return _member(row)
            if row.telegram_id != telegram_id:
                _deny(telegram_id)
            return _member(row)


def _deny(telegram_id: int) -> NoReturn:
    log.info("staff access denied telegram_id=%s", telegram_id)
    raise AccessDenied()


def _member(row: StaffUser) -> StaffMember:
    if row.telegram_id is None:
        raise AccessDenied()
    return StaffMember(
        id=row.id,
        telegram_id=row.telegram_id,
        role=row.role,
        display_name=row.display_name,
    )
