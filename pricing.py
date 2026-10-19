from decimal import Decimal, ROUND_DOWN

from dividends import daily_dividend_amount, daily_dividend_percent


PRICE_QUANTUM = Decimal("0.00000001")

PRICING_TIERS: tuple[dict[str, str], ...] = (
    {"id": "tier-300", "label": "Entry", "ton": "4.50"},
    {"id": "tier-1000", "label": "Starter", "ton": "15.00"},
    {"id": "tier-10000", "label": "Builder", "ton": "150.00"},
    {"id": "tier-100000", "label": "Pro", "ton": "1500.00"},
    {"id": "tier-1000000", "label": "Scale", "ton": "15000.00"},
    {"id": "tier-10000000", "label": "Enterprise", "ton": "150000.00"},
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


def price_tier(tier_id: str) -> Decimal:
    tier = get_tier(tier_id)
    return Decimal(tier["ton"]).quantize(PRICE_QUANTUM, rounding=ROUND_DOWN)


def public_server_listings() -> list[dict[str, object]]:
    listings: list[dict[str, object]] = []
    for listing in SERVER_LISTINGS:
        tier = get_tier(listing["tier_id"])
        ton_amount = price_tier(tier["id"])
        listings.append(
            {
                **listing,
                "tier": dict(tier),
                "ton_amount": str(ton_amount),
                "daily_dividend_percent": (
                    str(daily_dividend_percent(listing["id"]))
                    if daily_dividend_percent(listing["id"]) is not None
                    else None
                ),
                "daily_dividend_amount": (
                    str(daily_dividend_amount(listing["id"], ton_amount))
                    if daily_dividend_amount(listing["id"], ton_amount) is not None
                    else None
                ),
            }
        )
    return listings
