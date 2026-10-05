import re

# Practical TON raw / user-friendly address check (not full CRC validation).
_TON_ADDRESS = re.compile(r"^(EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}$|^[A-Za-z0-9+/=_-]{48,66}$")


def is_plausible_payout_address(address: str) -> bool:
    cleaned = address.strip()
    if len(cleaned) < 10 or len(cleaned) > 128:
        return False
    return bool(_TON_ADDRESS.match(cleaned)) or cleaned.startswith("0:")
