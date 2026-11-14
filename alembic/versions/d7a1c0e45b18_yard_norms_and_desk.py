"""cargo norms, buffer, vehicle, urgency, departure

Revision ID: d7a1c0e45b18
Revises: c4e8a1b09d22
Create Date: 2026-11-14 18:20:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7a1c0e45b18"
down_revision: Union[str, Sequence[str], None] = "c4e8a1b09d22"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NORMS = (
    ("pallets", "Паллеты", 60),
    ("bulk", "Навал", 60),
    ("liquid", "Жидкость", 60),
    ("reefer", "Рефрижератор", 60),
    ("other", "Другое", 60),
)


def upgrade() -> None:
    op.add_column(
        "gates",
        sa.Column(
            "light_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "site_settings",
        sa.Column(
            "buffer_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_site_settings_buffer", "site_settings", "buffer_minutes >= 0"
    )
    op.alter_column("bookings", "driver_telegram_id", existing_type=sa.BigInteger(), nullable=True)
    op.alter_column("bookings", "driver_chat_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column(
        "bookings",
        sa.Column(
            "vehicle_kind",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'heavy'"),
        ),
    )
    op.add_column(
        "bookings",
        sa.Column(
            "urgent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "bookings",
        sa.Column("departed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_bookings_vehicle_kind",
        "bookings",
        "vehicle_kind IN ('light', 'heavy')",
    )
    op.add_column(
        "booking_status_events",
        sa.Column("note", sa.String(length=40), nullable=True),
    )
    op.create_table(
        "cargo_norms",
        sa.Column("code", sa.String(length=32), primary_key=True),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.UniqueConstraint("label", name="uq_cargo_norms_label"),
        sa.CheckConstraint("char_length(btrim(code)) > 0", name="ck_cargo_norms_code"),
        sa.CheckConstraint("char_length(btrim(label)) > 0", name="ck_cargo_norms_label"),
        sa.CheckConstraint("minutes > 0", name="ck_cargo_norms_minutes"),
    )
    norms = sa.table(
        "cargo_norms",
        sa.column("code", sa.String),
        sa.column("label", sa.String),
        sa.column("minutes", sa.Integer),
    )
    op.bulk_insert(
        norms,
        [{"code": code, "label": label, "minutes": minutes} for code, label, minutes in _NORMS],
    )


def downgrade() -> None:
    op.drop_table("cargo_norms")
    op.drop_column("booking_status_events", "note")
    op.drop_constraint("ck_bookings_vehicle_kind", "bookings", type_="check")
    op.drop_column("bookings", "departed_at")
    op.drop_column("bookings", "urgent")
    op.drop_column("bookings", "vehicle_kind")
    op.alter_column("bookings", "driver_chat_id", existing_type=sa.BigInteger(), nullable=False)
    op.alter_column(
        "bookings", "driver_telegram_id", existing_type=sa.BigInteger(), nullable=False
    )
    op.drop_constraint("ck_site_settings_buffer", "site_settings", type_="check")
    op.drop_column("site_settings", "buffer_minutes")
    op.drop_column("gates", "light_only")
