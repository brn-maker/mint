import asyncio
import json
import time
from decimal import Decimal, ROUND_DOWN
from urllib.request import Request, urlopen

from dividends import daily_dividend_amount, daily_dividend_percent


PRICE_QUANTUM = Decimal("0.00000001")
TON_USD_PRICE_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=the-open-network&vs_currencies=usd"
)
TON_PRICE_CACHE_SECONDS = 60

PRICING_TIERS: tuple[dict[str, str], ...] = (
    {"id": "tier-300", "label": "Entry", "stars": "300", "usd": "4.50"},
    {"id": "tier-1000", "label": "Starter", "stars": "1000", "usd": "15.00"},
    {"id": "tier-10000", "label": "Builder", "stars": "10000", "usd": "150.00"},
    {"id": "tier-100000", "label": "Pro", "stars": "100000", "usd": "1500.00"},
    {"id": "tier-1000000", "label": "Scale", "stars": "1000000", "usd": "15000.00"},
    {"id": "tier-10000000", "label": "Enterprise", "stars": "10000000", "usd": "150000.00"},
)

SERVER_LISTINGS: tuple[dict[str, str], ...] = (
    {
        "id": "h100-node-01",
        "name": "HPE ML350 Gen 10 Server",
        "pool": "NVIDIA-H100-POOL-A",
        "hardware": "HPE ProLiant ML350 Gen10",
        "tier_id": "tier-300",
    },
    {
        "id": "h100-node-02",
        "name": "HPE ProLiant DL385 Gen11 Server",
        "pool": "NVIDIA-H100-POOL-A",
        "hardware": "HPE ProLiant DL385 Gen11",
        "tier_id": "tier-1000",
    },
    {
        "id": "a100-node-01",
        "name": "HPE ProLiant Compute DL380a Gen12",
        "pool": "NVIDIA-A100-POOL-B",
        "hardware": "HPE ProLiant Compute DL380a Gen12",
        "tier_id": "tier-10000",
    },
    {
        "id": "a100-node-02",
        "name": "HPE ProLiant DL380 Gen11 Server",
        "pool": "NVIDIA-A100-POOL-B",
        "hardware": "HPE ProLiant DL380 Gen11",
        "tier_id": "tier-100000",
    },
    {
        "id": "h100-node-03",
        "name": "ASUS ESC8000A-E11",
        "pool": "NVIDIA-H100-POOL-A",
        "hardware": "ASUS ESC8000A-E11",
        "tier_id": "tier-1000000",
    },
    {
        "id": "a100-node-03",
        "name": "NVIDIA HGX (H100/H200) 8-GPU",
        "pool": "NVIDIA-A100-POOL-B",
        "hardware": "NVIDIA HGX (H100/H200) 8-GPU",
        "tier_id": "tier-10000000",
    },
)

_cached_ton_usd: Decimal | None = None
_cached_ton_usd_at = 0.0
_price_lock = asyncio.Lock()


class PriceFeedError(RuntimeError):
    pass


def get_tier(tier_id: str) -> dict[str, str]:
    for tier in PRICING_TIERS:
        if tier["id"] == tier_id:
            return tier
    raise ValueError("Unknown server pricing tier")


def get_server_listing(server_id: str) -> dict[str, str]:
    for listing in SERVER_LISTINGS:
        if listing["id"] == server_id:
            return listing
    raise ValueError("Unknown server listing")


def _fetch_ton_usd_price() -> Decimal:
    request = Request(
        TON_USD_PRICE_URL,
        headers={"Accept": "application/json", "User-Agent": "MintAI-demo/1.0"},
    )
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read())
        price = Decimal(str(payload["the-open-network"]["usd"]))
    except Exception as exc:
        raise PriceFeedError("Live TON/USD quote is unavailable") from exc
    if not price.is_finite() or price <= 0:
        raise PriceFeedError("Live TON/USD quote is invalid")
    return price


async def get_ton_usd_price() -> Decimal:
    global _cached_ton_usd, _cached_ton_usd_at
    async with _price_lock:
        now = time.monotonic()
        if _cached_ton_usd is not None and now - _cached_ton_usd_at < TON_PRICE_CACHE_SECONDS:
            return _cached_ton_usd
        price = await asyncio.to_thread(_fetch_ton_usd_price)
        _cached_ton_usd = price
        _cached_ton_usd_at = time.monotonic()
        return price


async def price_tier(tier_id: str, currency: str) -> tuple[Decimal, Decimal | None]:
    tier = get_tier(tier_id)
    selected_currency = currency.upper()
    if selected_currency == "XTR":
        return Decimal(tier["stars"]), None
    usd_price = Decimal(tier["usd"])
    if selected_currency == "USDT":
        return usd_price, None
    if selected_currency == "TON":
        ton_usd = await get_ton_usd_price()
        ton_amount = (usd_price / ton_usd).quantize(PRICE_QUANTUM, rounding=ROUND_DOWN)
        if ton_amount <= 0:
            raise PriceFeedError("TON quote rounds below the supported amount precision")
        return ton_amount, ton_usd
    raise ValueError("Unsupported pricing currency")


def public_server_listings(ton_usd: Decimal | None) -> list[dict[str, object]]:
    listings: list[dict[str, object]] = []
    for listing in SERVER_LISTINGS:
        tier = get_tier(listing["tier_id"])
        ton_amount = (
            (Decimal(tier["usd"]) / ton_usd).quantize(PRICE_QUANTUM, rounding=ROUND_DOWN)
            if ton_usd is not None
            else None
        )
        listings.append(
            {
                **listing,
                "tier": dict(tier),
                "ton_amount": str(ton_amount) if ton_amount is not None else None,
                "daily_dividend_percent": (
                    str(daily_dividend_percent(listing["id"]))
                    if daily_dividend_percent(listing["id"]) is not None
                    else None
                ),
                "daily_dividend_amount": (
                    str(daily_dividend_amount(listing["id"], Decimal(tier["stars"])))
                    if daily_dividend_amount(listing["id"], Decimal(tier["stars"])) is not None
                    else None
                ),
            }
        )
    return listings
