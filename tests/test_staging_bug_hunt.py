"""Isolated regression checks for the app's money and identity rules.

Run with: .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
All database writes use a fresh in-memory SQLite database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import unittest
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import engine as investment_engine
from database import Base
from engine import (
    confirm_investment_from_payment,
    execute_withdrawal,
    process_approved_withdrawals,
    quote_withdrawal,
    sync_referral_rewards,
)
from ledger import LedgerError, apply_ledger_delta
from models import (
    LedgerEntry,
    PaymentIntent,
    ReferralDividendMilestone,
    ServerSlot,
    TreasuryBalance,
    User,
    WalletBalance,
    utc_now,
)
from payments.payouts import (
    NetworkFeeQuote,
    PayoutRejected,
    SimulatedPayoutAdapter,
)
from payments.ton import TonIndexerAdapter
from telegram_auth import TelegramAuthError, validate_telegram_init_data


class QuoteThenRejectAdapter:
    async def quote_network_fee(self, currency: str, payout_amount: Decimal) -> NetworkFeeQuote:
        return NetworkFeeQuote(Decimal("0"), Decimal("0"), currency)

    async def send(self, withdrawal, *, idempotency_key: str):
        raise PayoutRejected("staging provider rejected transfer")


class StagingBugHunt(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.db_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.db_engine, expire_on_commit=False)
        self.original_utc_now = investment_engine.utc_now

    async def asyncTearDown(self) -> None:
        investment_engine.utc_now = self.original_utc_now
        await self.db_engine.dispose()

    async def add_user(self, db, user_id: int, *, referred_by: int | None = None, wallet: str | None = None) -> User:
        user = User(
            id=user_id,
            username=f"stage_{user_id}",
            referred_by=referred_by,
            referral_code=f"T{user_id:05d}"[-6:],
            wallet_address=wallet,
        )
        db.add(user)
        await db.flush()
        return user

    async def add_intent(self, db, *, intent_id: str, user_id: int, amount: str = "100", currency: str = "USDT") -> PaymentIntent:
        now = utc_now()
        intent = PaymentIntent(
            id=intent_id,
            user_id=user_id,
            server_id="h100-node-01",
            pricing_tier_id="tier-300",
            server_pool="NVIDIA-H100-POOL-A",
            payment_method="crypto" if currency != "XTR" else "stars",
            currency=currency,
            expected_amount=Decimal(amount),
            quote_expires_at=now,
            status="awaiting_payment",
            provider="ton" if currency != "XTR" else "stars",
            expires_at=now + timedelta(hours=1),
        )
        db.add(intent)
        await db.flush()
        return intent

    async def credit(self, db, user_id: int, currency: str, amount: str) -> None:
        await apply_ledger_delta(
            db,
            user_id=user_id,
            currency=currency,
            amount=Decimal(amount),
            entry_type="STAGING_SEED",
            reference_type="staging_seed",
            reference_id=f"{user_id}:{currency}",
            idempotency_key=f"bug-hunt-seed:{user_id}:{currency}",
        )
        await db.commit()

    async def test_first_confirmed_deposit_commission_is_once_and_replay_safe(self) -> None:
        async with self.sessions() as db:
            await self.add_user(db, 1)
            await self.add_user(db, 2, referred_by=1)
            first = await self.add_intent(db, intent_id="intent-first-deposit", user_id=2)
            await db.commit()

            original = await confirm_investment_from_payment(
                db, first.id, external_payment_id="charge-first", provider="ton"
            )
            replay = await confirm_investment_from_payment(
                db, first.id, external_payment_id="charge-first", provider="ton"
            )
            second = await self.add_intent(
                db, intent_id="intent-second-deposit", user_id=2, amount="200"
            )
            await db.commit()
            await confirm_investment_from_payment(db, second.id)

            commissions = await db.scalar(
                select(func.count(LedgerEntry.id)).where(
                    LedgerEntry.user_id == 1, LedgerEntry.entry_type == "COMMISSION"
                )
            )
            referrer_balance = await db.scalar(
                select(WalletBalance).where(
                    WalletBalance.user_id == 1, WalletBalance.currency == "USDT"
                )
            )
            slots = await db.scalar(
                select(func.count(ServerSlot.id)).where(ServerSlot.user_id == 2)
            )
            self.assertEqual(original["slot_id"], replay["slot_id"])
            self.assertTrue(replay["already_confirmed"])
            self.assertEqual(commissions, 1)
            self.assertEqual(referrer_balance.withdrawable_amount, Decimal("30.00000000"))
            self.assertEqual(slots, 2)

    async def test_ledger_rejects_negative_balance_and_repeated_key_does_not_double_credit(self) -> None:
        async with self.sessions() as db:
            await self.add_user(db, 10)
            await self.credit(db, 10, "USDT", "12")
            for _ in range(2):
                await apply_ledger_delta(
                    db,
                    user_id=10,
                    currency="USDT",
                    amount=Decimal("12"),
                    entry_type="STAGING_SEED",
                    reference_type="staging_seed",
                    reference_id="same-credit",
                    idempotency_key="same-credit-key",
                )
                await db.commit()
            balance = await db.scalar(
                select(WalletBalance).where(
                    WalletBalance.user_id == 10, WalletBalance.currency == "USDT"
                )
            )
            with self.assertRaises(LedgerError):
                await apply_ledger_delta(
                    db,
                    user_id=10,
                    currency="USDT",
                    amount=Decimal("-13"),
                    entry_type="WITHDRAWAL",
                    reference_type="test",
                    reference_id="overdraw",
                    idempotency_key="overdraw-key",
                )
            await db.rollback()
            self.assertEqual(balance.withdrawable_amount, Decimal("24.00000000"))

    async def test_five_and_fifty_referral_thresholds_create_windows_and_unlock_principal_once(self) -> None:
        async with self.sessions() as db:
            now = utc_now()
            await self.add_user(db, 20)
            for user_id in range(21, 71):
                await self.add_user(db, user_id, referred_by=20)
                intent = PaymentIntent(
                    id=f"qualified-{user_id}",
                    user_id=user_id,
                    server_id="h100-node-01",
                    pricing_tier_id="tier-300",
                    server_pool="NVIDIA-H100-POOL-A",
                    payment_method="crypto",
                    currency="USDT",
                    expected_amount=100,
                    quote_expires_at=now,
                    status="confirmed",
                    provider="staging",
                    expires_at=now + timedelta(days=1),
                    confirmed_at=now,
                )
                db.add(intent)
            db.add(
                ServerSlot(
                    id="principal-to-unlock",
                    user_id=20,
                    server_id="h100-node-01",
                    pricing_tier_id="tier-300",
                    server_pool="NVIDIA-H100-POOL-A",
                    payment_method="crypto",
                    currency="USDT",
                    gross_deposit=100,
                    net_investment=100,
                    clearance_ready_at=now,
                    status="active",
                    is_active=True,
                    created_at=now,
                    last_dividend_at=now,
                )
            )
            await db.commit()

            qualified, milestones = await sync_referral_rewards(db, 20, now)
            await db.commit()
            balance = await db.scalar(
                select(WalletBalance).where(
                    WalletBalance.user_id == 20, WalletBalance.currency == "USDT"
                )
            )
            unlock_count = await db.scalar(
                select(func.count(LedgerEntry.id)).where(
                    LedgerEntry.idempotency_key == "principal-unlock:principal-to-unlock"
                )
            )
            self.assertEqual(qualified, 50)
            self.assertEqual(len(milestones), 10)
            self.assertEqual(milestones[0].referral_threshold, 5)
            self.assertEqual(milestones[-1].referral_threshold, 50)
            self.assertEqual(balance.withdrawable_amount, Decimal("100.00000000"))
            self.assertEqual(unlock_count, 1)

            await sync_referral_rewards(db, 20, now + timedelta(days=1))
            await db.commit()
            self.assertEqual(
                await db.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.idempotency_key == "principal-unlock:principal-to-unlock"
                    )
                ),
                1,
            )

    async def test_stars_clearance_blocks_withdrawal_quote(self) -> None:
        async with self.sessions() as db:
            user = await self.add_user(db, 30, wallet="EQ" + "A" * 46)
            now = utc_now()
            db.add(
                ServerSlot(
                    id="still-clearing-stars-slot",
                    user_id=user.id,
                    server_id="h100-node-01",
                    pricing_tier_id="tier-300",
                    server_pool="NVIDIA-H100-POOL-A",
                    payment_method="stars",
                    currency="XTR",
                    gross_deposit=300,
                    net_investment=300,
                    clearance_ready_at=now + timedelta(days=1),
                    status="active",
                    is_active=True,
                    created_at=now,
                    last_dividend_at=now,
                )
            )
            await self.credit(db, user.id, "USDT", "100")
            with self.assertRaisesRegex(ValueError, "Withdrawals are locked"):
                await quote_withdrawal(
                    db,
                    user.id,
                    Decimal("20"),
                    "USDT",
                    payout_adapter=SimulatedPayoutAdapter(),
                )

    async def test_quote_is_one_use_and_simulated_crypto_payout_reconciles(self) -> None:
        async with self.sessions() as db:
            user = await self.add_user(db, 40, wallet="EQ" + "A" * 46)
            await self.credit(db, user.id, "USDT", "100")
            adapter = SimulatedPayoutAdapter()
            quote = await quote_withdrawal(
                db, user.id, Decimal("50"), "USDT", payout_adapter=adapter
            )
            withdrawal = await execute_withdrawal(
                db,
                user.id,
                quote["quote_id"],
                idempotency_key="staging-withdrawal-idempotency-1",
            )
            replay = await execute_withdrawal(
                db,
                user.id,
                quote["quote_id"],
                idempotency_key="staging-withdrawal-idempotency-1",
            )
            self.assertEqual(withdrawal["withdrawal_id"], replay["withdrawal_id"])
            with self.assertRaisesRegex(ValueError, "already been used"):
                await execute_withdrawal(
                    db,
                    user.id,
                    quote["quote_id"],
                    idempotency_key="staging-withdrawal-idempotency-2",
                )

            processed = await process_approved_withdrawals(db, adapter)
            await db.refresh(user)
            final = await db.get(
                investment_engine.WithdrawalRequest, withdrawal["withdrawal_id"]
            )
            balance = await db.scalar(
                select(WalletBalance).where(
                    WalletBalance.user_id == user.id, WalletBalance.currency == "USDT"
                )
            )
            self.assertEqual(processed, 1)
            self.assertEqual(final.status, "completed")
            self.assertTrue(final.tx_hash.startswith("sim-"))
            self.assertEqual(balance.withdrawable_amount, Decimal("50.00000000"))

    async def test_definitive_payout_failure_refunds_user_and_reverses_fee(self) -> None:
        async with self.sessions() as db:
            user = await self.add_user(db, 50, wallet="EQ" + "A" * 46)
            await self.credit(db, user.id, "TON", "10")
            adapter = QuoteThenRejectAdapter()
            quote = await quote_withdrawal(
                db, user.id, Decimal("5"), "TON", payout_adapter=adapter
            )
            withdrawal = await execute_withdrawal(
                db,
                user.id,
                quote["quote_id"],
                idempotency_key="staging-failed-withdrawal-idempotency",
            )
            await process_approved_withdrawals(db, adapter)
            final = await db.get(
                investment_engine.WithdrawalRequest, withdrawal["withdrawal_id"]
            )
            balance = await db.scalar(
                select(WalletBalance).where(
                    WalletBalance.user_id == user.id, WalletBalance.currency == "TON"
                )
            )
            treasury = await db.get(TreasuryBalance, "TON")
            self.assertEqual(final.status, "refunded")
            self.assertEqual(balance.withdrawable_amount, Decimal("10.00000000"))
            self.assertEqual(treasury.amount, Decimal("0E-8"))


class ExternalAuthenticationBugHunt(unittest.TestCase):
    def test_telegram_init_data_signature_and_age_are_checked(self) -> None:
        token = "staging-test-bot-token"
        values = {
            "auth_date": str(int(time.time())),
            "query_id": "staging-query",
            "user": json.dumps({"id": 12345, "first_name": "Stage"}, separators=(",", ":")),
        }
        check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        init_data = urlencode(values)
        self.assertEqual(validate_telegram_init_data(init_data, token)["id"], 12345)

        tampered = init_data.replace("Stage", "Tampered")
        with self.assertRaises(TelegramAuthError):
            validate_telegram_init_data(tampered, token)

    def test_signed_ton_event_requires_expected_network_and_deposit(self) -> None:
        body = json.dumps(
            {
                "event_id": "staging-event-1",
                "intent_id": "12345678-1234-1234-1234-123456789012",
                "tx_hash": "a" * 64,
                "tx_log_index": 0,
                "amount": "4.5",
                "currency": "USDT",
                "memo": "MINT-STAGING",
                "to_address": "deposit-address",
                "token_master": "usdt-master",
                "confirmations": 2,
                "network": "mainnet",
            }
        ).encode()
        adapter = TonIndexerAdapter("staging-indexer-secret", minimum_confirmations=1)
        signature = hmac.new(
            b"staging-indexer-secret", body, hashlib.sha256
        ).hexdigest()
        self.assertTrue(adapter.verify_signature(body, signature))
        event = adapter.parse_event(body)
        adapter.validate_event(
            event,
            network="mainnet",
            deposit_address="deposit-address",
            usdt_jetton_master="usdt-master",
        )
        self.assertFalse(adapter.verify_signature(body, "0" * 64))
        with self.assertRaisesRegex(ValueError, "wrong network"):
            adapter.validate_event(
                event,
                network="testnet",
                deposit_address="deposit-address",
                usdt_jetton_master="usdt-master",
            )


if __name__ == "__main__":
    unittest.main()
