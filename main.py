import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import os
import random
import secrets
import time
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
    ENABLE_BACKGROUND_WORKERS,
    TELEGRAM_BOT_TOKEN,
    TON_NETWORK,
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
from payments.ton import TonVerifierError, find_matching_transaction, scan_ton_transactions
from payments.ton_proof import TonProofError, verify_ton_proof
from pricing import public_server_listings
from referrals import build_referral_link, generate_user_referral_code
from rate_limit import rate_limit_sensitive
from telegram_auth import TelegramAuthError, validate_telegram_init_data
from wallet_validation import is_mainnet_ton_address, is_plausible_payout_address


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


class ConfirmInvestmentRequest(BaseModel):
    intent_id: str = Field(min_length=36, max_length=36)


class ValidatePaymentRequest(BaseModel):
    transaction_boc: str = Field(min_length=1, max_length=100_000)


class WithdrawalRequest(BaseModel):
    quote_id: str = Field(min_length=36, max_length=36)


class WithdrawalQuoteRequest(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    currency: Literal["TON"] = "TON"


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
    for attempt in range(10):
        referral_code = generate_user_referral_code(user_id, attempt)
        code_in_use = await db.scalar(
            select(User.id).where(User.referral_code == referral_code)
        )
        if code_in_use is not None:
            continue

        user = User(
            id=user_id,
            username=str(username)[:64] if username else None,
            referred_by=referrer_id,
            referral_code=referral_code,
        )
        db.add(user)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            existing_user = await db.get(User, user_id)
            if existing_user is not None:
                return existing_user
            code_in_use = await db.scalar(
                select(User.id).where(User.referral_code == referral_code)
            )
            if code_in_use is not None:
                continue
            raise HTTPException(
                status_code=409,
                detail="Could not register user due to a database integrity conflict",
            ) from exc
        await db.refresh(user)
        return user
    raise HTTPException(
        status_code=503,
        detail="Could not allocate a unique referral code; retry registration",
    )


@app.get("/")
async def dashboard_page() -> FileResponse:
    return FileResponse(
        FRONTEND_FILE,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/tonconnect-manifest.json")
async def tonconnect_manifest(request: Request) -> dict[str, str]:
    origin = str(request.base_url).rstrip("/")
    return {
        "url": origin,
        "name": "Mint AI",
        "iconUrl": f"{origin}/assets/mint-icon.svg",
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/servers")
@app.get("/servers/{server_path:path}")
async def server_catalog_page(server_path: str = "") -> FileResponse:
    return FileResponse(
        FRONTEND_FILE,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


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
        select(WalletBalance)
        .where(WalletBalance.user_id == user_id, WalletBalance.currency == "TON")
        .order_by(WalletBalance.currency)
    )
    slots_result = await db.execute(
        select(ServerSlot)
        .where(ServerSlot.user_id == user_id, ServerSlot.currency == "TON")
        .order_by(ServerSlot.created_at.desc())
    )
    dividend_totals_result = await db.execute(
        select(LedgerEntry.reference_id, func.sum(LedgerEntry.amount))
        .where(
            LedgerEntry.user_id == user_id,
            LedgerEntry.entry_type == "DIVIDEND",
            LedgerEntry.reference_type == "server_slot",
            LedgerEntry.currency == "TON",
        )
        .group_by(LedgerEntry.reference_id)
    )
    dividend_totals = {
        reference_id: Decimal(amount)
        for reference_id, amount in dividend_totals_result
    }
    ledger_result = await db.execute(
        select(FinancialTelemetry)
        .where(
            FinancialTelemetry.user_id == user_id,
            FinancialTelemetry.currency == "TON",
        )
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
            (listing for listing in public_server_listings() if listing["id"] == slot.server_id),
            None,
        )
        diagnostics = generate_diagnostics(slot.server_pool)
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
    return {"servers": public_server_listings(), "currency": "TON"}


def _validate_payout_address(address: str) -> None:
    if not is_plausible_payout_address(address):
        raise HTTPException(status_code=422, detail="Enter a valid TON wallet address")
    if TON_NETWORK == "testnet" and is_mainnet_ton_address(address):
        raise HTTPException(status_code=422, detail="Use a TON testnet wallet address")


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
        _validate_payout_address(body.wallet_address)
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
    _validate_payout_address(body.wallet_address)
    user.wallet_address = body.wallet_address
    await db.commit()
    return {"wallet_address": user.wallet_address}


PROOF_TOKEN_TTL_SECONDS = 600


class WalletProofVerifyRequest(BaseModel):
    address: str = Field(min_length=10, max_length=128)
    public_key: str = Field(min_length=64, max_length=64)
    proof: dict[str, object] = Field(...)
    token: str = Field(min_length=1, max_length=512)
    domain: str = Field(min_length=1, max_length=253)
    network: str = Field(default="-3", max_length=8)


def _proof_secret() -> str:
    return os.getenv("ADMIN_TOKEN", "") or os.getenv("REFERRAL_SALT", "")


def _sign_proof_token(user_id: int, payload: str, expiry: int) -> str:
    secret = _proof_secret().encode()
    message = f"{user_id}:{payload}:{expiry}".encode()
    signature = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return f"{user_id}:{payload}:{expiry}:{signature}"


def _verify_proof_token(token: str, user_id: int, payload: str) -> bool:
    try:
        parts = token.split(":")
        token_user_id = int(parts[0])
        token_payload = parts[1]
        token_expiry = int(parts[2])
        token_signature = parts[3]
    except (ValueError, IndexError):
        return False
    if token_user_id != user_id or token_payload != payload:
        return False
    if int(time.time()) > token_expiry:
        return False
    secret = _proof_secret().encode()
    message = f"{token_user_id}:{token_payload}:{token_expiry}".encode()
    expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, token_signature)


@app.post("/api/wallet-proof/challenge")
async def wallet_proof_challenge(request: Request) -> dict[str, object]:
    user_id, _ = _telegram_user_id(request)
    payload = secrets.token_hex(16)
    expiry = int(time.time()) + PROOF_TOKEN_TTL_SECONDS
    token = _sign_proof_token(user_id, payload, expiry)
    return {"payload": payload, "token": token, "expires_at": expiry}


@app.post("/api/wallet-proof/verify")
async def wallet_proof_verify(
    body: WalletProofVerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    user_id, _ = _telegram_user_id(request)
    proof = body.proof
    payload = str(proof.get("payload", ""))
    if not _verify_proof_token(body.token, user_id, payload):
        raise HTTPException(status_code=400, detail="Invalid or expired proof token")
    if body.network not in ("-3", "-239"):
        raise HTTPException(status_code=400, detail="Unsupported wallet network")
    try:
        result = await verify_ton_proof(
            address=body.address,
            public_key=body.public_key,
            proof=proof,
            expected_domain=body.domain,
        )
    except TonProofError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result.get("valid"):
        raise HTTPException(status_code=400, detail=str(result.get("error", "Invalid proof")))
    user = await _get_or_create_user(db, user_id)
    verified_address = str(result["address"])
    if TON_NETWORK == "testnet" and is_mainnet_ton_address(verified_address):
        raise HTTPException(status_code=422, detail="Use a TON testnet wallet address")
    user.wallet_address = verified_address
    await db.commit()
    return {"verified": True, "wallet_address": verified_address}


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
    try:
        intent = await create_payment_intent(
            db,
            user_id=user_id,
            server_id=body.server_id,
            idempotency_key=idempotency_key,
        )
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


@app.post("/api/payment-intents/{intent_id}/validate")
async def validate_payment_intent(
    intent_id: str,
    body: ValidatePaymentRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    user_id, _ = _telegram_user_id(request)
    await rate_limit_sensitive(request, user_id)
    try:
        transaction_boc = base64.b64decode(body.transaction_boc, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid TON transaction BOC") from exc
    if not transaction_boc.startswith(bytes.fromhex("b5ee9c72")):
        raise HTTPException(status_code=422, detail="Invalid TON transaction BOC")
    intent = await db.get(PaymentIntent, intent_id)
    if intent is None or intent.user_id != user_id:
        raise HTTPException(status_code=404, detail="Payment intent not found")
    if intent.status == "confirmed":
        return {
            "intent_id": intent.id,
            "status": intent.status,
            "tx_hash": intent.tx_hash,
        }
    if intent.status != "awaiting_payment":
        raise HTTPException(
            status_code=409,
            detail=f"Payment intent is not payable (status={intent.status})",
        )

    try:
        transactions = await scan_ton_transactions(intent.created_at)
    except TonVerifierError as exc:
        logger.exception("TON payment validation failed for intent %s", intent.id)
        raise HTTPException(
            status_code=503,
            detail="TON Testnet validation is temporarily unavailable",
        ) from exc
    transaction = find_matching_transaction(intent, transactions)
    if transaction is None:
        return {"intent_id": intent.id, "status": intent.status, "tx_hash": None}

    try:
        await confirm_investment_from_payment(
            db,
            intent.id,
            tx_hash=transaction["tx_hash"],
            tx_log_index=0,
            provider="ton",
        )
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This TON transaction has already been used",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "intent_id": intent.id,
        "status": "confirmed",
        "tx_hash": transaction["tx_hash"],
    }


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
        "payout_amount": str(withdrawal.payout_amount),
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
        or withdrawal.currency != "TON"
        or withdrawal.status != "manual_review"
        or not withdrawal.tx_hash
    ):
        raise HTTPException(
            status_code=404, detail="Payout requiring network-fee reconciliation not found"
        )
    actual_charge = body.network_fee_ton
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
    slots = await db.scalar(
        select(func.count(ServerSlot.id)).where(ServerSlot.currency == "TON")
    ) or 0
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
        .where(FinancialTelemetry.currency == "TON")
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
            currency = "TON"
            await websocket.send_json(
                {"message": format_commission_event(username, amount, currency)}
            )
            await asyncio.sleep(random.uniform(3.0, 6.0))
    except WebSocketDisconnect:
        return
