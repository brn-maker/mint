from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    wallet_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    referred_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    referral_code: Mapped[str] = mapped_column(String(6), unique=True, index=True)
    accumulated_dividends: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), default=Decimal("0"), nullable=False
    )
    referral_commission_paid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    referrer: Mapped["User | None"] = relationship(
        remote_side="User.id", back_populates="referrals"
    )
    referrals: Mapped[list["User"]] = relationship(back_populates="referrer")
    slots: Mapped[list["ServerSlot"]] = relationship(back_populates="user")
    balances: Mapped[list["WalletBalance"]] = relationship(back_populates="user")
    ledger_entries: Mapped[list["LedgerEntry"]] = relationship(back_populates="user")
    payment_intents: Mapped[list["PaymentIntent"]] = relationship(back_populates="user")
    withdrawal_requests: Mapped[list["WithdrawalRequest"]] = relationship(back_populates="user")


class ReferralPending(Base):
    __tablename__ = "referral_pending"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    referral_code: Mapped[str] = mapped_column(String(6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PaymentIntent(Base):
    __tablename__ = "payment_intents"
    __table_args__ = (
        UniqueConstraint("provider", "external_payment_id", name="uq_payment_provider_external"),
        UniqueConstraint("chain", "tx_hash", "tx_log_index", name="uq_payment_chain_tx"),
        Index("ix_payment_intents_status_expires", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    server_id: Mapped[str] = mapped_column(String(32))
    pricing_tier_id: Mapped[str] = mapped_column(String(32))
    server_pool: Mapped[str] = mapped_column(String(64))
    payment_method: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(8))
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    ton_usd_rate: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    quote_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    provider: Mapped[str] = mapped_column(String(16))
    external_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deposit_memo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invoice_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    chain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tx_log_index: Mapped[int | None] = mapped_column(nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    user: Mapped[User] = relationship(back_populates="payment_intents")
    slot: Mapped["ServerSlot | None"] = relationship(back_populates="payment_intent")


class ServerSlot(Base):
    __tablename__ = "server_slots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    payment_intent_id: Mapped[str | None] = mapped_column(
        ForeignKey("payment_intents.id"), unique=True, nullable=True
    )
    server_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pricing_tier_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    server_pool: Mapped[str] = mapped_column(String(64))
    payment_method: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(8))
    gross_deposit: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    net_investment: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    clearance_ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    principal_unlocked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_dividend_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user: Mapped[User] = relationship(back_populates="slots")
    payment_intent: Mapped[PaymentIntent | None] = relationship(back_populates="slot")


class ReferralDividendMilestone(Base):
    __tablename__ = "referral_dividend_milestones"
    __table_args__ = (
        UniqueConstraint("user_id", "referral_threshold", name="uq_referral_dividend_milestone"),
        Index("ix_referral_dividend_milestones_window", "user_id", "starts_at", "ends_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    referral_threshold: Mapped[int] = mapped_column(nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WalletBalance(Base):
    __tablename__ = "wallet_balances"
    __table_args__ = (UniqueConstraint("user_id", "currency", name="uq_wallet_user_currency"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    currency: Mapped[str] = mapped_column(String(8))
    withdrawable_amount: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), default=Decimal("0"), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="balances")


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ledger_idempotency"),
        Index("ix_ledger_entries_account_currency_created", "account_code", "currency", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=True)
    account_code: Mapped[str] = mapped_column(String(64), index=True)
    currency: Mapped[str] = mapped_column(String(8))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    entry_type: Mapped[str] = mapped_column(String(24), index=True)
    reference_type: Mapped[str] = mapped_column(String(32))
    reference_id: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    user: Mapped[User] = relationship(back_populates="ledger_entries")


class WithdrawalRequest(Base):
    __tablename__ = "withdrawal_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_withdrawal_idempotency"),
        Index("ix_withdrawals_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    quote_id: Mapped[str | None] = mapped_column(
        ForeignKey("withdrawal_quotes.id"), unique=True, nullable=True
    )
    currency: Mapped[str] = mapped_column(String(8))
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    platform_fee: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    payout_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    network_fee_estimate: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), default=Decimal("0"), nullable=False
    )
    native_network_fee_estimate: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), default=Decimal("0"), nullable=False
    )
    network_fee_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True
    )
    network_fee_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    ton_usd_rate: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    settlement_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    settlement_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True
    )
    settlement_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True
    )
    wallet_address_snapshot: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="requested", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    user: Mapped[User] = relationship(back_populates="withdrawal_requests")


class WithdrawalQuote(Base):
    __tablename__ = "withdrawal_quotes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    currency: Mapped[str] = mapped_column(String(8))
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    platform_fee: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    network_fee_estimate: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    native_network_fee_estimate: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    network_fee_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    ton_usd_rate: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    payout_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    withdrawal_id: Mapped[str | None] = mapped_column(
        String(36), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TreasuryBalance(Base):
    __tablename__ = "treasury_balances"

    currency: Mapped[str] = mapped_column(String(8), primary_key=True)
    amount: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), default=Decimal("0"), nullable=False
    )


class FinancialTelemetry(Base):
    __tablename__ = "financial_telemetry"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=True)
    event_type: Mapped[str] = mapped_column(String(24), index=True)
    currency: Mapped[str] = mapped_column(String(8))
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=Decimal("0"))
    commissions_paid: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=Decimal("0"))
    platform_fees_collected: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), default=Decimal("0")
    )
    blockchain_gas_fee_ton: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), default=Decimal("0")
    )
    tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reference_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
