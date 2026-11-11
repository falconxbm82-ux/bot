"""Схема PostgreSQL для DockBot.

Длины строк — предел схемы из плана разработки, не норма ТЗ.
Статусы в базе: booked, confirmed, arrived, unloading, completed, late, cancelled.
"""

from __future__ import annotations

import enum
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


class Base(DeclarativeBase):
    pass


class StaffRole(str, enum.Enum):
    security = "security"
    warehouse = "warehouse"


class BookingStatus(str, enum.Enum):
    booked = "booked"
    confirmed = "confirmed"
    arrived = "arrived"
    unloading = "unloading"
    completed = "completed"
    late = "late"
    cancelled = "cancelled"


class StatusSource(str, enum.Enum):
    driver = "водитель"
    staff = "персонал"
    scheduler = "планировщик"


class NotificationKind(str, enum.Enum):
    h24 = "h24"
    h2 = "h2"
    m30 = "m30"
    late = "late"
    pass_issued = "pass"
    reschedule = "reschedule"


OCCUPYING_STATUSES: tuple[BookingStatus, ...] = (
    BookingStatus.booked,
    BookingStatus.confirmed,
    BookingStatus.arrived,
    BookingStatus.unloading,
)

TERMINAL_STATUSES: tuple[BookingStatus, ...] = (
    BookingStatus.completed,
    BookingStatus.late,
    BookingStatus.cancelled,
)

PLATE_MAX = 32
MAKE_MAX = 64
DRIVER_NAME_MAX = 200
CARGO_MAX = 200

STAFF_ROLE = Enum(
    StaffRole,
    name="staff_role",
    values_callable=_enum_values,
    native_enum=True,
)
BOOKING_STATUS = Enum(
    BookingStatus,
    name="booking_status",
    values_callable=_enum_values,
    native_enum=True,
)
STATUS_SOURCE = Enum(
    StatusSource,
    name="status_source",
    values_callable=_enum_values,
    native_enum=True,
)
NOTIFICATION_KIND = Enum(
    NotificationKind,
    name="notification_kind",
    values_callable=_enum_values,
    native_enum=True,
)


class StaffUser(Base):
    __tablename__ = "staff_users"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    role: Mapped[StaffRole] = mapped_column(STAFF_ROLE, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    pin_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(display_name)) > 0",
            name="ck_staff_users_display_name",
        ),
        CheckConstraint("char_length(pin_hash) > 0", name="ck_staff_users_pin_hash"),
    )


class Gate(Base):
    __tablename__ = "gates"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    __table_args__ = (
        CheckConstraint("char_length(btrim(code)) > 0", name="ck_gates_code"),
        CheckConstraint("char_length(btrim(name)) > 0", name="ck_gates_name"),
    )


class GateHour(Base):
    __tablename__ = "gate_hours"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    gate_id: Mapped[int] = mapped_column(
        ForeignKey("gates.id", ondelete="RESTRICT"), nullable=False
    )
    # ISO: 1 = понедельник … 7 = воскресенье.
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    slot_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "gate_id", "weekday", "start_time", name="uq_gate_hours_gate_weekday_start"
        ),
        CheckConstraint("weekday BETWEEN 1 AND 7", name="ck_gate_hours_weekday"),
        CheckConstraint("end_time > start_time", name="ck_gate_hours_time_order"),
        CheckConstraint("slot_minutes > 0", name="ck_gate_hours_slot_minutes"),
        Index("ix_gate_hours_gate_id", "gate_id"),
    )


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    gate_id: Mapped[int] = mapped_column(
        ForeignKey("gates.id", ondelete="RESTRICT"), nullable=False
    )
    slot_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    slot_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plate: Mapped[str] = mapped_column(String(PLATE_MAX), nullable=False)
    make: Mapped[str] = mapped_column(String(MAKE_MAX), nullable=False)
    driver_name: Mapped[str] = mapped_column(String(DRIVER_NAME_MAX), nullable=False)
    cargo_type: Mapped[str] = mapped_column(String(CARGO_MAX), nullable=False)
    driver_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    driver_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[BookingStatus] = mapped_column(BOOKING_STATUS, nullable=False)
    pass_token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    pass_pin: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unloading_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    late_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminded_24_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminded_2_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminded_30_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rescheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("slot_end > slot_start", name="ck_bookings_slot_order"),
        CheckConstraint("char_length(btrim(plate)) > 0", name="ck_bookings_plate"),
        CheckConstraint("char_length(btrim(make)) > 0", name="ck_bookings_make"),
        CheckConstraint(
            "char_length(btrim(driver_name)) > 0", name="ck_bookings_driver_name"
        ),
        CheckConstraint(
            "char_length(btrim(cargo_type)) > 0", name="ck_bookings_cargo_type"
        ),
        CheckConstraint("char_length(pass_token) > 0", name="ck_bookings_pass_token"),
        CheckConstraint("char_length(pass_pin) > 0", name="ck_bookings_pass_pin"),
        Index("ix_bookings_driver_slot", "driver_telegram_id", "slot_start"),
        Index("ix_bookings_slot_start", "slot_start"),
        Index(
            "uq_bookings_gate_slot_occupying",
            "gate_id",
            "slot_start",
            unique=True,
            postgresql_where=text(
                "status IN ('booked', 'confirmed', 'arrived', 'unloading')"
            ),
        ),
        Index(
            "uq_bookings_active_pass_pin",
            "pass_pin",
            unique=True,
            postgresql_where=text("status NOT IN ('completed', 'cancelled', 'late')"),
        ),
    )


class BookingStatusEvent(Base):
    __tablename__ = "booking_status_events"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False
    )
    from_status: Mapped[BookingStatus | None] = mapped_column(BOOKING_STATUS)
    to_status: Mapped[BookingStatus] = mapped_column(BOOKING_STATUS, nullable=False)
    source: Mapped[StatusSource] = mapped_column(STATUS_SOURCE, nullable=False)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_booking_status_events_booking_id", "booking_id"),)


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[NotificationKind] = mapped_column(NOTIFICATION_KIND, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_notification_log_booking_id", "booking_id"),)


class SiteSettings(Base):
    __tablename__ = "site_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    time_zone: Mapped[str] = mapped_column(String(64), nullable=False)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    cancel_window_hours: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_site_settings_singleton"),
        CheckConstraint("char_length(time_zone) > 0", name="ck_site_settings_time_zone"),
        CheckConstraint(
            "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
            name="ck_site_settings_latitude",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
            name="ck_site_settings_longitude",
        ),
        CheckConstraint(
            "cancel_window_hours > 0", name="ck_site_settings_cancel_window"
        ),
    )
