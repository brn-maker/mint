import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import engine as dividend_engine
from database import Base
from dividends import DAILY_DIVIDEND_RATES, accrued_dividend, ghost_week_start
from engine import accrue_dividends, sync_referral_rewards
from models import (
    LedgerEntry,
    PaymentIntent,
    ReferralDividendMilestone,
    ServerSlot,
    User,
    WalletBalance,
)


class DividendRulesTest(unittest.TestCase):
    def test_server_rates_are_daily_and_based_on_fixed_principal(self) -> None:
        server_ids = (
            "h100-node-01",
            "h100-node-02",
            "a100-node-01",
            "a100-node-02",
            "h100-node-03",
            "a100-node-03",
        )
        self.assertEqual(
            [DAILY_DIVIDEND_RATES[server_id] for server_id in server_ids],
            [Decimal(rate) / 100 for rate in (20, 25, 30, 35, 40, 45)],
        )
        self.assertEqual(
            accrued_dividend("h100-node-01", Decimal("300"), 86400),
            Decimal("60.00000000"),
        )

    def test_ghost_reset_uses_monday_midnight_utc_plus_three(self) -> None:
        now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
        self.assertEqual(
            ghost_week_start(now),
            datetime(2026, 9, 27, 21, tzinfo=timezone.utc),
        )


class ReferralDividendLedgerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.db_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.db_engine, expire_on_commit=False)
        self.original_utc_now = dividend_engine.utc_now

    async def asyncTearDown(self) -> None:
        dividend_engine.utc_now = self.original_utc_now
        await self.db_engine.dispose()

    async def test_milestone_pays_at_utc_midnight_and_unlocks_principal_at_50(self) -> None:
        start = datetime(2026, 1, 1, 23, 59, tzinfo=timezone.utc)
        async with self.sessions() as db:
            owner = User(id=1, referral_code="OWN001")
            db.add(owner)
            for user_id in range(2, 7):
                db.add(User(id=user_id, referred_by=1, referral_code=f"REF{user_id:03}"))
                db.add(
                    PaymentIntent(
                        id=f"intent-{user_id}",
                        user_id=user_id,
                        server_id="h100-node-01",
                        pricing_tier_id="tier-300",
                        server_pool="NVIDIA-H100-POOL-A",
                        payment_method="stars",
                        currency="XTR",
                        expected_amount=300,
                        quote_expires_at=start,
                        status="confirmed",
                        provider="stars",
                        expires_at=start + timedelta(days=1),
                    )
                )
            slot = ServerSlot(
                id="server-slot",
                user_id=1,
                server_id="h100-node-01",
                pricing_tier_id="tier-300",
                server_pool="NVIDIA-H100-POOL-A",
                payment_method="stars",
                currency="XTR",
                gross_deposit=100,
                net_investment=100,
                clearance_ready_at=start + timedelta(days=21),
                status="active",
                is_active=True,
                created_at=start - timedelta(days=1),
                last_dividend_at=start - timedelta(minutes=10),
            )
            db.add(slot)
            await db.commit()

            dividend_engine.utc_now = lambda: start
            await accrue_dividends(db, owner)
            milestone = await db.scalar(
                select(ReferralDividendMilestone).where(
                    ReferralDividendMilestone.user_id == 1,
                    ReferralDividendMilestone.referral_threshold == 5,
                )
            )
            self.assertIsNotNone(milestone)
            self.assertIsNone(
                await db.scalar(
                    select(WalletBalance.id).where(WalletBalance.user_id == 1)
                )
            )

            first_midnight = datetime(2026, 1, 2, 0, tzinfo=timezone.utc)
            dividend_engine.utc_now = lambda: first_midnight
            await accrue_dividends(db, owner)
            await accrue_dividends(db, owner)
            balance = await db.scalar(
                select(WalletBalance).where(WalletBalance.user_id == 1)
            )
            self.assertEqual(balance.withdrawable_amount, Decimal("20.00000000"))

            for user_id in range(7, 52):
                db.add(
                    User(id=user_id, referred_by=1, referral_code=f"REF{user_id:03}")
                )
                db.add(
                    PaymentIntent(
                        id=f"intent-{user_id}",
                        user_id=user_id,
                        server_id="h100-node-01",
                        pricing_tier_id="tier-300",
                        server_pool="NVIDIA-H100-POOL-A",
                        payment_method="stars",
                        currency="XTR",
                        expected_amount=300,
                        quote_expires_at=start,
                        status="confirmed",
                        provider="stars",
                        expires_at=start + timedelta(days=1),
                    )
                )
            await db.commit()
            await sync_referral_rewards(
                db, 1, datetime(2026, 1, 3, 1, tzinfo=timezone.utc)
            )
            await db.commit()
            await db.refresh(slot)
            balance = await db.scalar(
                select(WalletBalance).where(WalletBalance.user_id == 1)
            )
            unlock = await db.scalar(
                select(LedgerEntry).where(
                    LedgerEntry.idempotency_key == "principal-unlock:server-slot"
                )
            )
            milestone_50 = await db.scalar(
                select(ReferralDividendMilestone.id).where(
                    ReferralDividendMilestone.user_id == 1,
                    ReferralDividendMilestone.referral_threshold == 50,
                )
            )
            self.assertIsNotNone(slot.principal_unlocked_at)
            self.assertEqual(balance.withdrawable_amount, Decimal("120.00000000"))
            self.assertEqual(unlock.amount, Decimal("100.00000000"))
            self.assertIsNotNone(milestone_50)


if __name__ == "__main__":
    unittest.main()
