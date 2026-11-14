"""admin role, closed slots, address and lateness

Revision ID: c4e8a1b09d22
Revises: 440959ff991a
Create Date: 2026-11-14 17:40:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a1b09d22"
down_revision: Union[str, Sequence[str], None] = "440959ff991a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE staff_role ADD VALUE IF NOT EXISTS 'admin'")
    op.add_column("site_settings", sa.Column("address", sa.String(length=300), nullable=True))
    op.add_column(
        "site_settings",
        sa.Column(
            "late_after_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("15"),
        ),
    )
    op.create_check_constraint(
        "ck_site_settings_late_after",
        "site_settings",
        "late_after_minutes > 0",
    )
    op.create_table(
        "closed_slots",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("gate_id", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["gate_id"], ["gates.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("gate_id", "starts_at", name="uq_closed_slots_gate_start"),
    )
    op.create_index(
        "ix_closed_slots_gate_start",
        "closed_slots",
        ["gate_id", "starts_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_closed_slots_gate_start", table_name="closed_slots")
    op.drop_table("closed_slots")
    op.drop_constraint("ck_site_settings_late_after", "site_settings", type_="check")
    op.drop_column("site_settings", "late_after_minutes")
    op.drop_column("site_settings", "address")
