import hashlib
from datetime import datetime, timezone
from decimal import Decimal

from config import (
    APP_WALLET_MNEMONIC,
    TON_DEPOSIT_ADDRESS,
    TON_API_ENDPOINT,
    TON_API_KEY,
    TON_NETWORK,
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
        TON_DEPOSIT_ADDRESS
        and TON_API_ENDPOINT
        and TON_API_KEY
        and APP_WALLET_MNEMONIC
        and TON_NETWORK in ("testnet", "mainnet")
    )
    if not configured:
        return {
            "deposit_address": None,
            "memo": build_deposit_memo(intent),
            "network": TON_NETWORK,
            "configured": False,
            "note": "Configure the TON RPC and receiving wallet before sending funds.",
        }
    return {
        "deposit_address": TON_DEPOSIT_ADDRESS,
        "memo": build_deposit_memo(intent),
        "amount": str(intent.expected_amount),
        "currency": "TON",
        "network": TON_NETWORK,
        "quote_expires_at": _iso_utc(intent.quote_expires_at),
        "configured": True,
    }


def amounts_match(intent: PaymentIntent, observed: Decimal, tolerance_ratio: Decimal = Decimal("0.001")) -> bool:
    expected = intent.expected_amount
    if expected <= 0:
        return False
    delta = abs(observed - expected)
    return delta <= expected * tolerance_ratio
