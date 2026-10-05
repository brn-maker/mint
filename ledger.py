from decimal import Decimal, ROUND_DOWN

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from config import AMOUNT_QUANTUM
from models import FinancialTelemetry, LedgerEntry, WalletBalance
from models import TreasuryBalance


class LedgerError(ValueError):
    pass


async def record_financial_event(
    db: AsyncSession,
    *,
    user_id: int | None,
    event_type: str,
    currency: str,
    gross_amount: Decimal,
    reference_type: str,
    reference_id: str,
    commissions_paid: Decimal = Decimal("0"),
    platform_fees_collected: Decimal = Decimal("0"),
    tx_hash: str | None = None,
) -> None:
    existing = await db.execute(
        select(FinancialTelemetry.id).where(
            FinancialTelemetry.reference_id == reference_id,
            FinancialTelemetry.reference_type == reference_type,
            FinancialTelemetry.event_type == event_type,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return
    db.add(
        FinancialTelemetry(
            user_id=user_id,
            event_type=event_type,
            currency=currency,
            gross_amount=_quantize(gross_amount),
            commissions_paid=commissions_paid,
            platform_fees_collected=platform_fees_collected,
            tx_hash=tx_hash,
            reference_type=reference_type,
            reference_id=reference_id,
        )
    )


async def _get_or_create_balance(
    db: AsyncSession, user_id: int, currency: str, *, for_update: bool = False
) -> WalletBalance:
    query = select(WalletBalance).where(
        WalletBalance.user_id == user_id, WalletBalance.currency == currency
    )
    if for_update and db.bind and db.bind.dialect.name != "sqlite":
        query = query.with_for_update()
    result = await db.execute(query)
    balance = result.scalar_one_or_none()
    if balance is None:
        dialect = db.bind.dialect.name if db.bind else ""
        values = {"user_id": user_id, "currency": currency, "withdrawable_amount": Decimal("0")}
        if dialect == "sqlite":
            insert = sqlite_insert(WalletBalance)
        elif dialect == "postgresql":
            insert = postgresql_insert(WalletBalance)
        else:
            raise RuntimeError(f"Unsupported database dialect: {dialect}")
        await db.execute(
            insert.values(**values).on_conflict_do_nothing(
                index_elements=["user_id", "currency"]
            )
        )
        balance_query = select(WalletBalance).where(
            WalletBalance.user_id == user_id, WalletBalance.currency == currency
        )
        if for_update and dialect != "sqlite":
            balance_query = balance_query.with_for_update()
        balance = (await db.execute(balance_query)).scalar_one()
    return balance


async def _get_or_create_treasury(
    db: AsyncSession, currency: str, *, for_update: bool = False
) -> TreasuryBalance:
    query = select(TreasuryBalance).where(TreasuryBalance.currency == currency)
    if for_update and db.bind and db.bind.dialect.name != "sqlite":
        query = query.with_for_update()
    treasury = (await db.execute(query)).scalar_one_or_none()
    if treasury is None:
        dialect = db.bind.dialect.name if db.bind else ""
        if dialect == "sqlite":
            insert = sqlite_insert(TreasuryBalance)
        elif dialect == "postgresql":
            insert = postgresql_insert(TreasuryBalance)
        else:
            raise RuntimeError(f"Unsupported database dialect: {dialect}")
        await db.execute(
            insert.values(currency=currency, amount=Decimal("0")).on_conflict_do_nothing(
                index_elements=["currency"]
            )
        )
        treasury_query = select(TreasuryBalance).where(
            TreasuryBalance.currency == currency
        )
        if for_update and dialect != "sqlite":
            treasury_query = treasury_query.with_for_update()
        treasury = (await db.execute(treasury_query)).scalar_one()
    return treasury


async def _existing_idempotent(
    db: AsyncSession, idempotency_key: str
) -> LedgerEntry | None:
    result = await db.execute(
        select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


def _quantize(amount: Decimal) -> Decimal:
    return Decimal(amount).quantize(AMOUNT_QUANTUM, rounding=ROUND_DOWN)


async def apply_ledger_delta(
    db: AsyncSession,
    *,
    user_id: int,
    currency: str,
    amount: Decimal,
    entry_type: str,
    reference_type: str,
    reference_id: str,
    idempotency_key: str,
    telemetry: dict[str, Decimal] | None = None,
    tx_hash: str | None = None,
) -> LedgerEntry:
    existing = await _existing_idempotent(db, idempotency_key)
    if existing is not None:
        return existing

    delta = _quantize(amount)
    if delta == 0:
        raise LedgerError("Ledger delta must be non-zero")

    balance = await _get_or_create_balance(db, user_id, currency, for_update=True)
    next_balance = balance.withdrawable_amount + delta
    if next_balance < 0:
        raise LedgerError("Insufficient withdrawable balance")

    balance.withdrawable_amount = next_balance
    entry = LedgerEntry(
        user_id=user_id,
        account_code=f"user:{user_id}",
        currency=currency,
        amount=delta,
        entry_type=entry_type,
        reference_type=reference_type,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
        balance_after=next_balance,
    )
    db.add(entry)

    telemetry = telemetry or {}
    db.add(
        FinancialTelemetry(
            user_id=user_id,
            event_type=entry_type,
            currency=currency,
            gross_amount=abs(delta),
            commissions_paid=telemetry.get("commissions_paid", Decimal("0")),
            platform_fees_collected=telemetry.get("platform_fees_collected", Decimal("0")),
            blockchain_gas_fee_ton=telemetry.get("blockchain_gas_fee_ton", Decimal("0")),
            tx_hash=tx_hash,
            reference_type=reference_type,
            reference_id=reference_id,
        )
    )
    return entry


async def apply_treasury_ledger_delta(
    db: AsyncSession,
    *,
    currency: str,
    amount: Decimal,
    entry_type: str,
    reference_type: str,
    reference_id: str,
    idempotency_key: str,
    telemetry: dict[str, Decimal] | None = None,
    allow_negative: bool = False,
) -> LedgerEntry:
    existing = await _existing_idempotent(db, idempotency_key)
    if existing is not None:
        return existing

    delta = _quantize(amount)
    if delta == 0:
        raise LedgerError("Ledger delta must be non-zero")

    treasury = await _get_or_create_treasury(db, currency, for_update=True)
    next_balance = treasury.amount + delta
    if next_balance < 0 and not allow_negative:
        raise LedgerError("Treasury balance cannot become negative")
    treasury.amount = next_balance

    entry = LedgerEntry(
        user_id=None,
        account_code=f"treasury:{currency}",
        currency=currency,
        amount=delta,
        entry_type=entry_type,
        reference_type=reference_type,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
        balance_after=next_balance,
    )
    db.add(entry)
    telemetry = telemetry or {}
    db.add(
        FinancialTelemetry(
            user_id=None,
            event_type=entry_type,
            currency=currency,
            gross_amount=abs(delta),
            platform_fees_collected=telemetry.get(
                "platform_fees_collected", Decimal("0")
            ),
            blockchain_gas_fee_ton=telemetry.get(
                "blockchain_gas_fee_ton", Decimal("0")
            ),
            reference_type=reference_type,
            reference_id=reference_id,
        )
    )
    return entry
