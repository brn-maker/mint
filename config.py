import os
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

REFERRAL_COMMISSION_RATE = Decimal(os.getenv("REFERRAL_COMMISSION_RATE", "0.30"))
WITHDRAWAL_FEE_RATE = Decimal(os.getenv("WITHDRAWAL_FEE_RATE", "0.10"))
AMOUNT_QUANTUM = Decimal("0.00000001")

PAYMENT_QUOTE_MINUTES = int(os.getenv("PAYMENT_QUOTE_MINUTES", "30"))
PAYMENT_INTENT_TTL_MINUTES = int(os.getenv("PAYMENT_INTENT_TTL_MINUTES", "60"))
WITHDRAWAL_QUOTE_TTL_SECONDS = int(os.getenv("WITHDRAWAL_QUOTE_TTL_SECONDS", "300"))

TON_DEPOSIT_ADDRESS = os.getenv("TON_DEPOSIT_ADDRESS", "").strip()
TON_NETWORK = os.getenv("TON_NETWORK", "testnet").strip().lower()
TON_API_ENDPOINT = os.getenv("TON_API_ENDPOINT", "").strip()
TON_API_KEY = os.getenv("TON_API_KEY", "").strip()
APP_WALLET_MNEMONIC = os.getenv("APP_WALLET_MNEMONIC", "").strip()
TON_TESTNET_PAYOUTS_ENABLED = (
    os.getenv("TON_TESTNET_PAYOUTS_ENABLED", "false").strip().lower() == "true"
)
TON_PAYMENT_SCAN_INTERVAL_SECONDS = float(
    os.getenv("TON_PAYMENT_SCAN_INTERVAL_SECONDS", "10")
)

ALLOW_DEMO_PAYMENTS = os.getenv(
    "ALLOW_DEMO_PAYMENTS",
    "true" if os.getenv("ALLOW_DEMO_AUTH", "").lower() == "true" else "false",
).lower() == "true"

RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
ENABLE_BACKGROUND_WORKERS = os.getenv("ENABLE_BACKGROUND_WORKERS", "true").lower() == "true"
WITHDRAWAL_WORKER_INTERVAL_SECONDS = float(os.getenv("WITHDRAWAL_WORKER_INTERVAL_SECONDS", "15"))
DIVIDEND_BATCH_INTERVAL_SECONDS = float(os.getenv("DIVIDEND_BATCH_INTERVAL_SECONDS", "300"))
PAYMENT_EXPIRY_INTERVAL_SECONDS = float(os.getenv("PAYMENT_EXPIRY_INTERVAL_SECONDS", "60"))
WITHDRAWAL_PROCESSING_STALE_SECONDS = int(os.getenv("WITHDRAWAL_PROCESSING_STALE_SECONDS", "300"))
DEMO_TON_GAS_FEE_ESTIMATE = Decimal(
    os.getenv("DEMO_TON_GAS_FEE_ESTIMATE", "0.005")
)
AUTO_CREATE_SCHEMA = os.getenv("AUTO_CREATE_SCHEMA", "false").lower() == "true"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
