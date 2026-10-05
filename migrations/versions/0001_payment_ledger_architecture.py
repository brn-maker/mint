"""Add payment intents, append-only ledger and async withdrawals.

Revision ID: 0001_payment_ledger
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text
from decimal import Decimal

from database import Base
import models


revision = "0001_payment_ledger"
down_revision = None
branch_labels = None
depends_on = None


LEGACY_COLUMNS = {
    "users": (
        sa.Column("first_name", sa.String(128), nullable=True),
        sa.Column("language_code", sa.String(16), nullable=True),
        sa.Column(
            "referral_commission_paid",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    ),
    "server_slots": (
        sa.Column("payment_intent_id", sa.String(36), nullable=True),
        sa.Column("server_id", sa.String(32), nullable=True),
        sa.Column("pricing_tier_id", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    ),
    "financial_telemetry": (
        sa.Column("reference_type", sa.String(32), nullable=True),
        sa.Column("reference_id", sa.String(64), nullable=True),
    ),
    "ledger_entries": (
        sa.Column("account_code", sa.String(64), nullable=True),
    ),
    "withdrawal_requests": (
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
    ),
}


def _add_legacy_columns(bind, existing_tables: set[str]) -> None:
    inspector = inspect(bind)
    for table_name, columns in LEGACY_COLUMNS.items():
        if table_name not in existing_tables:
            continue
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        for column in columns:
            if column.name not in existing_columns:
                op.add_column(table_name, column)


def _make_nullable(bind, table_name: str, column_name: str) -> None:
    if table_name not in inspect(bind).get_table_names():
        return
    column = next(
        (
            item
            for item in inspect(bind).get_columns(table_name)
            if item["name"] == column_name
        ),
        None,
    )
    if column is None or column["nullable"]:
        return
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table_name) as batch:
            batch.alter_column(
                column_name,
                existing_type=column["type"],
                nullable=True,
            )
    else:
        op.alter_column(
            table_name,
            column_name,
            existing_type=column["type"],
            nullable=True,
        )


def _ensure_unique_indexes(bind, existing_tables: set[str]) -> None:
    inspector = inspect(bind)
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing_constraints = {
            tuple(constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(table.name)
        }
        existing_indexes = {
            (tuple(index.get("column_names") or ()), bool(index.get("unique")))
            for index in inspector.get_indexes(table.name)
        }
        for constraint in table.constraints:
            if not isinstance(constraint, sa.UniqueConstraint):
                continue
            columns = tuple(column.name for column in constraint.columns)
            if columns in existing_constraints or (columns, True) in existing_indexes:
                continue
            index_name = constraint.name or f"uq_{table.name}_{'_'.join(columns)}"
            op.create_index(index_name, table.name, list(columns), unique=True)


def _backfill_ledger_opening_balances(bind) -> None:
    connection = bind
    connection.execute(
        text(
            "UPDATE ledger_entries "
            "SET account_code = 'user:' || CAST(user_id AS VARCHAR) "
            "WHERE account_code IS NULL AND user_id IS NOT NULL"
        )
    )
    connection.execute(
        text(
            "UPDATE ledger_entries SET account_code = 'legacy:unassigned' "
            "WHERE account_code IS NULL"
        )
    )
    wallet_rows = connection.execute(
        text(
            "SELECT user_id, currency, withdrawable_amount "
            "FROM wallet_balances ORDER BY user_id, currency"
        )
    ).all()
    for user_id, currency, balance in wallet_rows:
        existing = connection.execute(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries "
                "WHERE account_code = :account AND currency = :currency"
            ),
            {"account": f"user:{user_id}", "currency": currency},
        ).scalar_one()
        difference = Decimal(str(balance)) - Decimal(str(existing))
        if difference == 0:
            continue
        idempotency_key = f"migration:opening:user:{user_id}:{currency}"
        found = connection.execute(
            text("SELECT 1 FROM ledger_entries WHERE idempotency_key = :key"),
            {"key": idempotency_key},
        ).first()
        if found:
            continue
        connection.execute(
            text(
                "INSERT INTO ledger_entries "
                "(user_id, account_code, currency, amount, entry_type, reference_type, "
                "reference_id, idempotency_key, balance_after, created_at) "
                "VALUES (:user_id, :account, :currency, :amount, 'OPENING_BALANCE', "
                "'migration', :reference_id, :key, :balance, CURRENT_TIMESTAMP)"
            ),
            {
                "user_id": user_id,
                "account": f"user:{user_id}",
                "currency": currency,
                "amount": str(difference),
                "reference_id": f"{user_id}:{currency}",
                "key": idempotency_key,
                "balance": str(balance),
            },
        )
    treasury_rows = connection.execute(
        text("SELECT currency, amount FROM treasury_balances ORDER BY currency")
    )
    for currency, balance in treasury_rows:
        account_code = f"treasury:{currency}"
        existing = connection.execute(
            text(
                "SELECT COALESCE(SUM(amount), 0) FROM ledger_entries "
                "WHERE account_code = :account AND currency = :currency"
            ),
            {"account": account_code, "currency": currency},
        ).scalar_one()
        difference = Decimal(str(balance)) - Decimal(str(existing))
        if difference == 0:
            continue
        idempotency_key = f"migration:opening:treasury:{currency}"
        found = connection.execute(
            text("SELECT 1 FROM ledger_entries WHERE idempotency_key = :key"),
            {"key": idempotency_key},
        ).first()
        if found:
            continue
        connection.execute(
            text(
                "INSERT INTO ledger_entries "
                "(user_id, account_code, currency, amount, entry_type, reference_type, "
                "reference_id, idempotency_key, balance_after, created_at) "
                "VALUES (NULL, :account, :currency, :amount, 'OPENING_BALANCE', "
                "'migration', :reference_id, :key, :balance, CURRENT_TIMESTAMP)"
            ),
            {
                "account": account_code,
                "currency": currency,
                "amount": str(difference),
                "reference_id": f"treasury:{currency}",
                "key": idempotency_key,
                "balance": str(balance),
            },
        )
    connection.execute(
        text(
            "UPDATE withdrawal_requests SET status = 'queued' "
            "WHERE status IN ('approved', 'requested')"
        )
    )
    connection.execute(
        text(
            "UPDATE withdrawal_requests SET status = 'processing' "
            "WHERE status = 'broadcasting'"
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(inspect(bind).get_table_names())
    _add_legacy_columns(bind, existing_tables)
    _make_nullable(bind, "ledger_entries", "user_id")
    _make_nullable(bind, "financial_telemetry", "user_id")
    Base.metadata.create_all(bind=bind, checkfirst=True)
    _ensure_unique_indexes(bind, existing_tables)
    _backfill_ledger_opening_balances(bind)
    for index in sorted(
        (index for table in Base.metadata.sorted_tables for index in table.indexes),
        key=lambda item: item.name or "",
    ):
        index.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    raise RuntimeError(
        "This financial baseline migration is intentionally irreversible; restore a database backup to roll back."
    )
