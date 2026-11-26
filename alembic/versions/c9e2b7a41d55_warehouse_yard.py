"""warehouse queue, pause, fact, unload photo, close check

Revision ID: c9e2b7a41d55
Revises: b6d1e4a28c30
Create Date: 2026-11-26 08:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9e2b7a41d55"
down_revision: Union[str, Sequence[str], None] = "b6d1e4a28c30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("place_count", sa.Integer(), nullable=True))
    op.add_column("bookings", sa.Column("pause_reason", sa.String(length=16), nullable=True))
    op.add_column("bookings", sa.Column("fact_kind", sa.String(length=16), nullable=True))
    op.add_column("bookings", sa.Column("fact_value", sa.String(length=200), nullable=True))
    op.add_column("bookings", sa.Column("unload_photo", sa.String(length=64), nullable=True))
    op.add_column(
        "bookings",
        sa.Column(
            "pallet_damage",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint(
        "ck_bookings_place_count",
        "bookings",
        "place_count IS NULL OR (place_count >= 1 AND place_count <= 500)",
    )
    op.create_check_constraint(
        "ck_bookings_pause_reason",
        "bookings",
        "pause_reason IS NULL OR pause_reason IN ('gate', 'cargo')",
    )
    op.create_check_constraint(
        "ck_bookings_fact_kind",
        "bookings",
        "fact_kind IS NULL OR fact_kind IN ('weight', 'cargo', 'places')",
    )
    op.create_check_constraint(
        "ck_bookings_pallet_damage",
        "bookings",
        "NOT pallet_damage OR unload_photo IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_bookings_pallet_damage", "bookings", type_="check")
    op.drop_constraint("ck_bookings_fact_kind", "bookings", type_="check")
    op.drop_constraint("ck_bookings_pause_reason", "bookings", type_="check")
    op.drop_constraint("ck_bookings_place_count", "bookings", type_="check")
    op.drop_column("bookings", "pallet_damage")
    op.drop_column("bookings", "unload_photo")
    op.drop_column("bookings", "fact_value")
    op.drop_column("bookings", "fact_kind")
    op.drop_column("bookings", "pause_reason")
    op.drop_column("bookings", "place_count")
