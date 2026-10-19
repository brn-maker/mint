from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import PAYMENT_INTENT_TTL_MINUTES, PAYMENT_QUOTE_MINUTES
from mock_data import SERVER_POOLS
from models import PaymentIntent, utc_now
from payments.crypto import build_crypto_instructions, build_deposit_memo
from pricing import get_server_listing, get_tier, price_tier


def _iso_utc(value: datetime) -> str:
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return normalized.isoformat()


async def create_payment_intent(
    db: AsyncSession,
    *,
    user_id: int,
    server_id: str,
    idempotency_key: str | None = None,
) -> PaymentIntent:
    if idempotency_key:
        existing = await db.scalar(
            select(PaymentIntent).where(PaymentIntent.idempotency_key == idempotency_key)
        )
        if existing is not None:
            if (
                existing.user_id != user_id
                or existing.server_id != server_id
                or existing.currency != "TON"
            ):
                raise ValueError("Idempotency key was already used for a different payment intent")
            return existing

    server = get_server_listing(server_id)
    tier = get_tier(server["tier_id"])
    if server["pool"] not in SERVER_POOLS:
        raise ValueError("Unknown server pool")

    deposit_amount = price_tier(server["tier_id"])
    now = utc_now()
    intent = PaymentIntent(
        id=str(uuid4()),
        user_id=user_id,
        server_id=server["id"],
        pricing_tier_id=tier["id"],
        server_pool=server["pool"],
        payment_method="crypto",
        currency="TON",
        expected_amount=deposit_amount,
        quote_expires_at=now + timedelta(minutes=PAYMENT_QUOTE_MINUTES),
        status="pending",
        provider="ton",
        expires_at=now + timedelta(minutes=PAYMENT_INTENT_TTL_MINUTES),
        idempotency_key=idempotency_key,
    )
    intent.deposit_memo = build_deposit_memo(intent)
    db.add(intent)
    await db.flush()

    intent.status = "awaiting_payment"
    intent.chain = "TON"
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if not idempotency_key:
            raise
        existing = await db.scalar(
            select(PaymentIntent).where(PaymentIntent.idempotency_key == idempotency_key)
        )
        if existing is None or (
            existing.user_id != user_id
            or existing.server_id != server_id
            or existing.currency != "TON"
        ):
            raise ValueError("Idempotency key was already used for a different payment intent")
        return existing
    await db.refresh(intent)
    return intent


def mark_intent_awaiting_payment(intent: PaymentIntent) -> None:
    if intent.status == "pending":
        intent.status = "awaiting_payment"


def build_client_payload(
    intent: PaymentIntent, *, slot_id: str | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "intent_id": intent.id,
        "status": intent.status,
        "server_id": intent.server_id,
        "pricing_tier_id": intent.pricing_tier_id,
        "payment_method": intent.payment_method,
        "currency": intent.currency,
        "expected_amount": str(intent.expected_amount),
        "quote_expires_at": _iso_utc(intent.quote_expires_at),
        "expires_at": _iso_utc(intent.expires_at),
        "confirmed_at": _iso_utc(intent.confirmed_at) if intent.confirmed_at else None,
        "tx_hash": intent.tx_hash,
        "slot_id": slot_id,
    }
    payload["crypto"] = build_crypto_instructions(intent)
    return payload
