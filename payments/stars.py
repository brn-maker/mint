from decimal import Decimal, ROUND_DOWN

import httpx

from config import TELEGRAM_API_BASE
from models import PaymentIntent


class TelegramStarsAdapter:
    @staticmethod
    async def create_invoice_link(intent: PaymentIntent, bot_token: str) -> str:
        return await create_stars_invoice_link(intent, bot_token)

    @staticmethod
    def validate_payment(
        intent: PaymentIntent, *, total_amount: int, currency: str
    ) -> None:
        validate_pre_checkout(intent, total_amount, currency)


async def create_stars_invoice_link(intent: PaymentIntent, bot_token: str) -> str:
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required for Stars payments")

    stars_amount = int(
        Decimal(intent.expected_amount).quantize(Decimal("1"), rounding=ROUND_DOWN)
    )
    if stars_amount <= 0:
        raise ValueError("Stars amount must be at least 1")

    payload = {
        "title": "Mint AI server slot",
        "description": f"Server {intent.server_id} · tier {intent.pricing_tier_id}",
        "payload": intent.id,
        "currency": "XTR",
        "prices": [{"label": "Server slot", "amount": stars_amount}],
    }
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/createInvoiceLink"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        body = response.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "Telegram createInvoiceLink failed"))
    return str(body["result"])


def validate_pre_checkout(intent: PaymentIntent, total_amount: int, currency: str) -> None:
    if currency != "XTR":
        raise ValueError("Only Telegram Stars (XTR) are accepted for this invoice")
    expected = int(
        Decimal(intent.expected_amount).quantize(Decimal("1"), rounding=ROUND_DOWN)
    )
    if total_amount != expected:
        raise ValueError("Invoice amount does not match the quoted server price")


async def answer_pre_checkout_query(
    query_id: str,
    bot_token: str,
    *,
    ok: bool,
    error_message: str | None = None,
) -> None:
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to answer pre-checkout queries")
    payload: dict[str, object] = {"pre_checkout_query_id": query_id, "ok": ok}
    if error_message and not ok:
        payload["error_message"] = error_message[:200]
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/answerPreCheckoutQuery"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        body = response.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "Telegram pre-checkout acknowledgement failed"))
