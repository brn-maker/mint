import hashlib
from datetime import datetime, timezone
from decimal import Decimal

from config import (
    CRYPTO_DEPOSIT_ADDRESS,
    TON_INDEXER_WEBHOOK_SECRET,
    TON_NETWORK,
    USDT_JETTON_MASTER,
)
from models import PaymentIntent


def _iso_utc(value: datetime) -> str:
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return normalized.isoformat()


def build_deposit_memo(intent: PaymentIntent) -> str:
    digest = hashlib.sha256(intent.id.encode("utf-8")).hexdigest()[:16]
    return f"MINT-{digest.upper()}"


def build_crypto_instructions(intent: PaymentIntent) -> dict[str, object]:
    configured = bool(
        CRYPTO_DEPOSIT_ADDRESS
        and TON_INDEXER_WEBHOOK_SECRET
        and (intent.currency != "USDT" or USDT_JETTON_MASTER)
    )
    if not configured:
        return {
            "deposit_address": None,
            "memo": build_deposit_memo(intent),
            "network": TON_NETWORK,
            "usdt_jetton_master": USDT_JETTON_MASTER or None,
            "configured": False,
            "note": "Configure the deposit address, signed TON indexer webhook, and USDT jetton master (for USDT) before sending funds.",
        }
    return {
        "deposit_address": CRYPTO_DEPOSIT_ADDRESS,
        "memo": build_deposit_memo(intent),
        "amount": str(intent.expected_amount),
        "currency": intent.currency,
        "network": TON_NETWORK,
        "usdt_jetton_master": USDT_JETTON_MASTER or None if intent.currency == "USDT" else None,
        "quote_expires_at": _iso_utc(intent.quote_expires_at),
        "configured": True,
    }


def amounts_match(intent: PaymentIntent, observed: Decimal, tolerance_ratio: Decimal = Decimal("0.001")) -> bool:
    expected = intent.expected_amount
    if expected <= 0:
        return False
    delta = abs(observed - expected)
    return delta <= expected * tolerance_ratio
