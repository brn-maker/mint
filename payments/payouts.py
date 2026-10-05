from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from config import (
    ALLOW_DEMO_PAYMENTS,
    DEMO_TON_GAS_FEE_ESTIMATE,
    DEMO_TON_USD_ESTIMATE,
)
from models import WithdrawalRequest


class PayoutRejected(RuntimeError):
    """A payout provider definitively rejected the transfer; it is safe to refund."""


class PayoutPending(RuntimeError):
    """Payout outcome is unknown; retry with the same idempotency key."""


@dataclass(frozen=True)
class PayoutResult:
    tx_hash: str
    payout_amount: Decimal
    payout_currency: str
    network_fee_amount: Decimal
    network_fee_currency: str = "TON"


@dataclass(frozen=True)
class NetworkFeeQuote:
    native_fee_amount: Decimal
    withdrawal_fee_amount: Decimal
    withdrawal_currency: str
    ton_usd_rate: Decimal | None = None


class PayoutAdapter(Protocol):
    async def quote_network_fee(
        self, currency: str, payout_amount: Decimal
    ) -> NetworkFeeQuote: ...

    async def send(
        self, withdrawal: WithdrawalRequest, *, idempotency_key: str
    ) -> PayoutResult: ...


class SimulatedPayoutAdapter:
    async def quote_network_fee(
        self, currency: str, payout_amount: Decimal
    ) -> NetworkFeeQuote:
        native_fee = DEMO_TON_GAS_FEE_ESTIMATE
        if currency == "TON":
            return NetworkFeeQuote(native_fee, native_fee, currency)
        if currency == "USDT":
            ton_usd = DEMO_TON_USD_ESTIMATE
            return NetworkFeeQuote(native_fee, native_fee * ton_usd, currency, ton_usd)
        raise ValueError("Simulated crypto payouts support TON and USDT only")

    async def send(
        self, withdrawal: WithdrawalRequest, *, idempotency_key: str
    ) -> PayoutResult:
        actual_fee_charge = withdrawal.native_network_fee_estimate
        if withdrawal.currency == "USDT":
            if withdrawal.ton_usd_rate is None:
                raise PayoutRejected("USDT payout is missing its TON/USD quote snapshot")
            actual_fee_charge *= withdrawal.ton_usd_rate
        actual_payout = (
            withdrawal.gross_amount - withdrawal.platform_fee - actual_fee_charge
        )
        return PayoutResult(
            tx_hash=f"sim-{withdrawal.id}",
            payout_amount=actual_payout,
            payout_currency=withdrawal.currency,
            network_fee_amount=withdrawal.native_network_fee_estimate,
        )


class UnconfiguredPayoutAdapter:
    async def quote_network_fee(
        self, currency: str, payout_amount: Decimal
    ) -> NetworkFeeQuote:
        raise PayoutRejected(
            "No live payout and network-fee quote provider is configured"
        )

    async def send(
        self, withdrawal: WithdrawalRequest, *, idempotency_key: str
    ) -> PayoutResult:
        raise PayoutRejected("No live payout provider is configured; funds were refunded")


def get_payout_adapter() -> PayoutAdapter:
    return SimulatedPayoutAdapter() if ALLOW_DEMO_PAYMENTS else UnconfiguredPayoutAdapter()
