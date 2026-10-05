"""Create repeatable, clearly labeled demo accounts in a dedicated SQLite DB.

This script refuses the app's normal mintai.db and accepts only a file whose
name contains "staging". It never deletes existing records.
"""

from __future__ import annotations

import argparse
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import sys
from urllib.parse import unquote

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import Base  # noqa: E402
from models import (  # noqa: E402
    FinancialTelemetry,
    LedgerEntry,
    PaymentIntent,
    ServerSlot,
    User,
    WalletBalance,
    utc_now,
)


SEED_PREFIX = 900_000_000


def _safe_database_path(database_url: str) -> Path:
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "sqlite" or parsed.database in (None, ":memory:"):
        raise SystemExit("Use a file-backed SQLite URL reserved for staging, for example sqlite+aiosqlite:////tmp/mintai-staging.db")
    database_path = Path(unquote(parsed.database)).expanduser().resolve()
    if "staging" not in database_path.name.lower() or database_path.name.lower() == "mintai.db":
        raise SystemExit("Refusing this database: its filename must contain 'staging' and must not be mintai.db")
    return database_path


def _referral_code(user_id: int) -> str:
    return f"S{user_id % 100_000:05d}"


def _add_user(db: Session, user_id: int, label: str, referred_by: int | None = None) -> None:
    if db.get(User, user_id) is None:
        db.add(
            User(
                id=user_id,
                username=f"stage_{label}"[:64],
                referred_by=referred_by,
                referral_code=_referral_code(user_id),
            )
        )


def _add_confirmed_referral_payment(db: Session, user_id: int, suffix: str) -> None:
    intent_id = f"staging-referral-{suffix}"
    if db.get(PaymentIntent, intent_id) is not None:
        return
    now = utc_now()
    db.add(
        PaymentIntent(
            id=intent_id,
            user_id=user_id,
            server_id="h100-node-01",
            pricing_tier_id="tier-300",
            server_pool="NVIDIA-H100-POOL-A",
            payment_method="crypto",
            currency="USDT",
            expected_amount=100,
            quote_expires_at=now,
            status="confirmed",
            provider="staging-seed",
            expires_at=now + timedelta(days=1),
            confirmed_at=now,
        )
    )


def _seed_balance(db: Session, user_id: int, currency: str, amount: str) -> None:
    key = f"staging-seed:balance:{user_id}:{currency}"
    if db.scalar(select(LedgerEntry.id).where(LedgerEntry.idempotency_key == key)):
        return
    value = Decimal(amount)
    balance = db.scalar(
        select(WalletBalance).where(
            WalletBalance.user_id == user_id,
            WalletBalance.currency == currency,
        )
    )
    if balance is None:
        balance = WalletBalance(
            user_id=user_id, currency=currency, withdrawable_amount=Decimal("0")
        )
        db.add(balance)
        db.flush()
    balance.withdrawable_amount += value
    db.add(
        LedgerEntry(
            user_id=user_id,
            account_code=f"user:{user_id}",
            currency=currency,
            amount=value,
            entry_type="STAGING_SEED",
            reference_type="staging_seed",
            reference_id=f"user:{user_id}:{currency}",
            idempotency_key=key,
            balance_after=balance.withdrawable_amount,
        )
    )
    db.add(
        FinancialTelemetry(
            user_id=user_id,
            event_type="STAGING_SEED",
            currency=currency,
            gross_amount=value,
            reference_type="staging_seed",
            reference_id=f"user:{user_id}:{currency}",
        )
    )


def seed(database_url: str) -> None:
    database_path = _safe_database_path(database_url)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    sync_database_url = make_url(database_url).set(drivername="sqlite")
    engine = create_engine(sync_database_url)
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        Base.metadata.create_all(engine)

        with Session(engine) as db:
            # Stable IDs make it easy to switch demo identities via ?demo_user_id=...
            users = {
                "dashboard": SEED_PREFIX + 1,
                "withdrawals": SEED_PREFIX + 2,
                "stars_hold": SEED_PREFIX + 3,
                "five_referrals": SEED_PREFIX + 4,
                "fifty_referrals": SEED_PREFIX + 5,
            }
            for label, user_id in users.items():
                _add_user(db, user_id, label)
            db.flush()

            withdrawals = db.get(User, users["withdrawals"])
            if not withdrawals.wallet_address:
                withdrawals.wallet_address = "EQ" + "A" * 46
            for currency, amount in (("XTR", "300"), ("TON", "5"), ("USDT", "100")):
                _seed_balance(db, withdrawals.id, currency, amount)

            stars_user_id = users["stars_hold"]
            stars_slot_id = f"staging-stars-{stars_user_id}"
            if db.get(ServerSlot, stars_slot_id) is None:
                now = utc_now()
                db.add(
                    ServerSlot(
                        id=stars_slot_id,
                        user_id=stars_user_id,
                        server_id="h100-node-01",
                        pricing_tier_id="tier-300",
                        server_pool="NVIDIA-H100-POOL-A",
                        payment_method="stars",
                        currency="XTR",
                        gross_deposit=300,
                        net_investment=300,
                        clearance_ready_at=now + timedelta(days=20),
                        status="active",
                        is_active=True,
                        created_at=now - timedelta(days=1),
                        last_dividend_at=now,
                    )
                )

            referral_groups = (
                (users["five_referrals"], range(SEED_PREFIX + 40, SEED_PREFIX + 45)),
                (users["fifty_referrals"], range(SEED_PREFIX + 100, SEED_PREFIX + 150)),
            )
            for owner_id, referral_ids in referral_groups:
                for referral_id in referral_ids:
                    _add_user(db, referral_id, f"ref_{referral_id % 100_000:05d}", owner_id)
                    _add_confirmed_referral_payment(
                        db, referral_id, str(referral_id)
                    )

            principal_owner_id = users["fifty_referrals"]
            principal_slot_id = f"staging-principal-{principal_owner_id}"
            if db.get(ServerSlot, principal_slot_id) is None:
                now = utc_now()
                db.add(
                    ServerSlot(
                        id=principal_slot_id,
                        user_id=principal_owner_id,
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
            db.commit()

            print("Staging seed complete (existing records were preserved).")
            print(f"Database: {database_path}")
            for label, user_id in users.items():
                print(f"{label:16} demo_user_id={user_id}")
            print("five_referrals   shows 5 paid referrals and a dividend milestone after dashboard refresh")
            print("fifty_referrals  unlocks its seeded principal on dashboard refresh")
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        required=True,
        help="Dedicated file-backed SQLite URL; filename must contain 'staging'.",
    )
    args = parser.parse_args()
    seed(args.database_url)


if __name__ == "__main__":
    main()
