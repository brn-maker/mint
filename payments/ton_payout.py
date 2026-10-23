import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from config import (
    APP_WALLET_MNEMONIC,
    TON_API_ENDPOINT,
    TON_API_KEY,
    TON_DEPOSIT_ADDRESS,
)
from models import WithdrawalRequest
from payments.payouts import (
    NetworkFeeQuote,
    PayoutPending,
    PayoutRejected,
    PayoutResult,
)


PAYOUT_SCRIPT = Path(__file__).with_name("ton_payout.mjs")
AMOUNT_QUANTUM = Decimal("0.00000001")


class TonPayoutAdapter:
    async def quote_network_fee(
        self,
        currency: str,
        payout_amount: Decimal,
        *,
        destination: str | None = None,
    ) -> NetworkFeeQuote:
        if currency != "TON" or not destination:
            raise PayoutRejected("TON payouts support TON only")
        try:
            result = await self._run(
                {
                    "operation": "quote",
                    "currency": currency,
                    "amount": str(payout_amount),
                    "destination": destination,
                }
            )
            native_fee = self._decimal(result, "native_fee_amount")
            withdrawal_fee = self._decimal(result, "withdrawal_fee_amount")
            if native_fee < 0 or withdrawal_fee < 0:
                raise ValueError("fee amounts must be non-negative")
            return NetworkFeeQuote(native_fee, withdrawal_fee, currency)
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            raise PayoutRejected("Unable to obtain a TON fee quote") from exc

    async def send(
        self, withdrawal: WithdrawalRequest, *, idempotency_key: str
    ) -> PayoutResult:
        if withdrawal.currency != "TON":
            raise PayoutRejected("TON payouts support TON only")
        try:
            result = await self._run(
                {
                    "operation": "send",
                    "withdrawal_id": withdrawal.id,
                    "idempotency_key": idempotency_key,
                    "destination": withdrawal.wallet_address_snapshot,
                    "amount": str(withdrawal.payout_amount),
                }
            )
            return PayoutResult(
                tx_hash=self._required_string(result, "tx_hash"),
                payout_amount=self._decimal(result, "payout_amount"),
                payout_currency=self._required_string(result, "payout_currency"),
                network_fee_amount=self._decimal(result, "network_fee_amount"),
                network_fee_currency=self._required_string(
                    result, "network_fee_currency"
                ),
            )
        except _PayoutScriptError as exc:
            if exc.category == "rejected":
                raise PayoutRejected(
                    "TON payout was rejected before submission"
                ) from exc
            raise PayoutPending(
                "TON payout submission may have reached the chain; "
                "it will be checked before retrying"
            ) from exc
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            raise PayoutPending(
                "TON payout result could not be verified; "
                "it will be checked before retrying"
            ) from exc

    async def _run(self, payload: dict[str, str]) -> dict[str, Any]:
        if not all(
            (
                TON_API_ENDPOINT,
                TON_API_KEY,
                APP_WALLET_MNEMONIC,
                TON_DEPOSIT_ADDRESS,
            )
        ):
            raise ValueError("TON payout configuration is incomplete")
        try:
            process = await asyncio.create_subprocess_exec(
                "node",
                str(PAYOUT_SCRIPT),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(json.dumps(payload).encode("utf-8")),
                    timeout=90,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise
        except (OSError, asyncio.TimeoutError) as exc:
            raise _PayoutScriptError("Payout helper could not complete", "pending") from exc

        try:
            response = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            category = "pending" if payload["operation"] == "send" else "rejected"
            raise _PayoutScriptError("Payout helper returned invalid output", category) from exc
        if not isinstance(response, dict):
            category = "pending" if payload["operation"] == "send" else "rejected"
            raise _PayoutScriptError("Payout helper returned invalid output", category)
        if process.returncode != 0 or "error" in response:
            category = response.get("category")
            if category not in {"rejected", "pending"}:
                category = "pending" if payload["operation"] == "send" else "rejected"
            raise _PayoutScriptError(
                str(response.get("error", "Payout helper failed"))[:200],
                category,
            )
        return response

    @staticmethod
    def _decimal(result: dict[str, Any], key: str) -> Decimal:
        value = result[key]
        if not isinstance(value, str):
            raise TypeError(f"{key} must be a decimal string")
        amount = Decimal(value)
        if not amount.is_finite():
            raise ValueError(f"{key} must be finite")
        return amount.quantize(AMOUNT_QUANTUM)

    @staticmethod
    def _required_string(result: dict[str, Any], key: str) -> str:
        value = result[key]
        if not isinstance(value, str) or not value:
            raise TypeError(f"{key} must be a non-empty string")
        return value


class _PayoutScriptError(RuntimeError):
    def __init__(self, message: str, category: str) -> None:
        super().__init__(message)
        self.category = category
