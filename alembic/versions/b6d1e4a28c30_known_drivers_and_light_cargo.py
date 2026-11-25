"""known drivers and light cargo norms

Revision ID: b6d1e4a28c30
Revises: e8c4b2a91f06
Create Date: 2026-11-25 20:50:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from dockbot.roster import LIGHT_NORMS, STAND_DRIVERS

revision: str = "b6d1e4a28c30"
down_revision: Union[str, Sequence[str], None] = "e8c4b2a91f06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "known_drivers",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("driver_name", sa.String(length=200), nullable=False),
        sa.Column("plate", sa.String(length=32), nullable=False),
        sa.UniqueConstraint("driver_name", name="uq_known_drivers_name"),
        sa.UniqueConstraint("plate", name="uq_known_drivers_plate"),
        sa.CheckConstraint(
            "char_length(btrim(driver_name)) > 0", name="ck_known_drivers_name"
        ),
        sa.CheckConstraint("char_length(btrim(plate)) > 0", name="ck_known_drivers_plate"),
    )
    drivers = sa.table(
        "known_drivers",
        sa.column("driver_name", sa.String),
        sa.column("plate", sa.String),
    )
    op.bulk_insert(
        drivers,
        [{"driver_name": name, "plate": plate} for name, plate in STAND_DRIVERS],
    )
    norms = sa.table(
        "cargo_norms",
        sa.column("code", sa.String),
        sa.column("label", sa.String),
        sa.column("minutes", sa.Integer),
    )
    op.bulk_insert(
        norms,
        [
            {"code": code, "label": label, "minutes": minutes}
            for code, label, minutes in LIGHT_NORMS
        ],
    )


def downgrade() -> None:
    codes = ", ".join(f"'{code}'" for code, _label, _minutes in LIGHT_NORMS)
    op.execute(f"DELETE FROM cargo_norms WHERE code IN ({codes})")
    op.drop_table("known_drivers")
