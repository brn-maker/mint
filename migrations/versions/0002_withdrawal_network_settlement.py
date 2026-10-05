"""Add network-fee quotes and manual Stars withdrawal settlement fields."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0002_withdrawal_network_settlement"
down_revision = "0001_payment_ledger"
branch_labels = None
depends_on = None


COLUMNS = (
    sa.Column("quote_id", sa.String(36), nullable=True),
    sa.Column(
        "network_fee_estimate",
        sa.Numeric(24, 8),
        nullable=False,
        server_default="0",
    ),
    sa.Column(
        "native_network_fee_estimate",
        sa.Numeric(24, 8),
        nullable=False,
        server_default="0",
    ),
    sa.Column("network_fee_amount", sa.Numeric(24, 8), nullable=True),
    sa.Column("network_fee_currency", sa.String(8), nullable=True),
    sa.Column("ton_usd_rate", sa.Numeric(24, 8), nullable=True),
    sa.Column("settlement_currency", sa.String(8), nullable=True),
    sa.Column("settlement_amount", sa.Numeric(24, 8), nullable=True),
    sa.Column("settlement_rate", sa.Numeric(24, 8), nullable=True),
)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "withdrawal_requests" not in tables:
        return
    existing = {
        column["name"] for column in inspect(bind).get_columns("withdrawal_requests")
    }
    for column in COLUMNS:
        if column.name not in existing:
            op.add_column("withdrawal_requests", column)
    if "withdrawal_quotes" not in tables:
        op.create_table(
            "withdrawal_quotes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("currency", sa.String(8), nullable=False),
            sa.Column("gross_amount", sa.Numeric(24, 8), nullable=False),
            sa.Column("platform_fee", sa.Numeric(24, 8), nullable=False),
            sa.Column("network_fee_estimate", sa.Numeric(24, 8), nullable=False),
            sa.Column("native_network_fee_estimate", sa.Numeric(24, 8), nullable=False),
            sa.Column("network_fee_currency", sa.String(8), nullable=True),
            sa.Column("ton_usd_rate", sa.Numeric(24, 8), nullable=True),
            sa.Column("payout_amount", sa.Numeric(24, 8), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "withdrawal_id",
                sa.String(36),
                nullable=True,
                unique=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_withdrawal_quotes_user_id", "withdrawal_quotes", ["user_id"]
        )
    indexes = {
        (tuple(index.get("column_names") or ()), bool(index.get("unique")))
        for index in inspect(bind).get_indexes("withdrawal_requests")
    }
    if (("quote_id",), True) not in indexes and not any(
        tuple(constraint.get("column_names") or ()) == ("quote_id",)
        for constraint in inspect(bind).get_unique_constraints("withdrawal_requests")
    ):
        op.create_index(
            "uq_withdrawal_requests_quote_id",
            "withdrawal_requests",
            ["quote_id"],
            unique=True,
        )


def downgrade() -> None:
    raise RuntimeError(
        "Withdrawal settlement history is financial data; restore a database backup to roll back."
    )
