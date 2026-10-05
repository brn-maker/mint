"""Add locked-principal tracking and referral dividend windows.

Revision ID: 0003_referral_locked_principal
Revises: 0002_withdrawal_network_settlement
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0003_referral_locked_principal"
down_revision = "0002_withdrawal_network_settlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "server_slots" in tables:
        columns = {
            column["name"] for column in inspect(bind).get_columns("server_slots")
        }
        if "principal_unlocked_at" not in columns:
            op.add_column(
                "server_slots",
                sa.Column("principal_unlocked_at", sa.DateTime(timezone=True), nullable=True),
            )
    if "referral_dividend_milestones" not in tables:
        op.create_table(
            "referral_dividend_milestones",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("referral_threshold", sa.Integer(), nullable=False),
            sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "user_id",
                "referral_threshold",
                name="uq_referral_dividend_milestone",
            ),
        )
        op.create_index(
            "ix_referral_dividend_milestones_user_id",
            "referral_dividend_milestones",
            ["user_id"],
        )
        op.create_index(
            "ix_referral_dividend_milestones_window",
            "referral_dividend_milestones",
            ["user_id", "starts_at", "ends_at"],
        )


def downgrade() -> None:
    raise RuntimeError(
        "Referral and principal unlock history is financial data; restore a database backup to roll back."
    )
