import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from config import (
    DIVIDEND_BATCH_INTERVAL_SECONDS,
    PAYMENT_EXPIRY_INTERVAL_SECONDS,
    TON_PAYMENT_SCAN_INTERVAL_SECONDS,
    WITHDRAWAL_WORKER_INTERVAL_SECONDS,
)
from database import SessionLocal
from engine import (
    accrue_dividends_batch,
    expire_stale_payment_intents,
    process_approved_withdrawals,
)
from models import PaymentIntent, utc_now
from payments.payouts import get_payout_adapter
from payments.ton import confirm_ton_transactions, scan_ton_transactions


logger = logging.getLogger(__name__)


async def _run_periodically(
    name: str,
    interval: float,
    job: Callable[[], Awaitable[int]],
) -> None:
    while True:
        try:
            count = await job()
            if count:
                logger.info("Background task %s processed %s records", name, count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background task %s failed", name)
        await asyncio.sleep(interval)


async def run_payment_workers() -> None:
    payout_adapter = get_payout_adapter()

    async def expire_intents() -> int:
        async with SessionLocal() as db:
            return await expire_stale_payment_intents(db)

    async def process_withdrawals() -> int:
        async with SessionLocal() as db:
            return await process_approved_withdrawals(db, payout_adapter)

    async def accrue_dividends() -> int:
        async with SessionLocal() as db:
            return await accrue_dividends_batch(db)

    async def scan_ton_payments() -> int:
        async with SessionLocal() as db:
            oldest_intent = await db.scalar(
                select(PaymentIntent.created_at)
                .where(
                    PaymentIntent.status == "awaiting_payment",
                    PaymentIntent.currency == "TON",
                    PaymentIntent.expires_at > utc_now(),
                )
                .order_by(PaymentIntent.created_at)
                .limit(1)
            )
            if oldest_intent is None:
                return 0
            transactions = await scan_ton_transactions(oldest_intent)
            return await confirm_ton_transactions(db, transactions)

    tasks = (
        asyncio.create_task(
            _run_periodically(
                "scan-ton-payments",
                TON_PAYMENT_SCAN_INTERVAL_SECONDS,
                scan_ton_payments,
            )
        ),
        asyncio.create_task(
            _run_periodically(
                "expire-payment-intents", PAYMENT_EXPIRY_INTERVAL_SECONDS, expire_intents
            )
        ),
        asyncio.create_task(
            _run_periodically(
                "process-withdrawals",
                WITHDRAWAL_WORKER_INTERVAL_SECONDS,
                process_withdrawals,
            )
        ),
        asyncio.create_task(
            _run_periodically(
                "accrue-dividends", DIVIDEND_BATCH_INTERVAL_SECONDS, accrue_dividends
            )
        ),
    )
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
