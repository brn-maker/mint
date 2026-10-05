import os
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    AMOUNT_QUANTUM,
    REFERRAL_COMMISSION_RATE,
    STARS_CLEARANCE_PERIOD,
    WITHDRAWAL_QUOTE_TTL_SECONDS,
    WITHDRAWAL_FEE_RATE,
)
from dividends import DAILY_DIVIDEND_RATES, accrued_dividend, utc_midnights_after
from ledger import (
    LedgerError,
    apply_ledger_delta,
    apply_treasury_ledger_delta,
    record_financial_event,
)
from mock_data import NETWORK_UPTIME_PERCENT, SERVER_POOLS
from models import (
    PaymentIntent,
    ReferralDividendMilestone,
    ServerSlot,
    User,
    WalletBalance,
    WithdrawalQuote,
    WithdrawalRequest,
    utc_now,
)
from payments.payouts import (
    NetworkFeeQuote,
    PayoutAdapter,
    PayoutPending,
    PayoutRejected,
)

logger = logging.getLogger(__name__)


def _currency_for(payment_method: str, currency: str | None) -> str:
    normalized_method = payment_method.lower()
    selected_currency = (currency or ("XTR" if normalized_method == "stars" else "USDT")).upper()
    if normalized_method == "stars" and selected_currency != "XTR":
        raise ValueError("Stars investments must use XTR")
    if normalized_method == "crypto" and selected_currency not in {"TON", "USDT"}:
        raise ValueError("Crypto investments must use TON or USDT")
    if normalized_method not in {"stars", "crypto"}:
        raise ValueError("payment_method must be 'stars' or 'crypto'")
    return selected_currency


async def confirm_investment_from_payment(
    db: AsyncSession,
    intent_id: str,
    *,
    external_payment_id: str | None = None,
    tx_hash: str | None = None,
    tx_log_index: int | None = None,
    provider: str | None = None,
) -> dict[str, object]:
    intent = (
        await db.execute(
            select(PaymentIntent)
            .where(PaymentIntent.id == intent_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if intent is None:
        raise ValueError("Payment intent not found")
    if intent.status == "confirmed":
        if external_payment_id and intent.external_payment_id != external_payment_id:
            raise ValueError("Intent was already confirmed by a different provider payment")
        if tx_hash and (
            intent.tx_hash != tx_hash or intent.tx_log_index != tx_log_index
        ):
            raise ValueError("Intent was already confirmed by a different chain transaction")
        slot = await db.scalar(
            select(ServerSlot).where(ServerSlot.payment_intent_id == intent.id)
        )
        return {
            "intent_id": intent.id,
            "slot_id": slot.id if slot else None,
            "status": "confirmed",
            "already_confirmed": True,
        }
    if intent.status not in {"awaiting_payment", "pending"}:
        raise ValueError(f"Payment intent is not payable (status={intent.status})")

    now = utc_now()
    if _as_utc(intent.expires_at) <= now:
        intent.status = "expired"
        await db.commit()
        raise ValueError("Payment intent has expired")

    user_id = intent.user_id
    amount = Decimal(intent.expected_amount).quantize(AMOUNT_QUANTUM, rounding=ROUND_DOWN)
    payment_method = intent.payment_method
    selected_currency = _currency_for(payment_method, intent.currency)
    clearance_ready_at = now + STARS_CLEARANCE_PERIOD if payment_method == "stars" else now

    try:
        user = await db.get(User, user_id)
        if user is None:
            raise ValueError("User is not registered")

        intent.status = "confirmed"
        intent.confirmed_at = now
        if external_payment_id:
            intent.external_payment_id = external_payment_id
        if tx_hash:
            intent.tx_hash = tx_hash
            intent.tx_log_index = tx_log_index
        if provider:
            intent.provider = provider

        commissions = []
        total_commissions = Decimal("0")
        trigger_result = await db.execute(
            update(User)
            .where(User.id == user_id, User.referral_commission_paid.is_(False))
            .values(referral_commission_paid=True)
        )
        first_investment = trigger_result.rowcount == 1
        if first_investment:
            referrer_id = user.referred_by
            if referrer_id is not None:
                referrer = await db.get(User, referrer_id)
                if referrer is not None:
                    commission = (amount * REFERRAL_COMMISSION_RATE).quantize(
                        AMOUNT_QUANTUM, rounding=ROUND_DOWN
                    )
                    if commission > 0:
                        await apply_ledger_delta(
                            db,
                            user_id=referrer.id,
                            currency=selected_currency,
                            amount=commission,
                            entry_type="COMMISSION",
                            reference_type="payment_intent",
                            reference_id=intent.id,
                            idempotency_key=f"commission:{intent.id}",
                            telemetry={"commissions_paid": commission},
                        )
                        total_commissions += commission
                        commissions.append({"user_id": referrer.id, "amount": str(commission)})

        slot = ServerSlot(
            user_id=user_id,
            payment_intent_id=intent.id,
            server_id=intent.server_id,
            pricing_tier_id=intent.pricing_tier_id,
            server_pool=intent.server_pool,
            payment_method=payment_method,
            currency=selected_currency,
            gross_deposit=amount,
            net_investment=amount - total_commissions,
            clearance_ready_at=clearance_ready_at,
            status="active",
            is_active=True,
            created_at=now,
            last_dividend_at=now,
        )
        db.add(slot)
        await sync_referral_rewards(db, user_id, now)
        if first_investment and user.referred_by is not None:
            await sync_referral_rewards(db, user.referred_by, now)
        await record_financial_event(
            db,
            user_id=user_id,
            event_type="INVESTMENT",
            currency=selected_currency,
            gross_amount=amount,
            reference_type="payment_intent",
            reference_id=intent.id,
        )
        await db.commit()
        return {
            "intent_id": intent.id,
            "slot_id": slot.id,
            "gross_deposit": str(amount),
            "currency": selected_currency,
            "clearance_ready_at": clearance_ready_at.isoformat(),
            "commissions": commissions,
            "status": "confirmed",
        }
    except LedgerError:
        await db.rollback()
        raise ValueError("Unable to record investment ledger entries") from None
    except Exception:
        await db.rollback()
        raise


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def sync_referral_rewards(
    db: AsyncSession, user_id: int, now: datetime | None = None
) -> tuple[int, list[ReferralDividendMilestone]]:
    now = _as_utc(now or utc_now())
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    qualified_count = await db.scalar(
        select(func.count(func.distinct(User.id)))
        .join(PaymentIntent, PaymentIntent.user_id == User.id)
        .where(User.referred_by == user_id, PaymentIntent.status == "confirmed")
    )
    qualified_count = int(qualified_count or 0)

    milestones_result = await db.execute(
        select(ReferralDividendMilestone)
        .where(ReferralDividendMilestone.user_id == user_id)
        .order_by(ReferralDividendMilestone.referral_threshold)
    )
    milestones = list(milestones_result.scalars())
    existing_thresholds = {item.referral_threshold for item in milestones}
    next_start = max(
        (_as_utc(item.ends_at) for item in milestones),
        default=now,
    )
    for threshold in range(5, qualified_count + 1, 5):
        if threshold in existing_thresholds:
            continue
        starts_at = max(now, next_start)
        dialect = db.bind.dialect.name if db.bind else ""
        insert = (
            sqlite_insert(ReferralDividendMilestone)
            if dialect == "sqlite"
            else postgresql_insert(ReferralDividendMilestone)
            if dialect == "postgresql"
            else None
        )
        if insert is None:
            raise RuntimeError(f"Unsupported database dialect: {dialect}")
        ends_at = starts_at + timedelta(days=5)
        await db.execute(
            insert.values(
                user_id=user_id,
                referral_threshold=threshold,
                starts_at=starts_at,
                ends_at=ends_at,
                created_at=now,
            ).on_conflict_do_nothing(
                index_elements=["user_id", "referral_threshold"]
            )
        )
        existing_thresholds.add(threshold)
        next_start = ends_at
    if qualified_count >= 50:
        slots_result = await db.execute(
            select(ServerSlot).where(
                ServerSlot.user_id == user_id,
                ServerSlot.principal_unlocked_at.is_(None),
            )
        )
        for slot in slots_result.scalars():
            await apply_ledger_delta(
                db,
                user_id=user_id,
                currency=slot.currency,
                amount=slot.net_investment,
                entry_type="PRINCIPAL_UNLOCK",
                reference_type="server_slot",
                reference_id=slot.id,
                idempotency_key=f"principal-unlock:{slot.id}",
            )
            slot.principal_unlocked_at = now
    await db.flush()
    current_milestones = await db.execute(
        select(ReferralDividendMilestone)
        .where(ReferralDividendMilestone.user_id == user_id)
        .order_by(ReferralDividendMilestone.referral_threshold)
    )
    return qualified_count, list(current_milestones.scalars())


async def accrue_dividends(db: AsyncSession, user: User) -> None:
    now = utc_now()
    _, milestones = await sync_referral_rewards(db, user.id, now)
    result = await db.execute(
        select(ServerSlot).where(
            ServerSlot.user_id == user.id,
            ServerSlot.is_active.is_(True),
            ServerSlot.status == "active",
        )
    )
    total_earned = Decimal("0")
    for slot in result.scalars():
        start = _as_utc(slot.last_dividend_at)
        elapsed_seconds = max(0, (now - start).total_seconds())
        if elapsed_seconds <= 0:
            continue
        dividend_entries: list[tuple[datetime | None, Decimal]] = []
        if slot.server_id in DAILY_DIVIDEND_RATES:
            earned = Decimal("0")
            for payout_at in utc_midnights_after(start, now):
                if any(
                    _as_utc(milestone.starts_at) <= payout_at < _as_utc(milestone.ends_at)
                    for milestone in milestones
                ):
                    daily_amount = accrued_dividend(
                        slot.server_id, slot.net_investment, 86400
                    )
                    if daily_amount <= 0:
                        continue
                    dividend_entries.append((payout_at, daily_amount))
                    earned += daily_amount
        else:
            annual_yield = SERVER_POOLS[slot.server_pool]["annual_yield"]
            uptime_factor = NETWORK_UPTIME_PERCENT / Decimal("100")
            earned = (
                slot.net_investment * annual_yield * uptime_factor * Decimal(str(elapsed_seconds))
                / Decimal("31536000")
            ).quantize(AMOUNT_QUANTUM, rounding=ROUND_DOWN)
            if earned > 0:
                dividend_entries.append((None, earned))
        claim = await db.execute(
            update(ServerSlot)
            .where(
                ServerSlot.id == slot.id,
                ServerSlot.last_dividend_at == slot.last_dividend_at,
            )
            .values(last_dividend_at=now)
        )
        if claim.rowcount != 1:
            continue
        slot.last_dividend_at = now
        for payout_at, amount in dividend_entries:
            idempotency_key = (
                f"dividend:{slot.id}:{payout_at.strftime('%Y-%m-%d')}"
                if payout_at is not None
                else f"dividend:{slot.id}:{start.isoformat()}:{now.isoformat()}"
            )
            await apply_ledger_delta(
                db,
                user_id=user.id,
                currency=slot.currency,
                amount=amount,
                entry_type="DIVIDEND",
                reference_type="server_slot",
                reference_id=slot.id,
                idempotency_key=idempotency_key,
            )
        total_earned += earned
    if total_earned > 0:
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(accumulated_dividends=User.accumulated_dividends + total_earned)
        )
    await db.commit()
    if total_earned > 0:
        await db.refresh(user, attribute_names=["accumulated_dividends"])


async def accrue_dividends_batch(db: AsyncSession, limit: int = 200) -> int:
    result = await db.execute(
        select(User.id)
        .join(ServerSlot, ServerSlot.user_id == User.id)
        .where(ServerSlot.is_active.is_(True), ServerSlot.status == "active")
        .distinct()
        .limit(limit)
    )
    user_ids = [row[0] for row in result.all()]
    processed = 0
    for user_id in user_ids:
        user = await db.get(User, user_id)
        if user is None:
            continue
        await accrue_dividends(db, user)
        processed += 1
    return processed


async def quote_withdrawal(
    db: AsyncSession,
    user_id: int,
    amount: Decimal,
    currency: str,
    *,
    payout_adapter: PayoutAdapter,
) -> dict[str, object]:
    raw_amount = Decimal(amount)
    if not raw_amount.is_finite() or raw_amount <= 0:
        raise ValueError("Withdrawal amount must be a finite positive number")
    requested_amount = raw_amount.quantize(AMOUNT_QUANTUM, rounding=ROUND_DOWN)
    selected_currency = currency.upper()
    if selected_currency not in {"XTR", "TON", "USDT"}:
        raise ValueError("Unsupported withdrawal currency")
    if requested_amount <= 0:
        raise ValueError("Withdrawal amount is below supported precision")

    user = await db.get(User, user_id)
    if user is None:
        raise ValueError("User is not registered")
    now = utc_now()
    slots_result = await db.execute(
        select(ServerSlot).where(
            ServerSlot.user_id == user_id,
            ServerSlot.clearance_ready_at > now,
        )
    )
    locked_slots = list(slots_result.scalars())
    if locked_slots:
        next_clearance = min(_as_utc(slot.clearance_ready_at) for slot in locked_slots)
        raise ValueError(
            f"Withdrawals are locked until {next_clearance.isoformat()} while Stars slots clear"
        )
    if not user.wallet_address:
        raise ValueError("Set a wallet address before withdrawing")
    balance = await db.scalar(
        select(WalletBalance).where(
            WalletBalance.user_id == user_id,
            WalletBalance.currency == selected_currency,
        )
    )
    if balance is None or balance.withdrawable_amount < requested_amount:
        raise ValueError(f"Insufficient {selected_currency} withdrawable balance")

    fee = (requested_amount * WITHDRAWAL_FEE_RATE).quantize(
        AMOUNT_QUANTUM, rounding=ROUND_DOWN
    )
    if fee <= 0:
        raise ValueError(
            "Withdrawal amount is too small to apply the 10% fee at supported precision"
        )
    fee_quote = (
        NetworkFeeQuote(
            native_fee_amount=Decimal("0"),
            withdrawal_fee_amount=Decimal("0"),
            withdrawal_currency=selected_currency,
        )
        if selected_currency == "XTR"
        else await payout_adapter.quote_network_fee(
            selected_currency, requested_amount - fee
        )
    )
    if (
        fee_quote.withdrawal_currency != selected_currency
        or not isinstance(fee_quote.native_fee_amount, Decimal)
        or not isinstance(fee_quote.withdrawal_fee_amount, Decimal)
        or not fee_quote.native_fee_amount.is_finite()
        or not fee_quote.withdrawal_fee_amount.is_finite()
        or fee_quote.native_fee_amount < 0
        or fee_quote.withdrawal_fee_amount < 0
        or (selected_currency == "USDT" and (
            fee_quote.ton_usd_rate is None
            or not isinstance(fee_quote.ton_usd_rate, Decimal)
            or not fee_quote.ton_usd_rate.is_finite()
            or fee_quote.ton_usd_rate <= 0
        ))
    ):
        raise ValueError("Payout provider returned an invalid network-fee quote")
    native_fee_estimate = fee_quote.native_fee_amount.quantize(
        AMOUNT_QUANTUM, rounding=ROUND_DOWN
    )
    network_fee_estimate = fee_quote.withdrawal_fee_amount.quantize(
        AMOUNT_QUANTUM, rounding=ROUND_DOWN
    )
    payout = requested_amount - fee - network_fee_estimate
    if payout <= 0:
        raise ValueError("Withdrawal amount does not cover the platform and network fees")
    quote = WithdrawalQuote(
        user_id=user_id,
        currency=selected_currency,
        gross_amount=requested_amount,
        platform_fee=fee,
        network_fee_estimate=network_fee_estimate,
        native_network_fee_estimate=native_fee_estimate,
        network_fee_currency="TON" if selected_currency != "XTR" else None,
        ton_usd_rate=fee_quote.ton_usd_rate,
        payout_amount=payout,
        expires_at=now + timedelta(seconds=WITHDRAWAL_QUOTE_TTL_SECONDS),
    )
    db.add(quote)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    await db.refresh(quote)
    return {
        "quote_id": quote.id,
        "currency": quote.currency,
        "gross_amount": str(quote.gross_amount),
        "platform_fee": str(quote.platform_fee),
        "network_fee_estimate": str(quote.network_fee_estimate),
        "native_network_fee_estimate": str(quote.native_network_fee_estimate),
        "network_fee_currency": quote.network_fee_currency,
        "ton_usd_rate": str(quote.ton_usd_rate) if quote.ton_usd_rate else None,
        "payout_amount": str(quote.payout_amount),
        "manual_settlement": quote.currency == "XTR",
        "expires_at": _as_utc(quote.expires_at).isoformat(),
    }


async def request_withdrawal(
    db: AsyncSession,
    user_id: int,
    quote_id: str,
    *,
    idempotency_key: str,
) -> dict[str, object]:
    prior = await db.scalar(
        select(WithdrawalRequest).where(
            WithdrawalRequest.idempotency_key == idempotency_key
        )
    )
    if prior is not None:
        if prior.user_id != user_id or prior.quote_id != quote_id:
            raise ValueError("Idempotency key was already used for a different withdrawal")
        return _withdrawal_payload(prior)

    try:
        quote = (
            await db.execute(
                select(WithdrawalQuote)
                .where(WithdrawalQuote.id == quote_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if quote is None or quote.user_id != user_id:
            raise ValueError("Withdrawal quote not found")
        if quote.withdrawal_id is not None:
            raise ValueError("Withdrawal quote has already been used")
        now = utc_now()
        if _as_utc(quote.expires_at) <= now:
            raise ValueError("Withdrawal quote has expired; request a new quote")

        user = await db.get(User, user_id)
        if user is None:
            raise ValueError("User is not registered")
        if not user.wallet_address:
            raise ValueError("Set a wallet address before withdrawing")
        locked_slot = await db.scalar(
            select(ServerSlot.id).where(
                ServerSlot.user_id == user_id,
                ServerSlot.clearance_ready_at > now,
            ).limit(1)
        )
        if locked_slot is not None:
            raise ValueError("Withdrawals are locked while a Stars slot is clearing")

        withdrawal = WithdrawalRequest(
            user_id=user_id,
            quote_id=quote.id,
            currency=quote.currency,
            gross_amount=quote.gross_amount,
            platform_fee=quote.platform_fee,
            payout_amount=quote.payout_amount,
            network_fee_estimate=quote.network_fee_estimate,
            native_network_fee_estimate=quote.native_network_fee_estimate,
            network_fee_currency=quote.network_fee_currency,
            ton_usd_rate=quote.ton_usd_rate,
            wallet_address_snapshot=user.wallet_address,
            status="manual_review" if quote.currency == "XTR" else "queued",
            idempotency_key=idempotency_key,
        )
        db.add(withdrawal)
        await db.flush()
        quote.withdrawal_id = withdrawal.id
        await apply_ledger_delta(
            db,
            user_id=user_id,
            currency=quote.currency,
            amount=-quote.gross_amount,
            entry_type="WITHDRAWAL",
            reference_type="withdrawal_request",
            reference_id=withdrawal.id,
            idempotency_key=f"withdrawal:debit:{withdrawal.id}",
        )
        await apply_treasury_ledger_delta(
            db,
            currency=quote.currency,
            amount=quote.platform_fee,
            entry_type="WITHDRAWAL_FEE",
            reference_type="withdrawal_request",
            reference_id=withdrawal.id,
            idempotency_key=f"withdrawal:fee:{withdrawal.id}",
            telemetry={"platform_fees_collected": quote.platform_fee},
        )
        await db.commit()
        await db.refresh(withdrawal)
        return _withdrawal_payload(withdrawal)
    except LedgerError as exc:
        await db.rollback()
        raise ValueError(str(exc)) from exc
    except IntegrityError:
        await db.rollback()
        prior = await db.scalar(
            select(WithdrawalRequest).where(
                WithdrawalRequest.idempotency_key == idempotency_key
            )
        )
        if prior is None:
            raise
        if prior.user_id != user_id or prior.quote_id != quote_id:
            raise ValueError("Idempotency key was already used for a different withdrawal") from None
        return _withdrawal_payload(prior)
    except Exception:
        await db.rollback()
        raise


def _withdrawal_payload(withdrawal: WithdrawalRequest) -> dict[str, object]:
    return {
        "withdrawal_id": withdrawal.id,
        "quote_id": withdrawal.quote_id,
        "currency": withdrawal.currency,
        "gross_amount": str(withdrawal.gross_amount),
        "platform_fee": str(withdrawal.platform_fee),
        "network_fee_estimate": str(withdrawal.network_fee_estimate),
        "native_network_fee_estimate": str(
            withdrawal.native_network_fee_estimate
        ),
        "network_fee_amount": (
            str(withdrawal.network_fee_amount)
            if withdrawal.network_fee_amount is not None
            else None
        ),
        "network_fee_currency": withdrawal.network_fee_currency,
        "ton_usd_rate": (
            str(withdrawal.ton_usd_rate) if withdrawal.ton_usd_rate else None
        ),
        "payout_amount": str(withdrawal.payout_amount),
        "settlement_currency": withdrawal.settlement_currency,
        "settlement_amount": (
            str(withdrawal.settlement_amount)
            if withdrawal.settlement_amount is not None
            else None
        ),
        "settlement_rate": (
            str(withdrawal.settlement_rate)
            if withdrawal.settlement_rate is not None
            else None
        ),
        "wallet_address": withdrawal.wallet_address_snapshot,
        "treasury_account": os.getenv("TREASURY_ACCOUNT", "Mint AI Treasury"),
        "tx_hash": withdrawal.tx_hash,
        "status": withdrawal.status,
        "error_message": withdrawal.error_message,
    }


async def execute_withdrawal(
    db: AsyncSession,
    user_id: int,
    quote_id: str,
    *,
    idempotency_key: str,
) -> dict[str, object]:
    return await request_withdrawal(
        db,
        user_id,
        quote_id,
        idempotency_key=idempotency_key,
    )


async def process_approved_withdrawals(
    db: AsyncSession, payout_adapter: PayoutAdapter, limit: int = 20
) -> int:
    stale_before = utc_now() - timedelta(
        seconds=int(os.getenv("WITHDRAWAL_PROCESSING_STALE_SECONDS", "300"))
    )
    result = await db.execute(
        select(WithdrawalRequest.id)
        .where(
            (WithdrawalRequest.status == "queued")
            | (
                (WithdrawalRequest.status == "processing")
                & (WithdrawalRequest.updated_at < stale_before)
            )
        )
        .order_by(WithdrawalRequest.created_at)
        .limit(limit)
    )
    processed = 0
    for withdrawal_id in result.scalars():
        claim = await db.execute(
            update(WithdrawalRequest)
            .where(
                WithdrawalRequest.id == withdrawal_id,
                (WithdrawalRequest.status == "queued")
                | (
                    (WithdrawalRequest.status == "processing")
                    & (WithdrawalRequest.updated_at < stale_before)
                ),
            )
            .values(status="processing", updated_at=utc_now())
        )
        if claim.rowcount != 1:
            await db.rollback()
            continue
        await db.commit()
        withdrawal = await db.get(WithdrawalRequest, withdrawal_id)
        if withdrawal is None:
            continue
        try:
            payout = await payout_adapter.send(
                withdrawal, idempotency_key=f"withdrawal:{withdrawal.id}"
            )
        except PayoutPending as exc:
            logger.warning("Withdrawal %s awaits payout reconciliation: %s", withdrawal.id, exc)
            continue
        except PayoutRejected as exc:
            await db.rollback()
            withdrawal = await db.get(WithdrawalRequest, withdrawal_id)
            if withdrawal is not None and withdrawal.status == "processing":
                withdrawal.error_message = str(exc)[:512]
                await _refund_failed_withdrawal(db, withdrawal)
                await db.commit()
                processed += 1
            continue
        except Exception:
            await db.rollback()
            logger.exception("Unexpected payout adapter error for withdrawal %s", withdrawal_id)
            withdrawal = await db.get(WithdrawalRequest, withdrawal_id)
            if withdrawal is not None and withdrawal.status == "processing":
                withdrawal.updated_at = utc_now()
                await db.commit()
            continue

        await db.rollback()
        withdrawal = await db.get(WithdrawalRequest, withdrawal_id)
        if withdrawal is None or withdrawal.status != "processing":
            continue
        valid_fee_amount = (
            isinstance(payout.network_fee_amount, Decimal)
            and payout.network_fee_amount.is_finite()
            and payout.network_fee_amount >= 0
        )
        valid_payout_amount = (
            isinstance(payout.payout_amount, Decimal)
            and payout.payout_amount.is_finite()
            and payout.payout_amount > 0
        )
        if (
            not valid_fee_amount
            or payout.network_fee_currency != "TON"
            or payout.payout_currency != withdrawal.currency
            or not valid_payout_amount
            or not payout.tx_hash
        ):
            withdrawal.status = "manual_review"
            withdrawal.tx_hash = payout.tx_hash if isinstance(payout.tx_hash, str) else None
            withdrawal.network_fee_amount = (
                payout.network_fee_amount if valid_fee_amount else None
            )
            withdrawal.network_fee_currency = (
                payout.network_fee_currency
                if payout.network_fee_currency == "TON"
                else None
            )
            if valid_payout_amount:
                withdrawal.payout_amount = payout.payout_amount
            withdrawal.error_message = (
                "Payout succeeded but its reported amount/fee needs administrator reconciliation"
            )
            withdrawal.updated_at = utc_now()
            await db.commit()
            processed += 1
            continue
        actual_fee_charge = payout.network_fee_amount
        if withdrawal.currency == "USDT":
            if payout.network_fee_currency != "TON" or withdrawal.ton_usd_rate is None:
                withdrawal.status = "manual_review"
                withdrawal.error_message = "Payout fee currency/rate cannot be reconciled"
                await db.commit()
                processed += 1
                continue
            actual_fee_charge *= withdrawal.ton_usd_rate
        elif withdrawal.currency != "TON" or payout.network_fee_currency != "TON":
            withdrawal.status = "manual_review"
            withdrawal.error_message = "Payout provider returned an unexpected network fee currency"
            await db.commit()
            processed += 1
            continue
        actual_fee_charge = actual_fee_charge.quantize(
            AMOUNT_QUANTUM, rounding=ROUND_DOWN
        )
        expected_payout = (
            withdrawal.gross_amount - withdrawal.platform_fee - actual_fee_charge
        ).quantize(AMOUNT_QUANTUM, rounding=ROUND_DOWN)
        actual_payout = payout.payout_amount.quantize(
            AMOUNT_QUANTUM, rounding=ROUND_DOWN
        )
        if expected_payout <= 0 or actual_payout != expected_payout:
            withdrawal.status = "manual_review"
            withdrawal.tx_hash = payout.tx_hash
            withdrawal.network_fee_amount = payout.network_fee_amount
            withdrawal.network_fee_currency = payout.network_fee_currency
            withdrawal.payout_amount = actual_payout
            withdrawal.error_message = (
                "Payout amount does not reconcile to the actual network fee"
            )
            withdrawal.updated_at = utc_now()
            await db.commit()
            processed += 1
            continue
        withdrawal.status = "completed"
        withdrawal.tx_hash = payout.tx_hash
        withdrawal.network_fee_amount = payout.network_fee_amount
        withdrawal.network_fee_currency = payout.network_fee_currency
        withdrawal.payout_amount = actual_payout
        withdrawal.error_message = None
        withdrawal.updated_at = utc_now()
        if payout.network_fee_amount > 0:
            await apply_treasury_ledger_delta(
                db,
                currency=payout.network_fee_currency,
                amount=-payout.network_fee_amount,
                entry_type="NETWORK_FEE",
                reference_type="withdrawal_request",
                reference_id=withdrawal.id,
                idempotency_key=f"withdrawal:network-fee:{withdrawal.id}",
                telemetry={"blockchain_gas_fee_ton": payout.network_fee_amount},
                allow_negative=True,
            )
        await record_financial_event(
            db,
            user_id=withdrawal.user_id,
            event_type="WITHDRAWAL",
            currency=withdrawal.currency,
            gross_amount=withdrawal.payout_amount,
            reference_type="withdrawal_payout",
            reference_id=withdrawal.id,
            tx_hash=payout.tx_hash,
        )
        await db.commit()
        processed += 1
    return processed


async def _refund_failed_withdrawal(db: AsyncSession, withdrawal: WithdrawalRequest) -> None:
    await apply_treasury_ledger_delta(
        db,
        currency=withdrawal.currency,
        amount=-withdrawal.platform_fee,
        entry_type="WITHDRAWAL_FEE_REVERSAL",
        reference_type="withdrawal_request",
        reference_id=withdrawal.id,
        idempotency_key=f"withdrawal:fee-reversal:{withdrawal.id}",
        telemetry={"platform_fees_collected": -withdrawal.platform_fee},
    )
    await apply_ledger_delta(
        db,
        user_id=withdrawal.user_id,
        currency=withdrawal.currency,
        amount=withdrawal.gross_amount,
        entry_type="WITHDRAWAL_REFUND",
        reference_type="withdrawal_request",
        reference_id=withdrawal.id,
        idempotency_key=f"withdrawal:refund:{withdrawal.id}",
    )
    withdrawal.status = "refunded"
    withdrawal.updated_at = utc_now()


async def expire_stale_payment_intents(db: AsyncSession, limit: int = 100) -> int:
    now = utc_now()
    result = await db.execute(
        select(PaymentIntent)
        .where(
            PaymentIntent.status.in_(("pending", "awaiting_payment")),
            PaymentIntent.expires_at <= now,
        )
        .limit(limit)
    )
    count = 0
    for intent in result.scalars():
        intent.status = "expired"
        count += 1
    if count:
        await db.commit()
    return count
