"""weight step, booking weight, document check

Revision ID: e8c4b2a91f06
Revises: d7a1c0e45b18
Create Date: 2026-11-14 18:40:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8c4b2a91f06"
down_revision: Union[str, Sequence[str], None] = "d7a1c0e45b18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "site_settings",
        sa.Column(
            "weight_step_kg",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("5000"),
        ),
    )
    op.create_check_constraint(
        "ck_site_settings_weight_step",
        "site_settings",
        "weight_step_kg > 0 AND weight_step_kg <= 20000",
    )
    op.add_column("bookings", sa.Column("weight_kg", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_bookings_weight",
        "bookings",
        "weight_kg IS NULL OR (weight_kg >= 1000 AND weight_kg <= 40000)",
    )
    op.add_column("bookings", sa.Column("doc_file_id", sa.String(length=256), nullable=True))
    op.add_column("bookings", sa.Column("doc_name", sa.String(length=64), nullable=True))
    op.add_column("bookings", sa.Column("doc_result", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        "ck_bookings_doc_result",
        "bookings",
        "doc_result IS NULL OR doc_result IN ('match', 'mismatch')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_bookings_doc_result", "bookings", type_="check")
    op.drop_column("bookings", "doc_result")
    op.drop_column("bookings", "doc_name")
    op.drop_column("bookings", "doc_file_id")
    op.drop_constraint("ck_bookings_weight", "bookings", type_="check")
    op.drop_column("bookings", "weight_kg")
    op.drop_constraint("ck_site_settings_weight_step", "site_settings", type_="check")
    op.drop_column("site_settings", "weight_step_kg")
