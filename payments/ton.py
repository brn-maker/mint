import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import PaymentIntent, utc_now


logger = logging.getLogger(__name__)
VERIFIER_SCRIPT = Path(__file__).with_name("ton_verifier.mjs")
NANO_TON = Decimal("1000000000")


class TonTransaction(TypedDict):
    tx_hash: str
    amount_nano: str
    comment: str
    incoming_bounced: bool
    bounce_output: bool


class TonVerifierError(RuntimeError):
    pass


async def scan_ton_transactions(since: datetime) -> list[TonTransaction]:
    since_utc = (
        since.replace(tzinfo=timezone.utc)
        if since.tzinfo is None
        else since.astimezone(timezone.utc)
    )
    try:
        process = await asyncio.create_subprocess_exec(
            "node",
            str(VERIFIER_SCRIPT),
            str(int(since_utc.timestamp())),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
    except (OSError, asyncio.TimeoutError) as exc:
        raise TonVerifierError("TON transaction verifier could not complete") from exc
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip() if stderr else ""
        raise TonVerifierError(
            "TON transaction verifier failed to scan"
            + (f": {detail}" if detail else "")
        )
    try:
        records = json.loads(stdout)
        if not isinstance(records, list):
            raise TypeError("verifier output must be an array")
        parsed: list[TonTransaction] = []
        for record in records:
            if not isinstance(record, dict):
                raise TypeError("transaction record must be an object")
            if (
                not isinstance(record.get("tx_hash"), str)
                or not isinstance(record.get("amount_nano"), str)
                or not isinstance(record.get("comment"), str)
                or not isinstance(record.get("incoming_bounced"), bool)
                or not isinstance(record.get("bounce_output"), bool)
            ):
                raise TypeError("transaction record has missing or invalid fields")
            parsed.append({
                "tx_hash": str(record["tx_hash"]),
                "amount_nano": str(record["amount_nano"]),
                "comment": str(record["comment"]),
                "incoming_bounced": record["incoming_bounced"],
                "bounce_output": record["bounce_output"],
            })
        return parsed
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise TonVerifierError("TON transaction verifier returned invalid data") from exc


def find_matching_transaction(
    intent: PaymentIntent, transactions: list[TonTransaction]
) -> TonTransaction | None:
    expected_nano = Decimal(intent.expected_amount) * NANO_TON
    if expected_nano != expected_nano.to_integral_value():
        raise ValueError("Payment amount cannot be represented in nanoTON")
    for transaction in transactions:
        if (
            transaction.get("incoming_bounced", False)
            or transaction.get("bounce_output", False)
        ):
            continue
        if (
            transaction["comment"] == intent.deposit_memo
            and Decimal(transaction["amount_nano"]) == expected_nano
        ):
            return transaction
    return None


async def confirm_ton_transactions(
    db: AsyncSession, transactions: list[TonTransaction]
) -> int:
    from engine import confirm_investment_from_payment

    confirmed = 0
    for transaction in transactions:
        if (
            transaction.get("incoming_bounced", False)
            or transaction.get("bounce_output", False)
        ):
            continue
        tx_hash = transaction["tx_hash"]
        already_used = await db.scalar(
            select(PaymentIntent.id).where(
                PaymentIntent.chain == "TON",
                PaymentIntent.tx_hash == tx_hash,
                PaymentIntent.tx_log_index == 0,
            )
        )
        if already_used is not None:
            continue

        amount = Decimal(transaction["amount_nano"]) / NANO_TON
        intent = await db.scalar(
            select(PaymentIntent).where(
                PaymentIntent.status == "awaiting_payment",
                PaymentIntent.expires_at > utc_now(),
                PaymentIntent.currency == "TON",
                PaymentIntent.deposit_memo == transaction["comment"],
                PaymentIntent.expected_amount == amount,
            )
        )
        if intent is None:
            continue

        try:
            await confirm_investment_from_payment(
                db,
                intent.id,
                tx_hash=tx_hash,
                tx_log_index=0,
                provider="ton",
            )
        except IntegrityError:
            await db.rollback()
            logger.exception("TON transaction %s was already claimed", tx_hash)
            continue
        except ValueError as exc:
            logger.warning(
                "TON payment intent %s was not confirmed: %s", intent.id, exc
            )
            continue
        confirmed += 1
    return confirmed
