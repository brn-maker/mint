"""widen user id columns to bigint for large telegram user ids

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0004_bigint_user_ids"
down_revision = "0003_referral_locked_principal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # users.id is the primary key — alter it and every FK column that references it
    op.alter_column("users", "id",
                    existing_type=sa.Integer(),
                    type_=sa.BigInteger(),
                    existing_nullable=False)
    op.alter_column("users", "referred_by",
                    existing_type=sa.Integer(),
                    type_=sa.BigInteger(),
                    existing_nullable=True)

    op.alter_column("referral_pending", "user_id",
                    existing_type=sa.Integer(),
                    type_=sa.BigInteger(),
                    existing_nullable=False)

    op.alter_column("payment_intents", "user_id",
                    existing_type=sa.Integer(),
                    type_=sa.BigInteger(),
                    existing_nullable=False)

    op.alter_column("server_slots", "user_id",
                    existing_type=sa.Integer(),
                    type_=sa.BigInteger(),
                    existing_nullable=False)

    op.alter_column("referral_dividend_milestones", "user_id",
                    existing_type=sa.Integer(),
                    type_=sa.BigInteger(),
                    existing_nullable=False)

    op.alter_column("wallet_balances", "user_id",
                    existing_type=sa.Integer(),
                    type_=sa.BigInteger(),
                    existing_nullable=False)

    op.alter_column("ledger_entries", "user_id",
                    existing_type=sa.Integer(),
                    type_=sa.BigInteger(),
                    existing_nullable=True)

    op.alter_column("withdrawal_requests", "user_id",
                    existing_type=sa.Integer(),
                    type_=sa.BigInteger(),
                    existing_nullable=False)

    op.alter_column("withdrawal_quotes", "user_id",
                    existing_type=sa.Integer(),
                    type_=sa.BigInteger(),
                    existing_nullable=False)

    op.alter_column("financial_telemetry", "user_id",
                    existing_type=sa.Integer(),
                    type_=sa.BigInteger(),
                    existing_nullable=True)


def downgrade() -> None:
    op.alter_column("financial_telemetry", "user_id",
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=True)
    op.alter_column("withdrawal_quotes", "user_id",
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=False)
    op.alter_column("withdrawal_requests", "user_id",
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=False)
    op.alter_column("ledger_entries", "user_id",
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=True)
    op.alter_column("wallet_balances", "user_id",
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=False)
    op.alter_column("referral_dividend_milestones", "user_id",
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=False)
    op.alter_column("server_slots", "user_id",
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=False)
    op.alter_column("payment_intents", "user_id",
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=False)
    op.alter_column("referral_pending", "user_id",
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=False)
    op.alter_column("users", "referred_by",
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=True)
    op.alter_column("users", "id",
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=False)
