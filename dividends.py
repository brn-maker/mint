from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN


DIVIDEND_QUANTUM = Decimal("0.00000001")
DAILY_DIVIDEND_RATES: dict[str, Decimal] = {
    "h100-node-01": Decimal("0.20"),
    "h100-node-02": Decimal("0.25"),
    "a100-node-01": Decimal("0.30"),
    "a100-node-02": Decimal("0.35"),
    "h100-node-03": Decimal("0.40"),
    "a100-node-03": Decimal("0.45"),
}


def daily_dividend_percent(server_id: str) -> Decimal | None:
    rate = DAILY_DIVIDEND_RATES.get(server_id)
    return rate * Decimal("100") if rate is not None else None


def daily_dividend_amount(server_id: str, principal: Decimal) -> Decimal | None:
    rate = DAILY_DIVIDEND_RATES.get(server_id)
    if rate is None:
        return None
    return (principal * rate).quantize(DIVIDEND_QUANTUM, rounding=ROUND_DOWN)


def accrued_dividend(server_id: str, principal: Decimal, elapsed_seconds: float) -> Decimal:
    rate = DAILY_DIVIDEND_RATES.get(server_id)
    if rate is None or elapsed_seconds <= 0:
        return Decimal("0")
    amount = principal * rate * Decimal(str(elapsed_seconds)) / Decimal("86400")
    return amount.quantize(DIVIDEND_QUANTUM, rounding=ROUND_DOWN)


def utc_midnights_after(start: datetime, end: datetime) -> list[datetime]:
    start = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start.astimezone(timezone.utc)
    end = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end.astimezone(timezone.utc)
    boundary = datetime(
        start.year, start.month, start.day, tzinfo=timezone.utc
    ) + timedelta(days=1)
    boundaries = []
    while boundary <= end:
        boundaries.append(boundary)
        boundary += timedelta(days=1)
    return boundaries


def ghost_week_start(now: datetime) -> datetime:
    reset_timezone = timezone(timedelta(hours=3))
    local_now = now.astimezone(reset_timezone)
    monday = local_now.date() - timedelta(days=local_now.weekday())
    return datetime.combine(monday, datetime.min.time(), tzinfo=reset_timezone).astimezone(
        timezone.utc
    )
