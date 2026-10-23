from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from config import (
    ALLOW_DEMO_PAYMENTS,
    APP_WALLET_MNEMONIC,
    DEMO_TON_GAS_FEE_ESTIMATE,
    TON_API_ENDPOINT,
    TON_API_KEY,
    TON_DEPOSIT_ADDRESS,
    TON_NETWORK,
    TON_PAYOUTS_ENABLED,
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


class PayoutAdapter(Protocol):
    async def quote_network_fee(
        self,
        currency: str,
        payout_amount: Decimal,
        *,
        destination: str | None = None,
    ) -> NetworkFeeQuote: ...

    async def send(
        self, withdrawal: WithdrawalRequest, *, idempotency_key: str
    ) -> PayoutResult: ...


class SimulatedPayoutAdapter:
    async def quote_network_fee(
        self,
        currency: str,
        payout_amount: Decimal,
        *,
        destination: str | None = None,
    ) -> NetworkFeeQuote:
        if currency != "TON":
            raise ValueError("Payouts support TON only")
        native_fee = DEMO_TON_GAS_FEE_ESTIMATE
        return NetworkFeeQuote(native_fee, native_fee, currency)

    async def send(
        self, withdrawal: WithdrawalRequest, *, idempotency_key: str
    ) -> PayoutResult:
        if withdrawal.currency != "TON":
            raise PayoutRejected("Payouts support TON only")
        actual_fee_charge = withdrawal.native_network_fee_estimate
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
        self,
        currency: str,
        payout_amount: Decimal,
        *,
        destination: str | None = None,
    ) -> NetworkFeeQuote:
        raise PayoutRejected("No live TON payout and network-fee quote provider is configured")

    async def send(
        self, withdrawal: WithdrawalRequest, *, idempotency_key: str
    ) -> PayoutResult:
        raise PayoutRejected("No live TON payout provider is configured; funds were refunded")


def get_payout_adapter() -> PayoutAdapter:
    if TON_PAYOUTS_ENABLED:
        if ALLOW_DEMO_PAYMENTS:
            raise RuntimeError(
                "Disable ALLOW_DEMO_PAYMENTS before enabling live TON payouts"
            )
        if not all(
            (
                TON_API_ENDPOINT,
                TON_API_KEY,
                APP_WALLET_MNEMONIC,
                TON_DEPOSIT_ADDRESS,
            )
        ):
            raise RuntimeError(
                "Live TON payouts require the configured RPC, key, mnemonic, and wallet address"
            )
        from payments.ton_payout import TonPayoutAdapter

        return TonPayoutAdapter()
    return SimulatedPayoutAdapter() if ALLOW_DEMO_PAYMENTS else UnconfiguredPayoutAdapter()
