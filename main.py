import asyncio
import hmac
import logging
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    AUTO_CREATE_SCHEMA,
    CRYPTO_DEPOSIT_ADDRESS,
    ENABLE_BACKGROUND_WORKERS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_WEBHOOK_PATH,
    TELEGRAM_WEBHOOK_SECRET,
    TON_INDEXER_WEBHOOK_SECRET,
    TON_MIN_CONFIRMATIONS,
    TON_NETWORK,
    USDT_JETTON_MASTER,
)
from database import Base, engine, get_db
from dividends import (
    DAILY_DIVIDEND_RATES,
    daily_dividend_amount,
    daily_dividend_percent,
    ghost_week_start,
)
from engine import (
    accrue_dividends,
    confirm_investment_from_payment,
    execute_withdrawal,
    quote_withdrawal,
)
from ledger import LedgerError, apply_treasury_ledger_delta, record_financial_event
from mock_data import SERVER_POOLS, format_commission_event, generate_diagnostics
from models import (
    FinancialTelemetry,
    LedgerEntry,
    PaymentIntent,
    ReferralDividendMilestone,
    ServerSlot,
    User,
    WalletBalance,
    WithdrawalRequest as WithdrawalRequestModel,
)
from payments.service import build_client_payload, create_payment_intent
from payments.payouts import PayoutRejected, get_payout_adapter
from payments.stars import TelegramStarsAdapter, answer_pre_checkout_query
from payments.ton import TonIndexerAdapter
from pricing import (
    PriceFeedError,
    get_ton_usd_price,
    public_server_listings,
)
from referrals import build_referral_link, generate_user_referral_code
from rate_limit import rate_limit_sensitive
from telegram_auth import TelegramAuthError, validate_telegram_init_data
from wallet_validation import is_plausible_payout_address


BASE_DIR = Path(__file__).resolve().parent
FRONTEND_FILE = BASE_DIR / "frontend" / "index.html"
logger = logging.getLogger(__name__)
DEMO_USER_ID = int(os.getenv("DEMO_USER_ID", "10001"))
BOT_TOKEN = TELEGRAM_BOT_TOKEN
ALLOW_DEMO_AUTH = os.getenv("ALLOW_DEMO_AUTH", "true" if not BOT_TOKEN else "false").lower() == "true"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if AUTO_CREATE_SCHEMA:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    worker_task = None
    if ENABLE_BACKGROUND_WORKERS:
        from workers.runner import run_payment_workers

        worker_task = asyncio.create_task(run_payment_workers())
    yield
    if worker_task is not None:
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
    await engine.dispose()


app = FastAPI(title="Mint AI API", version="1.0.0", lifespan=lifespan)
allowed_origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials="*" not in allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/assets", StaticFiles(directory=BASE_DIR / "frontend" / "assets"), name="assets")


class RegisterRequest(BaseModel):
    referral_code: str | None = Field(default=None, min_length=6, max_length=6)
    username: str | None = Field(default=None, max_length=64)
    wallet_address: str | None = Field(default=None, min_length=10, max_length=128)


class ProfileRequest(BaseModel):
    wallet_address: str = Field(min_length=48, max_length=128)


class PaymentIntentRequest(BaseModel):
    server_id: str = Field(min_length=1, max_length=32)
    payment_method: Literal["stars", "crypto"]
    currency: Literal["XTR", "TON", "USDT"]


class ConfirmInvestmentRequest(BaseModel):
    intent_id: str = Field(min_length=36, max_length=36)


class WithdrawalRequest(BaseModel):
    quote_id: str = Field(min_length=36, max_length=36)


class WithdrawalQuoteRequest(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    currency: Literal["XTR", "TON", "USDT"]


class ManualStarsSettlementRequest(BaseModel):
    settlement_currency: Literal["TON", "USDT"]
    settlement_amount: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    settlement_rate: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    ton_usd_rate: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=8)
    network_fee_ton: Decimal = Field(ge=0, max_digits=24, decimal_places=8)
    tx_hash: str = Field(min_length=8, max_length=128)


class NetworkFeeReconciliationRequest(BaseModel):
    payout_amount: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    network_fee_ton: Decimal = Field(ge=0, max_digits=24, decimal_places=8)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _telegram_user_id(request: Request) -> tuple[int, dict[str, object] | None]:
    authorization = request.headers.get("authorization", "")
    init_data = ""
    if authorization.lower().startswith("tma "):
        init_data = authorization[4:]
    elif request.headers.get("x-telegram-init-data"):
        init_data = request.headers["x-telegram-init-data"]

    if init_data:
        if not BOT_TOKEN:
            raise HTTPException(status_code=503, detail="Telegram authentication is not configured")
        try:
            user = validate_telegram_init_data(init_data, BOT_TOKEN)
        except TelegramAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return int(user["id"]), user

    if ALLOW_DEMO_AUTH:
        raw_id = request.headers.get("x-demo-user-id", str(DEMO_USER_ID))
        try:
            user_id = int(raw_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="x-demo-user-id must be an integer") from exc
        if user_id <= 0:
            raise HTTPException(status_code=400, detail="User ID must be positive")
        return user_id, None
    raise HTTPException(status_code=401, detail="Valid Telegram Mini App init data is required")


async def _get_or_create_user(
    db: AsyncSession,
    user_id: int,
    telegram_user: dict[str, object] | None = None,
    referral_code: str | None = None,
) -> User:
    user = await db.get(User, user_id)
    if user is not None:
        return user
    referrer_id = None
    if referral_code:
        result = await db.execute(select(User).where(User.referral_code == referral_code.upper()))
        referrer = result.scalar_one_or_none()
        if referrer is None:
            raise HTTPException(status_code=400, detail="Referral code not found")
        if referrer.id == user_id:
            raise HTTPException(status_code=400, detail="Self-referrals are not allowed")
        referrer_id = referrer.id

    username = telegram_user.get("username") if telegram_user else None
    user = User(
        id=user_id,
        username=str(username)[:64] if username else None,
        referred_by=referrer_id,
        referral_code=generate_user_referral_code(user_id),
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="The user or deterministic referral code already exists",
        ) from exc
    await db.refresh(user)
    return user


@app.get("/")
async def dashboard_page() -> FileResponse:
    return FileResponse(FRONTEND_FILE)


@app.get("/servers")
@app.get("/servers/{server_path:path}")
async def server_catalog_page(server_path: str = "") -> FileResponse:
    return FileResponse(FRONTEND_FILE)


@app.get("/api/dashboard")
async def dashboard(
    request: Request, db: AsyncSession = Depends(get_db)
) -> dict[str, object]:
    user_id, telegram_user = _telegram_user_id(request)
    user = await _get_or_create_user(db, user_id, telegram_user)
    await accrue_dividends(db, user)

    qualified_referrals = int(
        await db.scalar(
            select(func.count(func.distinct(User.id)))
            .join(PaymentIntent, PaymentIntent.user_id == User.id)
            .where(User.referred_by == user_id, PaymentIntent.status == "confirmed")
        )
        or 0
    )
    milestones_result = await db.execute(
        select(ReferralDividendMilestone)
        .where(ReferralDividendMilestone.user_id == user_id)
        .order_by(ReferralDividendMilestone.referral_threshold)
    )
    referral_milestones = list(milestones_result.scalars())
    now = datetime.now(timezone.utc)
    active_milestone = next(
        (
            milestone
            for milestone in referral_milestones
            if _as_utc(milestone.starts_at) <= now < _as_utc(milestone.ends_at)
        ),
        None,
    )
    commission_received = bool(
        await db.scalar(
            select(LedgerEntry.id)
            .where(LedgerEntry.user_id == user_id, LedgerEntry.entry_type == "COMMISSION")
            .limit(1)
        )
    )
    ghost_enabled = active_milestone is None and (
        qualified_referrals >= 5 or not commission_received
    )

    balances_result = await db.execute(
        select(WalletBalance).where(WalletBalance.user_id == user_id).order_by(WalletBalance.currency)
    )
    slots_result = await db.execute(
        select(ServerSlot).where(ServerSlot.user_id == user_id).order_by(ServerSlot.created_at.desc())
    )
    dividend_totals_result = await db.execute(
        select(LedgerEntry.reference_id, func.sum(LedgerEntry.amount))
        .where(
            LedgerEntry.user_id == user_id,
            LedgerEntry.entry_type == "DIVIDEND",
            LedgerEntry.reference_type == "server_slot",
        )
        .group_by(LedgerEntry.reference_id)
    )
    dividend_totals = {
        reference_id: Decimal(amount)
        for reference_id, amount in dividend_totals_result
    }
    ledger_result = await db.execute(
        select(FinancialTelemetry)
        .where(FinancialTelemetry.user_id == user_id)
        .order_by(FinancialTelemetry.created_at.desc())
        .limit(50)
    )
    balances = [
        {
            "currency": balance.currency,
            "withdrawable": str(balance.withdrawable_amount),
        }
        for balance in balances_result.scalars()
    ]
    slots = []
    slot_models = list(slots_result.scalars())
    ghost_by_currency: dict[str, dict[str, Decimal | datetime]] = {}
    if ghost_enabled:
        reset_at = ghost_week_start(now)
        ghost_timezone = timezone(timedelta(hours=3))
        for slot in slot_models:
            rate = DAILY_DIVIDEND_RATES.get(slot.server_id or "")
            if not slot.is_active or rate is None:
                continue
            projection = ghost_by_currency.setdefault(
                slot.currency,
                {
                    "principal": Decimal("0"),
                    "daily_dividend": Decimal("0"),
                    "latest_deposit_at": reset_at,
                },
            )
            projection["principal"] += Decimal(slot.net_investment)
            projection["daily_dividend"] += Decimal(slot.net_investment) * rate
            projection["latest_deposit_at"] = max(
                _as_utc(projection["latest_deposit_at"]),
                _as_utc(slot.created_at),
            )
        ghost_dividends = []
        for currency, projection in ghost_by_currency.items():
            anchor = max(reset_at, _as_utc(projection["latest_deposit_at"]))
            elapsed_days = max(
                0,
                (
                    now.astimezone(ghost_timezone).date()
                    - anchor.astimezone(ghost_timezone).date()
                ).days,
            )
            principal = projection["principal"]
            ghost_amount = (
                projection["daily_dividend"] * elapsed_days
            ).quantize(Decimal("0.00000001"))
            ghost_dividends.append(
                {
                    "currency": currency,
                    "principal": str(principal),
                    "ghost_dividend": str(ghost_amount),
                    "projected_total": str(principal + ghost_amount),
                    "weekly_reset_at": reset_at.isoformat(),
                }
            )
    else:
        ghost_dividends = []

    for slot in slot_models:
        pool = SERVER_POOLS[slot.server_pool]
        server_listing = next(
            (listing for listing in public_server_listings(None) if listing["id"] == slot.server_id),
            None,
        )
        diagnostics = generate_diagnostics(slot.server_pool)
        locked = slot.clearance_ready_at.replace(tzinfo=now.tzinfo) > now if slot.clearance_ready_at.tzinfo is None else slot.clearance_ready_at > now
        slot_rate = daily_dividend_percent(slot.server_id or "")
        slot_daily_amount = daily_dividend_amount(
            slot.server_id or "", Decimal(slot.net_investment)
        )
        slots.append(
            {
                "id": slot.id,
                "server_id": slot.server_id,
                "created_at": _as_utc(slot.created_at).isoformat(),
                "pricing_tier_id": slot.pricing_tier_id,
                "server_pool": slot.server_pool,
                "label": pool["label"],
                "gpu": server_listing["hardware"] if server_listing else pool["gpu"],
                "gpu_count": pool["gpu_count"],
                "payment_method": slot.payment_method,
                "currency": slot.currency,
                "gross_deposit": str(slot.gross_deposit),
                "net_investment": str(slot.net_investment),
                "clearance_ready_at": _as_utc(slot.clearance_ready_at).isoformat(),
                "is_locked": locked,
                "principal_is_locked": slot.principal_unlocked_at is None,
                "principal_unlocked_at": (
                    _as_utc(slot.principal_unlocked_at).isoformat()
                    if slot.principal_unlocked_at
                    else None
                ),
                "is_active": slot.is_active,
                "annual_yield_percent": float(pool["annual_yield"] * 100),
                "daily_dividend_percent": str(slot_rate) if slot_rate is not None else None,
                "daily_dividend_amount": (
                    str(slot_daily_amount) if slot_daily_amount is not None else None
                ),
                "dividends_earned": str(dividend_totals.get(slot.id, Decimal("0"))),
                "diagnostics": diagnostics,
            }
        )
    ledger = [
        {
            "id": entry.id,
            "event_type": entry.event_type,
            "currency": entry.currency,
            "amount": str(entry.gross_amount),
            "created_at": _as_utc(entry.created_at).isoformat(),
        }
        for entry in ledger_result.scalars()
    ]
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "wallet_address": user.wallet_address,
            "referral_code": user.referral_code,
            "referral_link": build_referral_link(user.referral_code),
            "accumulated_dividends": str(user.accumulated_dividends),
            "qualified_referrals": qualified_referrals,
            "referrals_to_next_milestone": (
                5 - qualified_referrals % 5 if qualified_referrals % 5 else 5
            ),
            "true_dividends_active_until": (
                _as_utc(active_milestone.ends_at).isoformat()
                if active_milestone
                else None
            ),
            "principal_unlock_referrals_remaining": max(0, 50 - qualified_referrals),
            "ghost_dividends_enabled": ghost_enabled,
            "commission_received": commission_received,
        },
        "balances": balances,
        "ghost_dividends": ghost_dividends,
        "slots": slots,
        "ledger": ledger,
        "withdrawals_locked": any(slot["is_locked"] for slot in slots),
        "server_pools": [
            {
                "id": pool_id,
                "label": details["label"],
                "gpu": details["gpu"],
            }
            for pool_id, details in SERVER_POOLS.items()
        ],
    }


@app.get("/api/pricing")
async def pricing() -> dict[str, object]:
    try:
        ton_usd = await get_ton_usd_price()
        return {
            "servers": public_server_listings(ton_usd),
            "ton_usd": str(ton_usd),
            "ton_quote_available": True,
            "ton_quote_error": None,
        }
    except PriceFeedError:
        logger.warning("Live TON/USD price quote unavailable")
        return {
            "servers": public_server_listings(None),
            "ton_usd": None,
            "ton_quote_available": False,
            "ton_quote_error": "Live TON/USD quote is unavailable. Choose Stars or USDT, or try again later.",
        }


@app.post("/api/register")
async def register(
    body: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    user_id, telegram_user = _telegram_user_id(request)
    user = await _get_or_create_user(
        db, user_id, telegram_user, referral_code=body.referral_code
    )
    if body.username:
        user.username = body.username[:64]
    if body.wallet_address:
        if not is_plausible_payout_address(body.wallet_address):
            raise HTTPException(status_code=422, detail="Enter a valid TON wallet address")
        user.wallet_address = body.wallet_address
    await db.commit()
    return {"user_id": user.id, "referral_code": user.referral_code}


@app.put("/api/profile")
async def update_profile(
    body: ProfileRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    user_id, telegram_user = _telegram_user_id(request)
    user = await _get_or_create_user(db, user_id, telegram_user)
    if not is_plausible_payout_address(body.wallet_address):
        raise HTTPException(status_code=422, detail="Enter a valid TON wallet address")
    user.wallet_address = body.wallet_address
    await db.commit()
    return {"wallet_address": user.wallet_address}


@app.post("/api/payment-intents", status_code=201)
async def create_intent(
    body: PaymentIntentRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=16, max_length=64),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    user_id, telegram_user = _telegram_user_id(request)
    await rate_limit_sensitive(request, user_id)
    await _get_or_create_user(db, user_id, telegram_user)
    if body.payment_method == "stars" and body.currency != "XTR":
        raise HTTPException(status_code=400, detail="Stars payments require XTR")
    if body.payment_method == "crypto" and body.currency not in {"TON", "USDT"}:
        raise HTTPException(status_code=400, detail="Crypto payments require TON or USDT")
    try:
        intent = await create_payment_intent(
            db,
            user_id=user_id,
            server_id=body.server_id,
            payment_method=body.payment_method,
            currency=body.currency,
            idempotency_key=idempotency_key,
            bot_token=BOT_TOKEN,
        )
    except PriceFeedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return build_client_payload(intent)


@app.get("/api/payment-intents/{intent_id}")
async def get_payment_intent(
    intent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    user_id, _ = _telegram_user_id(request)
    intent = await db.get(PaymentIntent, intent_id)
    if intent is None or intent.user_id != user_id:
        raise HTTPException(status_code=404, detail="Payment intent not found")
    slot_id = await db.scalar(
        select(ServerSlot.id).where(ServerSlot.payment_intent_id == intent.id)
    )
    return build_client_payload(intent, slot_id=slot_id)


@app.post("/api/investments")
async def create_investment(
    body: ConfirmInvestmentRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    user_id, telegram_user = _telegram_user_id(request)
    await _get_or_create_user(db, user_id, telegram_user)
    intent = await db.get(PaymentIntent, body.intent_id)
    if intent is None or intent.user_id != user_id:
        raise HTTPException(status_code=404, detail="Payment intent not found")
    if intent.status != "confirmed":
        raise HTTPException(
            status_code=409,
            detail=f"Payment is not confirmed (status={intent.status})",
        )
    try:
        result = await confirm_investment_from_payment(db, intent.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.post(TELEGRAM_WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    if not TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Telegram webhook secret is not configured")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, TELEGRAM_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Telegram bot token is not configured")
    try:
        update = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Telegram update JSON") from exc

    checkout = update.get("pre_checkout_query") if isinstance(update, dict) else None
    if checkout:
        try:
            intent = await db.get(PaymentIntent, str(checkout["invoice_payload"]))
            if intent is None:
                raise ValueError("Payment intent was not found")
            if int(checkout["from"]["id"]) != intent.user_id:
                raise ValueError("Invoice belongs to a different Telegram user")
            if intent.payment_method != "stars" or intent.currency != "XTR":
                raise ValueError("Payment intent is not a Telegram Stars invoice")
            if intent.status not in {"pending", "awaiting_payment"}:
                raise ValueError("Payment intent is no longer payable")
            if _as_utc(intent.expires_at) <= datetime.now(timezone.utc):
                raise ValueError("Payment intent has expired")
            TelegramStarsAdapter.validate_payment(
                intent,
                total_amount=int(checkout["total_amount"]),
                currency=str(checkout["currency"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            await answer_pre_checkout_query(
                str(checkout.get("id", "")),
                BOT_TOKEN,
                ok=False,
                error_message=str(exc),
            )
            return {"ok": True, "pre_checkout_accepted": False}
        await answer_pre_checkout_query(str(checkout["id"]), BOT_TOKEN, ok=True)
        return {"ok": True, "pre_checkout_accepted": True}

    message = update.get("message") if isinstance(update, dict) else None
    successful_payment = message.get("successful_payment") if isinstance(message, dict) else None
    if successful_payment:
        try:
            intent_id = str(successful_payment["invoice_payload"])
            intent = await db.get(PaymentIntent, intent_id)
            if intent is None:
                raise ValueError("Payment intent was not found")
            from_user = message.get("from", {})
            if int(from_user["id"]) != intent.user_id:
                raise ValueError("Successful payment belongs to a different Telegram user")
            TelegramStarsAdapter.validate_payment(
                intent,
                total_amount=int(successful_payment["total_amount"]),
                currency=str(successful_payment["currency"]),
            )
            charge_id = str(successful_payment["telegram_payment_charge_id"])
            result = await confirm_investment_from_payment(
                db,
                intent.id,
                external_payment_id=charge_id,
                provider="stars",
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Rejected Telegram Stars payment update: %s", exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "investment": result}
    return {"ok": True, "ignored": True}


@app.post("/webhooks/ton/indexer")
async def ton_indexer_webhook(
    request: Request,
    x_ton_signature: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    if not TON_INDEXER_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="TON indexer webhook is not configured")
    adapter = TonIndexerAdapter(TON_INDEXER_WEBHOOK_SECRET, TON_MIN_CONFIRMATIONS)
    body = await request.body()
    if not adapter.verify_signature(body, x_ton_signature or ""):
        raise HTTPException(status_code=401, detail="Invalid TON indexer signature")
    try:
        event = adapter.parse_event(body)
        adapter.validate_event(
            event,
            network=TON_NETWORK,
            deposit_address=CRYPTO_DEPOSIT_ADDRESS,
            usdt_jetton_master=USDT_JETTON_MASTER,
        )
        intent = await db.get(PaymentIntent, event.intent_id)
        if intent is None:
            raise ValueError("Payment intent was not found")
        if intent.payment_method != "crypto" or intent.currency != event.currency:
            raise ValueError("Transfer currency does not match the payment intent")
        if event.memo != intent.deposit_memo:
            raise ValueError("Transfer memo does not match the payment intent")
        if Decimal(event.amount) != Decimal(intent.expected_amount):
            raise ValueError("Transfer amount does not match the quoted amount")
        result = await confirm_investment_from_payment(
            db,
            intent.id,
            tx_hash=event.tx_hash,
            tx_log_index=event.tx_log_index,
            provider="ton",
        )
    except ValueError as exc:
        logger.warning("Rejected TON indexer event: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "investment": result}


@app.post("/api/slots/{slot_id}/close")
async def close_slot(
    slot_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    user_id, _ = _telegram_user_id(request)
    slot = await db.get(ServerSlot, slot_id)
    if slot is None or slot.user_id != user_id:
        raise HTTPException(status_code=404, detail="Server slot not found")
    clearance_ready_at = (
        slot.clearance_ready_at.replace(tzinfo=timezone.utc)
        if slot.clearance_ready_at.tzinfo is None
        else slot.clearance_ready_at
    )
    if clearance_ready_at > datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="A slot cannot be closed during its clearance period")
    if not slot.is_active:
        raise HTTPException(status_code=409, detail="Server slot is already historical")
    slot.is_active = False
    await db.commit()
    return {"slot_id": slot.id, "status": "historical"}


@app.post("/api/withdrawal-quotes", status_code=201)
async def create_withdrawal_quote(
    body: WithdrawalQuoteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    user_id, _ = _telegram_user_id(request)
    await rate_limit_sensitive(request, user_id)
    try:
        return await quote_withdrawal(
            db,
            user_id,
            body.amount,
            body.currency,
            payout_adapter=get_payout_adapter(),
        )
    except PayoutRejected as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/withdrawals", status_code=202)
async def create_withdrawal(
    body: WithdrawalRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=16, max_length=128),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    user_id, _ = _telegram_user_id(request)
    await rate_limit_sensitive(request, user_id)
    try:
        result = await execute_withdrawal(
            db,
            user_id,
            body.quote_id,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.get("/api/withdrawals/{withdrawal_id}")
async def get_withdrawal(
    withdrawal_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    user_id, _ = _telegram_user_id(request)
    withdrawal = await db.get(WithdrawalRequestModel, withdrawal_id)
    if withdrawal is None or withdrawal.user_id != user_id:
        raise HTTPException(status_code=404, detail="Withdrawal not found")
    return {
        "withdrawal_id": withdrawal.id,
        "status": withdrawal.status,
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
        "tx_hash": withdrawal.tx_hash,
        "error_message": withdrawal.error_message,
    }


def _require_admin_token(supplied_token: str | None) -> None:
    configured_token = os.getenv("ADMIN_TOKEN", "")
    if not configured_token:
        raise HTTPException(
            status_code=503, detail="Admin operations are disabled until ADMIN_TOKEN is set"
        )
    if not supplied_token or not hmac.compare_digest(supplied_token, configured_token):
        raise HTTPException(status_code=401, detail="Invalid admin token")


@app.get("/admin/withdrawals")
async def list_manual_withdrawals(
    x_admin_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _require_admin_token(x_admin_token)
    result = await db.execute(
        select(WithdrawalRequestModel)
        .where(
            WithdrawalRequestModel.status == "manual_review",
        )
        .order_by(WithdrawalRequestModel.created_at)
        .limit(100)
    )
    return {
        "withdrawals": [
            {
                "withdrawal_id": item.id,
                "user_id": item.user_id,
                "gross_amount": str(item.gross_amount),
                "currency": item.currency,
                "platform_fee": str(item.platform_fee),
                "payout_amount_before_settlement": str(item.payout_amount),
                "wallet_address": item.wallet_address_snapshot,
                "status": item.status,
                "created_at": _as_utc(item.created_at).isoformat(),
            }
            for item in result.scalars()
        ]
    }


@app.post("/admin/withdrawals/{withdrawal_id}/settle")
async def settle_manual_stars_withdrawal(
    withdrawal_id: str,
    body: ManualStarsSettlementRequest,
    x_admin_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _require_admin_token(x_admin_token)
    withdrawal = await db.get(WithdrawalRequestModel, withdrawal_id)
    if withdrawal is None or withdrawal.currency != "XTR":
        raise HTTPException(status_code=404, detail="Manual Stars withdrawal not found")
    if withdrawal.status != "manual_review":
        raise HTTPException(status_code=409, detail="Withdrawal is not awaiting manual settlement")
    if body.settlement_currency == "USDT" and body.ton_usd_rate is None:
        raise HTTPException(
            status_code=422,
            detail="A TON/USD rate is required to account for USDT payout gas",
        )
    gas_in_settlement_currency = (
        body.network_fee_ton * body.ton_usd_rate
        if body.settlement_currency == "USDT" and body.ton_usd_rate is not None
        else body.network_fee_ton
    )
    expected_settlement = (
        withdrawal.payout_amount * body.settlement_rate
        - gas_in_settlement_currency
    ).quantize(Decimal("0.00000001"))
    if expected_settlement <= 0 or body.settlement_amount != expected_settlement:
        raise HTTPException(
            status_code=422,
            detail="Settlement amount must equal the converted Stars balance less actual network gas",
        )
    if body.network_fee_ton > 0:
        try:
            await apply_treasury_ledger_delta(
                db,
                currency="TON",
                amount=-body.network_fee_ton,
                entry_type="NETWORK_FEE",
                reference_type="withdrawal_request",
                reference_id=withdrawal.id,
                idempotency_key=f"withdrawal:network-fee:{withdrawal.id}",
                telemetry={"blockchain_gas_fee_ton": body.network_fee_ton},
                allow_negative=True,
            )
        except LedgerError as exc:
            await db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    withdrawal.settlement_currency = body.settlement_currency
    withdrawal.settlement_amount = body.settlement_amount
    withdrawal.settlement_rate = body.settlement_rate
    withdrawal.ton_usd_rate = body.ton_usd_rate
    withdrawal.network_fee_amount = body.network_fee_ton
    withdrawal.network_fee_currency = "TON"
    withdrawal.tx_hash = body.tx_hash
    withdrawal.status = "completed"
    withdrawal.error_message = None
    withdrawal.updated_at = datetime.now(timezone.utc)
    await record_financial_event(
        db,
        user_id=withdrawal.user_id,
        event_type="WITHDRAWAL",
        currency=body.settlement_currency,
        gross_amount=body.settlement_amount,
        reference_type="manual_withdrawal_payout",
        reference_id=withdrawal.id,
        tx_hash=body.tx_hash,
    )
    await db.commit()
    return {
        "withdrawal_id": withdrawal.id,
        "status": withdrawal.status,
        "settlement_currency": withdrawal.settlement_currency,
        "settlement_amount": str(withdrawal.settlement_amount),
        "network_fee_amount": str(withdrawal.network_fee_amount),
        "network_fee_currency": withdrawal.network_fee_currency,
        "tx_hash": withdrawal.tx_hash,
    }


@app.post("/admin/withdrawals/{withdrawal_id}/reconcile-network-fee")
async def reconcile_withdrawal_network_fee(
    withdrawal_id: str,
    body: NetworkFeeReconciliationRequest,
    x_admin_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _require_admin_token(x_admin_token)
    withdrawal = await db.get(WithdrawalRequestModel, withdrawal_id)
    if (
        withdrawal is None
        or withdrawal.currency not in {"TON", "USDT"}
        or withdrawal.status != "manual_review"
        or not withdrawal.tx_hash
    ):
        raise HTTPException(
            status_code=404, detail="Payout requiring network-fee reconciliation not found"
        )
    actual_charge = body.network_fee_ton
    if withdrawal.currency == "USDT":
        if withdrawal.ton_usd_rate is None or withdrawal.ton_usd_rate <= 0:
            raise HTTPException(status_code=409, detail="Withdrawal has no valid TON/USD quote")
        actual_charge *= withdrawal.ton_usd_rate
    actual_charge = actual_charge.quantize(Decimal("0.00000001"))
    expected_payout = (
        withdrawal.gross_amount - withdrawal.platform_fee - actual_charge
    ).quantize(Decimal("0.00000001"))
    if expected_payout <= 0 or body.payout_amount != expected_payout:
        raise HTTPException(
            status_code=422,
            detail="Reported payout must equal the gross amount less the 10% platform fee and actual network fee",
        )
    try:
        if body.network_fee_ton > 0:
            await apply_treasury_ledger_delta(
                db,
                currency="TON",
                amount=-body.network_fee_ton,
                entry_type="NETWORK_FEE",
                reference_type="withdrawal_request",
                reference_id=withdrawal.id,
                idempotency_key=f"withdrawal:network-fee:{withdrawal.id}",
                telemetry={"blockchain_gas_fee_ton": body.network_fee_ton},
                allow_negative=True,
            )
    except LedgerError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    withdrawal.network_fee_amount = body.network_fee_ton
    withdrawal.network_fee_currency = "TON"
    withdrawal.payout_amount = body.payout_amount
    withdrawal.status = "completed"
    withdrawal.error_message = None
    withdrawal.updated_at = datetime.now(timezone.utc)
    await record_financial_event(
        db,
        user_id=withdrawal.user_id,
        event_type="WITHDRAWAL",
        currency=withdrawal.currency,
        gross_amount=withdrawal.payout_amount,
        reference_type="withdrawal_payout",
        reference_id=withdrawal.id,
        tx_hash=withdrawal.tx_hash,
    )
    await db.commit()
    return {
        "withdrawal_id": withdrawal.id,
        "status": withdrawal.status,
        "payout_amount": str(withdrawal.payout_amount),
        "network_fee_amount": str(withdrawal.network_fee_amount),
        "network_fee_currency": withdrawal.network_fee_currency,
        "error_message": withdrawal.error_message,
    }


@app.get("/admin/metrics")
async def admin_metrics(
    x_admin_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    _require_admin_token(x_admin_token)

    users = await db.scalar(select(func.count(User.id))) or 0
    slots = await db.scalar(select(func.count(ServerSlot.id))) or 0
    currency_metrics_result = await db.execute(
        select(
            FinancialTelemetry.currency,
            func.coalesce(
                func.sum(
                    case(
                        (FinancialTelemetry.event_type == "INVESTMENT", FinancialTelemetry.gross_amount),
                        else_=0,
                    )
                ),
                0,
            ).label("investment_volume"),
            func.coalesce(func.sum(FinancialTelemetry.commissions_paid), 0).label(
                "commissions_paid"
            ),
            func.coalesce(func.sum(FinancialTelemetry.platform_fees_collected), 0).label(
                "platform_fees_collected"
            ),
            func.coalesce(
                func.sum(FinancialTelemetry.blockchain_gas_fee_ton), 0
            ).label("network_gas_fee_ton"),
        )
        .group_by(FinancialTelemetry.currency)
        .order_by(FinancialTelemetry.currency)
    )
    financial_by_currency = []
    for currency, investment_volume, commissions, fees, gas_fees_ton in currency_metrics_result:
        gas_cost_for_currency = Decimal(str(gas_fees_ton)) if currency == "TON" else Decimal("0")
        financial_by_currency.append(
            {
                "currency": currency,
                "investment_volume": str(investment_volume),
                "commissions_paid": str(commissions),
                "platform_fees_collected": str(fees),
                "network_gas_fees_ton": str(gas_fees_ton),
                "net_platform_revenue": str(
                    Decimal(str(fees)) - Decimal(str(commissions)) - gas_cost_for_currency
                ),
            }
        )
    return {
        "users": users,
        "server_slots": slots,
        "financial_by_currency": financial_by_currency,
        "note": "Currency totals are not converted; this is a simulation, not audited accounting.",
    }


@app.websocket("/ws/live-wheel")
async def live_wheel(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            username = "anon" + str(random.randint(100, 999))
            amount = Decimal(random.choice([250, 500, 750, 1200]))
            currency = random.choice(["XTR", "TON", "USDT"])
            await websocket.send_json(
                {"message": format_commission_event(username, amount, currency)}
            )
            await asyncio.sleep(random.uniform(3.0, 6.0))
    except WebSocketDisconnect:
        return
