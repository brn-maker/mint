"""Isolated regression checks for the app's money and identity rules.

Run with: .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
All database writes use a fresh in-memory SQLite database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import unittest
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode
from unittest.mock import patch

import httpx
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost:5432/test",
)

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import engine as investment_engine
from database import Base
from main import _get_or_create_user
import main as mint_app
from engine import (
    confirm_investment_from_payment,
    execute_withdrawal,
    expire_stale_payment_intents,
    process_approved_withdrawals,
    quote_withdrawal,
    sync_referral_rewards,
)
from ledger import LedgerError, apply_ledger_delta
from models import (
    FinancialTelemetry,
    LedgerEntry,
    PaymentIntent,
    ReferralDividendMilestone,
    ServerSlot,
    TreasuryBalance,
    User,
    WalletBalance,
    WithdrawalQuote,
    utc_now,
)
from payments.payouts import (
    NetworkFeeQuote,
    PayoutPending,
    PayoutRejected,
    PayoutResult,
    SimulatedPayoutAdapter,
)
from payments.service import build_client_payload, create_payment_intent
from payments.ton import confirm_ton_transactions, find_matching_transaction
from pricing import PRICING_TIERS, price_tier
from referrals import build_referral_link, generate_user_referral_code
from telegram_auth import TelegramAuthError, validate_telegram_init_data


class QuoteThenRejectAdapter:
    async def quote_network_fee(
        self, currency: str, payout_amount: Decimal, *, destination: str | None = None
    ) -> NetworkFeeQuote:
        return NetworkFeeQuote(Decimal("0"), Decimal("0"), currency)

    async def send(self, withdrawal, *, idempotency_key: str):
        raise PayoutRejected("staging provider rejected transfer")


class QuoteThenPendingAdapter:
    async def quote_network_fee(
        self, currency: str, payout_amount: Decimal, *, destination: str | None = None
    ) -> NetworkFeeQuote:
        return NetworkFeeQuote(Decimal("0"), Decimal("0"), currency)

    async def send(self, withdrawal, *, idempotency_key: str):
        raise PayoutPending("staging provider transfer status is unknown")


class QuoteThenVarianceAdapter:
    async def quote_network_fee(
        self, currency: str, payout_amount: Decimal, *, destination: str | None = None
    ) -> NetworkFeeQuote:
        return NetworkFeeQuote(Decimal("0.01"), Decimal("0.01"), currency)

    async def send(self, withdrawal, *, idempotency_key: str):
        return PayoutResult(
            tx_hash="testnet-variance",
            payout_amount=withdrawal.payout_amount,
            payout_currency="TON",
            network_fee_amount=Decimal("0.005"),
        )


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

    async def add_intent(self, db, *, intent_id: str, user_id: int, amount: str = "100") -> PaymentIntent:
        now = utc_now()
        intent = PaymentIntent(
            id=intent_id,
            user_id=user_id,
            server_id="h100-node-01",
            pricing_tier_id="tier-300",
            server_pool="NVIDIA-H100-POOL-A",
            payment_method="crypto",
            currency="TON",
            expected_amount=Decimal(amount),
            quote_expires_at=now,
            status="awaiting_payment",
            provider="ton",
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
                    WalletBalance.user_id == 1, WalletBalance.currency == "TON"
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
            await self.credit(db, 10, "TON", "12")
            for _ in range(2):
                await apply_ledger_delta(
                    db,
                    user_id=10,
                    currency="TON",
                    amount=Decimal("12"),
                    entry_type="STAGING_SEED",
                    reference_type="staging_seed",
                    reference_id="same-credit",
                    idempotency_key="same-credit-key",
                )
                await db.commit()
            balance = await db.scalar(
                select(WalletBalance).where(
                    WalletBalance.user_id == 10, WalletBalance.currency == "TON"
                )
            )
            with self.assertRaises(LedgerError):
                await apply_ledger_delta(
                    db,
                    user_id=10,
                    currency="TON",
                    amount=Decimal("-25"),
                    entry_type="WITHDRAWAL",
                    reference_type="test",
                    reference_id="overdraw",
                    idempotency_key="overdraw-key",
                )
            await db.rollback()
            balance = await db.scalar(
                select(WalletBalance).where(
                    WalletBalance.user_id == 10, WalletBalance.currency == "TON"
                )
            )
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
                    currency="TON",
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
                    currency="TON",
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
                    WalletBalance.user_id == 20, WalletBalance.currency == "TON"
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

    async def test_ton_withdrawal_has_no_clearance_period(self) -> None:
        async with self.sessions() as db:
            user = await self.add_user(db, 30, wallet="EQ" + "A" * 46)
            now = utc_now()
            db.add(
                ServerSlot(
                    id="ton-slot",
                    user_id=user.id,
                    server_id="h100-node-01",
                    pricing_tier_id="tier-300",
                    server_pool="NVIDIA-H100-POOL-A",
                    payment_method="crypto",
                    currency="TON",
                    gross_deposit=300,
                    net_investment=300,
                    clearance_ready_at=now + timedelta(days=1),
                    status="active",
                    is_active=True,
                    created_at=now,
                    last_dividend_at=now,
                )
            )
            await self.credit(db, user.id, "TON", "100")
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                await quote_withdrawal(
                    db,
                    user.id,
                    Decimal("20"),
                    "OTHER",
                    payout_adapter=SimulatedPayoutAdapter(),
                )
            quote = await quote_withdrawal(
                db,
                user.id,
                Decimal("20"),
                "TON",
                payout_adapter=SimulatedPayoutAdapter(),
            )
            self.assertEqual(quote["currency"], "TON")

    async def test_non_ton_payment_intents_cannot_be_confirmed(self) -> None:
        async with self.sessions() as db:
            await self.add_user(db, 31)
            intent = await self.add_intent(
                db, intent_id="legacy-non-ton-intent", user_id=31
            )
            intent.currency = "OTHER"
            intent.payment_method = "legacy"
            await db.commit()
            with self.assertRaisesRegex(ValueError, "Only TON"):
                await confirm_investment_from_payment(db, intent.id)

    async def test_registration_retries_referral_code_collision(self) -> None:
        with patch.dict(os.environ, {"REFERRAL_SALT": "r" * 48}):
            collided_code = generate_user_referral_code(32)
            async with self.sessions() as db:
                referrer = await self.add_user(db, 320)
                db.add(
                    User(
                        id=322,
                        referral_code=collided_code,
                    )
                )
                await db.commit()

                new_user = await _get_or_create_user(
                    db, 32, referral_code=referrer.referral_code
                )
                self.assertNotEqual(new_user.referral_code, collided_code)
                self.assertEqual(new_user.referred_by, referrer.id)

    async def test_quote_is_one_use_and_simulated_crypto_payout_reconciles(self) -> None:
        async with self.sessions() as db:
            user = await self.add_user(db, 40, wallet="EQ" + "A" * 46)
            await self.credit(db, user.id, "TON", "100")
            adapter = SimulatedPayoutAdapter()
            quote = await quote_withdrawal(
                db, user.id, Decimal("50"), "TON", payout_adapter=adapter
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
                    WalletBalance.user_id == user.id, WalletBalance.currency == "TON"
                )
            )
            self.assertEqual(processed, 1)
            self.assertEqual(final.status, "completed")
            self.assertTrue(final.tx_hash.startswith("sim-"))
            self.assertEqual(balance.withdrawable_amount, Decimal("50.00000000"))

    async def test_definitive_payout_failure_refunds_user_and_reverses_fee(self) -> None:
        async with self.sessions() as db:
            user = await self.add_user(db, 50, wallet="EQ" + "A" * 46)
            user_id = user.id
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
                    WalletBalance.user_id == user_id, WalletBalance.currency == "TON"
                )
            )
            treasury = await db.get(TreasuryBalance, "TON")
            self.assertEqual(final.status, "refunded")
            self.assertEqual(balance.withdrawable_amount, Decimal("10.00000000"))
            self.assertEqual(treasury.amount, Decimal("0E-8"))

    async def test_payout_fee_variance_credits_user_and_completes_reconciled_withdrawal(self) -> None:
        async with self.sessions() as db:
            user = await self.add_user(db, 501, wallet="EQ" + "A" * 46)
            user_id = user.id
            await self.credit(db, user.id, "TON", "10")
            adapter = QuoteThenVarianceAdapter()
            quote = await quote_withdrawal(
                db, user.id, Decimal("5"), "TON", payout_adapter=adapter
            )
            withdrawal = await execute_withdrawal(
                db,
                user.id,
                quote["quote_id"],
                idempotency_key="staging-fee-variance-withdrawal",
            )

            self.assertEqual(await process_approved_withdrawals(db, adapter), 1)
            final = await db.get(
                investment_engine.WithdrawalRequest, withdrawal["withdrawal_id"]
            )
            balance = await db.scalar(
                select(WalletBalance).where(
                    WalletBalance.user_id == user_id, WalletBalance.currency == "TON"
                )
            )
            adjustment = await db.scalar(
                select(LedgerEntry).where(
                    LedgerEntry.reference_id == withdrawal["withdrawal_id"],
                    LedgerEntry.entry_type == "WITHDRAWAL_FEE_ADJ",
                )
            )
            self.assertEqual(final.status, "completed")
            self.assertEqual(final.network_fee_amount, Decimal("0.00500000"))
            self.assertEqual(balance.withdrawable_amount, Decimal("5.00500000"))
            self.assertEqual(adjustment.amount, Decimal("0.00500000"))

    async def test_pending_payout_keeps_withdrawal_locked_for_reconciliation(self) -> None:
        async with self.sessions() as db:
            user = await self.add_user(db, 51, wallet="EQ" + "A" * 46)
            await self.credit(db, user.id, "TON", "10")
            adapter = QuoteThenPendingAdapter()
            quote = await quote_withdrawal(
                db, user.id, Decimal("5"), "TON", payout_adapter=adapter
            )
            withdrawal = await execute_withdrawal(
                db,
                user.id,
                quote["quote_id"],
                idempotency_key="staging-pending-withdrawal-1",
            )

            self.assertEqual(await process_approved_withdrawals(db, adapter), 0)
            stored = await db.get(
                investment_engine.WithdrawalRequest, withdrawal["withdrawal_id"]
            )
            balance = await db.scalar(
                select(WalletBalance).where(
                    WalletBalance.user_id == user.id,
                    WalletBalance.currency == "TON",
                )
            )
            treasury = await db.get(TreasuryBalance, "TON")
            self.assertEqual(stored.status, "processing")
            self.assertEqual(balance.withdrawable_amount, Decimal("5.00000000"))
            self.assertEqual(treasury.amount, Decimal("0.50000000"))

    async def test_withdrawal_requires_wallet_balance_and_unexpired_quote(self) -> None:
        async with self.sessions() as db:
            user = await self.add_user(db, 52)
            user_id = user.id
            adapter = SimulatedPayoutAdapter()
            with self.assertRaisesRegex(ValueError, "Set a wallet address"):
                await quote_withdrawal(
                    db, user.id, Decimal("1"), "TON", payout_adapter=adapter
                )

            user.wallet_address = "EQ" + "A" * 46
            await db.commit()
            with self.assertRaisesRegex(ValueError, "Insufficient TON"):
                await quote_withdrawal(
                    db, user.id, Decimal("1"), "TON", payout_adapter=adapter
                )

            await self.credit(db, user.id, "TON", "10")
            quote = await quote_withdrawal(
                db, user.id, Decimal("5"), "TON", payout_adapter=adapter
            )
            quote_row = await db.get(WithdrawalQuote, quote["quote_id"])
            quote_row.expires_at = utc_now() - timedelta(seconds=1)
            await db.commit()
            with self.assertRaisesRegex(ValueError, "quote has expired"):
                await execute_withdrawal(
                    db,
                    user.id,
                    quote["quote_id"],
                    idempotency_key="staging-expired-quote-1",
                )

            await db.refresh(quote_row)
            balance = await db.scalar(
                select(WalletBalance).where(
                    WalletBalance.user_id == user_id,
                    WalletBalance.currency == "TON",
                )
            )
            self.assertIsNone(quote_row.withdrawal_id)
            self.assertEqual(balance.withdrawable_amount, Decimal("10.00000000"))

    async def test_seeded_user_payment_flow_confirms_only_matching_testnet_transfer(self) -> None:
        async with self.sessions() as db:
            await self.add_user(db, 70)
            referred_user = await self.add_user(db, 71, referred_by=70)
            intent = await create_payment_intent(
                db,
                user_id=referred_user.id,
                server_id="h100-node-01",
                idempotency_key="seeded-ton-flow-0001",
            )
            replayed_intent = await create_payment_intent(
                db,
                user_id=referred_user.id,
                server_id="h100-node-01",
                idempotency_key="seeded-ton-flow-0001",
            )
            self.assertEqual(replayed_intent.id, intent.id)
            with self.assertRaisesRegex(ValueError, "different payment intent"):
                await create_payment_intent(
                    db,
                    user_id=referred_user.id,
                    server_id="h100-node-02",
                    idempotency_key="seeded-ton-flow-0001",
                )
            payload = build_client_payload(intent)
            self.assertEqual(payload["crypto"]["memo"], intent.deposit_memo)
            self.assertEqual(intent.status, "awaiting_payment")
            self.assertEqual(intent.chain, "TON")

            transfers = [
                {
                    "tx_hash": "a" * 64,
                    "amount_nano": "4500000000",
                    "comment": "MINT-WRONG-COMMENT",
                },
                {
                    "tx_hash": "b" * 64,
                    "amount_nano": "4499999999",
                    "comment": intent.deposit_memo,
                },
                {
                    "tx_hash": "c" * 64,
                    "amount_nano": "4500000000",
                    "comment": intent.deposit_memo,
                },
            ]
            self.assertEqual(await confirm_ton_transactions(db, transfers), 1)
            self.assertEqual(await confirm_ton_transactions(db, transfers), 0)

            await db.refresh(intent)
            self.assertEqual(intent.status, "confirmed")
            self.assertEqual(intent.tx_hash, "c" * 64)
            self.assertEqual(intent.tx_log_index, 0)
            slot = await db.scalar(
                select(ServerSlot).where(ServerSlot.payment_intent_id == intent.id)
            )
            commission = await db.scalar(
                select(LedgerEntry).where(
                    LedgerEntry.user_id == 70,
                    LedgerEntry.reference_id == intent.id,
                    LedgerEntry.entry_type == "COMMISSION",
                )
            )
            self.assertIsNotNone(slot)
            self.assertEqual(commission.amount, Decimal("1.35000000"))
            commission_event = await db.scalar(
                select(FinancialTelemetry).where(
                    FinancialTelemetry.reference_id == intent.id,
                    FinancialTelemetry.event_type == "COMMISSION",
                )
            )
            investment_event = await db.scalar(
                select(FinancialTelemetry).where(
                    FinancialTelemetry.reference_id == intent.id,
                    FinancialTelemetry.event_type == "INVESTMENT",
                )
            )
            self.assertEqual(commission_event.tx_hash, "c" * 64)
            self.assertEqual(investment_event.tx_hash, "c" * 64)
            await self.add_user(db, 72)
            second_intent = await create_payment_intent(
                db,
                user_id=72,
                server_id="h100-node-01",
                idempotency_key="seeded-ton-flow-0002",
            )
            self.assertEqual(
                await confirm_ton_transactions(
                    db,
                    [
                        {
                            "tx_hash": "c" * 64,
                            "amount_nano": "4500000000",
                            "comment": second_intent.deposit_memo,
                        }
                    ],
                ),
                0,
            )
            await db.refresh(second_intent)
            self.assertEqual(second_intent.status, "awaiting_payment")

    async def test_bounced_ton_transfer_is_never_confirmed(self) -> None:
        async with self.sessions() as db:
            await self.add_user(db, 73)
            intent = await create_payment_intent(
                db,
                user_id=73,
                server_id="h100-node-01",
                idempotency_key="bounced-ton-payment-test",
            )
            bounced = {
                "tx_hash": "d" * 64,
                "amount_nano": "4500000000",
                "comment": intent.deposit_memo,
                "incoming_bounced": False,
                "bounce_output": True,
            }
            self.assertIsNone(find_matching_transaction(intent, [bounced]))
            self.assertEqual(await confirm_ton_transactions(db, [bounced]), 0)
            await db.refresh(intent)
            self.assertEqual(intent.status, "awaiting_payment")

    async def test_ton_connect_submission_is_validated_against_testnet_transfer(self) -> None:
        async with self.sessions() as db:
            await self.add_user(db, 74)
            intent = await create_payment_intent(
                db,
                user_id=74,
                server_id="h100-node-01",
                idempotency_key="ton-connect-validation-test",
            )
            transaction_hash = "e" * 64
            transaction = {
                "tx_hash": transaction_hash,
                "amount_nano": "4500000000",
                "comment": intent.deposit_memo,
                "incoming_bounced": False,
                "bounce_output": False,
            }
            intent_id = intent.id

        async def override_get_db():
            async with self.sessions() as db:
                yield db

        async def scan_submitted_payment(_since):
            return [transaction]

        original_demo_auth = mint_app.ALLOW_DEMO_AUTH
        mint_app.app.dependency_overrides[mint_app.get_db] = override_get_db
        mint_app.ALLOW_DEMO_AUTH = True
        try:
            transport = httpx.ASGITransport(app=mint_app.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                with patch.object(
                    mint_app,
                    "scan_ton_transactions",
                    side_effect=scan_submitted_payment,
                ):
                    response = await client.post(
                        f"/api/payment-intents/{intent_id}/validate",
                        headers={"X-Demo-User-ID": "74"},
                        json={"transaction_boc": "te6ccg=="},
                    )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "confirmed")
            self.assertEqual(response.json()["tx_hash"], transaction_hash)
            async with self.sessions() as db:
                confirmed_intent = await db.get(PaymentIntent, intent_id)
                self.assertEqual(confirmed_intent.status, "confirmed")
                self.assertEqual(confirmed_intent.tx_hash, transaction_hash)
        finally:
            mint_app.ALLOW_DEMO_AUTH = original_demo_auth
            mint_app.app.dependency_overrides.pop(mint_app.get_db, None)

    async def test_user_1_buys_45_ton_server_after_user_2_refers_them(self) -> None:
        original_demo_auth = mint_app.ALLOW_DEMO_AUTH

        async def override_get_db():
            async with self.sessions() as db:
                yield db

        mint_app.app.dependency_overrides[mint_app.get_db] = override_get_db
        mint_app.ALLOW_DEMO_AUTH = True
        try:
            with patch.dict(
                os.environ,
                {
                    "REFERRAL_SALT": "api-payment-flow-test-salt-0123456789abcdef",
                    "TELEGRAM_BOT_USERNAME": "mintai_test_bot",
                },
            ):
                transport = httpx.ASGITransport(app=mint_app.app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    root_response = await client.post(
                        "/api/register",
                        headers={"X-Demo-User-ID": "2"},
                        json={"wallet_address": "0Q" + "A" * 46},
                    )
                    self.assertEqual(root_response.status_code, 200)
                    referral_code = root_response.json()["referral_code"]

                    referred_response = await client.post(
                        "/api/register",
                        headers={"X-Demo-User-ID": "1"},
                        json={"referral_code": referral_code},
                    )
                    self.assertEqual(referred_response.status_code, 200)

                    intent_response = await client.post(
                        "/api/payment-intents",
                        headers={
                            "X-Demo-User-ID": "1",
                            "Idempotency-Key": "api-payment-flow-0001",
                        },
                        json={"server_id": "h100-node-01"},
                    )
                    self.assertEqual(intent_response.status_code, 201)
                    intent_payload = intent_response.json()
                    intent_id = intent_payload["intent_id"]
                    memo = intent_payload["crypto"]["memo"]

                    async with self.sessions() as db:
                        self.assertEqual(
                            await confirm_ton_transactions(
                                db,
                                [
                                    {
                                        "tx_hash": "9" * 64,
                                        "amount_nano": "4500000000",
                                        "comment": memo,
                                    }
                                ],
                            ),
                            1,
                        )

                    status_response = await client.get(
                        f"/api/payment-intents/{intent_id}",
                        headers={"X-Demo-User-ID": "1"},
                    )
                    self.assertEqual(status_response.status_code, 200)
                    self.assertEqual(status_response.json()["status"], "confirmed")

                    investment_response = await client.post(
                        "/api/investments",
                        headers={"X-Demo-User-ID": "1"},
                        json={"intent_id": intent_id},
                    )
                    self.assertEqual(investment_response.status_code, 200)
                    self.assertTrue(investment_response.json()["already_confirmed"])

                    dashboard_response = await client.get(
                        "/api/dashboard", headers={"X-Demo-User-ID": "1"}
                    )
                    self.assertEqual(dashboard_response.status_code, 200)
                    self.assertEqual(len(dashboard_response.json()["slots"]), 1)
                    self.assertEqual(
                        dashboard_response.json()["slots"][0]["gross_deposit"],
                        "4.50000000",
                    )

                    root_dashboard_response = await client.get(
                        "/api/dashboard", headers={"X-Demo-User-ID": "2"}
                    )
                    self.assertEqual(root_dashboard_response.status_code, 200)
                    self.assertEqual(
                        root_dashboard_response.json()["user"]["qualified_referrals"],
                        1,
                    )
                    self.assertEqual(
                        root_dashboard_response.json()["balances"][0]["withdrawable"],
                        "1.35000000",
                    )

                    async with self.sessions() as db:
                        payment = await db.get(PaymentIntent, intent_id)
                        investor = await db.get(User, 1)
                        referrer = await db.get(User, 2)
                        slot = await db.scalar(
                            select(ServerSlot).where(
                                ServerSlot.payment_intent_id == intent_id
                            )
                        )
                        investor_balance = await db.scalar(
                            select(WalletBalance).where(
                                WalletBalance.user_id == 1,
                                WalletBalance.currency == "TON",
                            )
                        )
                        referrer_balance = await db.scalar(
                            select(WalletBalance).where(
                                WalletBalance.user_id == 2,
                                WalletBalance.currency == "TON",
                            )
                        )
                        self.assertEqual(payment.status, "confirmed")
                        self.assertEqual(payment.expected_amount, Decimal("4.50000000"))
                        self.assertEqual(payment.chain, "TON")
                        self.assertEqual(payment.provider, "ton")
                        self.assertEqual(payment.tx_hash, "9" * 64)
                        self.assertEqual(investor.referred_by, 2)
                        self.assertTrue(investor.referral_commission_paid)
                        self.assertFalse(referrer.referral_commission_paid)
                        self.assertEqual(slot.net_investment, Decimal("3.15000000"))
                        self.assertIsNone(investor_balance)
                        self.assertEqual(
                            referrer_balance.withdrawable_amount,
                            Decimal("1.35000000"),
                        )

                    with patch.object(
                        mint_app,
                        "get_payout_adapter",
                        return_value=SimulatedPayoutAdapter(),
                    ):
                        quote_response = await client.post(
                            "/api/withdrawal-quotes",
                            headers={"X-Demo-User-ID": "2"},
                            json={"amount": "1", "currency": "TON"},
                        )
                    self.assertEqual(quote_response.status_code, 201)
                    quote = quote_response.json()
                    self.assertEqual(quote["platform_fee"], "0.10000000")
                    self.assertEqual(
                        Decimal(quote["payout_amount"]),
                        Decimal("1")
                        - Decimal(quote["platform_fee"])
                        - Decimal(quote["network_fee_estimate"]),
                    )

                    withdrawal_response = await client.post(
                        "/api/withdrawals",
                        headers={
                            "X-Demo-User-ID": "2",
                            "Idempotency-Key": "api-withdraw-flow-0001",
                        },
                        json={"quote_id": quote["quote_id"]},
                    )
                    self.assertEqual(withdrawal_response.status_code, 202)
                    withdrawal_id = withdrawal_response.json()["withdrawal_id"]

                    async with self.sessions() as db:
                        self.assertEqual(
                            await process_approved_withdrawals(
                                db, SimulatedPayoutAdapter()
                            ),
                            1,
                        )

                    withdrawal_status = await client.get(
                        f"/api/withdrawals/{withdrawal_id}",
                        headers={"X-Demo-User-ID": "2"},
                    )
                    self.assertEqual(withdrawal_status.status_code, 200)
                    self.assertEqual(
                        withdrawal_status.json()["status"], "completed"
                    )
                    self.assertTrue(
                        withdrawal_status.json()["tx_hash"].startswith("sim-")
                    )
                    final_balance = await client.get(
                        "/api/dashboard", headers={"X-Demo-User-ID": "2"}
                    )
                    self.assertEqual(
                        final_balance.json()["balances"][0]["withdrawable"],
                        "0.35000000",
                    )
        finally:
            mint_app.ALLOW_DEMO_AUTH = original_demo_auth
            mint_app.app.dependency_overrides.pop(mint_app.get_db, None)

    async def test_referred_investor_can_refer_and_only_pays_direct_commission(self) -> None:
        async with self.sessions() as db:
            await self.add_user(db, 80)
            referred_investor = await self.add_user(db, 81, referred_by=80)
            downstream_investor = await self.add_user(
                db, 82, referred_by=referred_investor.id
            )

            transfers = []
            payment_intents = []
            for index, investor in enumerate(
                (referred_investor, downstream_investor), start=1
            ):
                intent = await create_payment_intent(
                    db,
                    user_id=investor.id,
                    server_id="h100-node-01",
                    idempotency_key=f"referral-chain-{index:02d}",
                )
                payment_intents.append(intent)
                transfers.append(
                    {
                        "tx_hash": f"{index:064x}",
                        "amount_nano": "4500000000",
                        "comment": intent.deposit_memo,
                    }
                )

            self.assertEqual(await confirm_ton_transactions(db, transfers), 2)
            self.assertEqual(
                await db.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.entry_type == "COMMISSION",
                        LedgerEntry.user_id == 80,
                    )
                ),
                1,
            )
            self.assertEqual(
                await db.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.entry_type == "COMMISSION",
                        LedgerEntry.user_id == referred_investor.id,
                    )
                ),
                1,
            )
            root_commission_from_downstream = await db.scalar(
                select(func.count(LedgerEntry.id)).where(
                    LedgerEntry.entry_type == "COMMISSION",
                    LedgerEntry.user_id == 80,
                    LedgerEntry.reference_id == payment_intents[1].id,
                )
            )
            self.assertEqual(root_commission_from_downstream, 0)
            root_commission_from_direct = await db.scalar(
                select(func.count(LedgerEntry.id)).where(
                    LedgerEntry.entry_type == "COMMISSION",
                    LedgerEntry.user_id == 80,
                    LedgerEntry.reference_id == payment_intents[0].id,
                )
            )
            self.assertEqual(root_commission_from_direct, 1)

            root_qualified, _ = await sync_referral_rewards(db, 80)
            referred_qualified, _ = await sync_referral_rewards(
                db, referred_investor.id
            )
            self.assertEqual(root_qualified, 1)
            self.assertEqual(referred_qualified, 1)

    async def test_forty_and_fifty_live_confirmed_referrals_unlock_principal_once(self) -> None:
        async with self.sessions() as db:
            await self.add_user(db, 90)
            root_intent = await create_payment_intent(
                db,
                user_id=90,
                server_id="h100-node-01",
                idempotency_key="forty-referrals-root",
            )
            self.assertEqual(
                await confirm_ton_transactions(
                    db,
                    [
                        {
                            "tx_hash": "f" * 64,
                            "amount_nano": "4500000000",
                            "comment": root_intent.deposit_memo,
                        }
                    ],
                ),
                1,
            )

            referral_transfers = []
            for index, user_id in enumerate(range(100, 150), start=1):
                await self.add_user(db, user_id, referred_by=90)
                intent = await create_payment_intent(
                    db,
                    user_id=user_id,
                    server_id="h100-node-01",
                    idempotency_key=f"fifty-referrals-{index:03d}",
                )
                referral_transfers.append(
                    {
                        "tx_hash": f"{index:064x}",
                        "amount_nano": "4500000000",
                        "comment": intent.deposit_memo,
                    }
                )

            self.assertEqual(
                await confirm_ton_transactions(db, referral_transfers[:40]), 40
            )
            qualified, milestones = await sync_referral_rewards(db, 90)
            root_slot = await db.scalar(
                select(ServerSlot).where(
                    ServerSlot.payment_intent_id == root_intent.id
                )
            )
            self.assertEqual(qualified, 40)
            self.assertEqual(len(milestones), 8)
            self.assertIsNone(root_slot.principal_unlocked_at)
            self.assertIsNone(
                await db.scalar(
                    select(LedgerEntry.id).where(
                        LedgerEntry.idempotency_key
                        == f"principal-unlock:{root_slot.id}"
                    )
                )
            )

            self.assertEqual(
                await confirm_ton_transactions(db, referral_transfers[40:]), 10
            )
            qualified, milestones = await sync_referral_rewards(db, 90)
            await db.commit()
            await db.refresh(root_slot)
            root_balance = await db.scalar(
                select(WalletBalance).where(
                    WalletBalance.user_id == 90, WalletBalance.currency == "TON"
                )
            )
            unlock_count = await db.scalar(
                select(func.count(LedgerEntry.id)).where(
                    LedgerEntry.idempotency_key
                    == f"principal-unlock:{root_slot.id}"
                )
            )
            self.assertEqual(qualified, 50)
            self.assertEqual(len(milestones), 10)
            self.assertIsNotNone(root_slot.principal_unlocked_at)
            self.assertEqual(root_balance.withdrawable_amount, Decimal("72.00000000"))
            self.assertEqual(unlock_count, 1)

            await sync_referral_rewards(db, 90)
            await db.commit()
            self.assertEqual(
                await db.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.idempotency_key
                        == f"principal-unlock:{root_slot.id}"
                    )
                ),
                1,
            )

    async def test_wrong_comment_underpayment_and_expired_payment_do_not_confirm(self) -> None:
        async with self.sessions() as db:
            await self.add_user(db, 91)
            intent = await create_payment_intent(
                db,
                user_id=91,
                server_id="h100-node-01",
                idempotency_key="invalid-ton-payment-case",
            )
            invalid_transfers = [
                {
                    "tx_hash": "1" * 64,
                    "amount_nano": "4500000000",
                    "comment": "MINT-WRONG",
                },
                {
                    "tx_hash": "2" * 64,
                    "amount_nano": "4499999999",
                    "comment": intent.deposit_memo,
                },
            ]
            self.assertEqual(await confirm_ton_transactions(db, invalid_transfers), 0)
            await db.refresh(intent)
            self.assertEqual(intent.status, "awaiting_payment")

            intent.expires_at = utc_now() - timedelta(seconds=1)
            await db.commit()
            self.assertEqual(
                await confirm_ton_transactions(
                    db,
                    [
                        {
                            "tx_hash": "3" * 64,
                            "amount_nano": "4500000000",
                            "comment": intent.deposit_memo,
                        }
                    ],
                ),
                0,
            )
            self.assertEqual(await expire_stale_payment_intents(db), 1)
            await db.refresh(intent)
            self.assertEqual(intent.status, "expired")


class ExternalAuthenticationBugHunt(unittest.TestCase):
    def test_referral_codes_are_salted_and_links_support_mini_apps(self) -> None:
        with patch.dict(
            os.environ,
            {
                "REFERRAL_SALT": "s" * 48,
                "TELEGRAM_BOT_USERNAME": "@mintai_bot",
                "TELEGRAM_MINI_APP_SHORT_NAME": "dashboard",
            },
        ):
            first = generate_user_referral_code(12345)
            retry = generate_user_referral_code(12345, attempt=1)
            self.assertEqual(len(first), 6)
            self.assertNotEqual(first, retry)
            self.assertEqual(
                build_referral_link(first),
                f"https://t.me/mintai_bot/dashboard?startapp={first}",
            )

    def test_catalog_uses_fixed_ton_prices_only(self) -> None:
        self.assertEqual(
            [price_tier(tier["id"]) for tier in PRICING_TIERS],
            [
                Decimal("4.50000000"),
                Decimal("15.00000000"),
                Decimal("150.00000000"),
                Decimal("1500.00000000"),
                Decimal("15000.00000000"),
                Decimal("150000.00000000"),
            ],
        )

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

    def test_ton_transfer_requires_exact_comment_and_amount(self) -> None:
        class Intent:
            deposit_memo = "MINT-STAGING"
            expected_amount = Decimal("4.5")

        transactions = [
            {"tx_hash": "a" * 64, "amount_nano": "4500000000", "comment": "OTHER"},
            {"tx_hash": "b" * 64, "amount_nano": "4499999999", "comment": "MINT-STAGING"},
            {"tx_hash": "c" * 64, "amount_nano": "4500000000", "comment": "MINT-STAGING"},
        ]
        self.assertEqual(
            find_matching_transaction(Intent(), transactions), transactions[-1]
        )
        self.assertIsNone(
            find_matching_transaction(
                Intent(), [transactions[0], transactions[1]]
            )
        )
        bounced = {
            "tx_hash": "d" * 64,
            "amount_nano": "4500000000",
            "comment": "MINT-STAGING",
            "incoming_bounced": False,
            "bounce_output": True,
        }
        self.assertIsNone(find_matching_transaction(Intent(), [bounced]))

if __name__ == "__main__":
    unittest.main()
