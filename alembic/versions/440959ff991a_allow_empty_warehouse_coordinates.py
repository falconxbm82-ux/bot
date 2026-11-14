"""allow empty warehouse coordinates

Revision ID: 440959ff991a
Revises: 8c7b2f2aa7db
Create Date: 2026-11-11 21:45:15.266883

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '440959ff991a'
down_revision: Union[str, Sequence[str], None] = '8c7b2f2aa7db'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "site_settings",
        "latitude",
        existing_type=sa.NUMERIC(precision=9, scale=6),
        nullable=True,
    )
    op.alter_column(
        "site_settings",
        "longitude",
        existing_type=sa.NUMERIC(precision=9, scale=6),
        nullable=True,
    )
    op.drop_constraint("ck_site_settings_latitude", "site_settings", type_="check")
    op.drop_constraint("ck_site_settings_longitude", "site_settings", type_="check")
    op.create_check_constraint(
        "ck_site_settings_latitude",
        "site_settings",
        "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
    )
    op.create_check_constraint(
        "ck_site_settings_longitude",
        "site_settings",
        "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_site_settings_latitude", "site_settings", type_="check")
    op.drop_constraint("ck_site_settings_longitude", "site_settings", type_="check")
    op.create_check_constraint(
        "ck_site_settings_latitude",
        "site_settings",
        "latitude >= -90 AND latitude <= 90",
    )
    op.create_check_constraint(
        "ck_site_settings_longitude",
        "site_settings",
        "longitude >= -180 AND longitude <= 180",
    )
    op.alter_column(
        "site_settings",
        "longitude",
        existing_type=sa.NUMERIC(precision=9, scale=6),
        nullable=False,
    )
    op.alter_column(
        "site_settings",
        "latitude",
        existing_type=sa.NUMERIC(precision=9, scale=6),
        nullable=False,
    )
