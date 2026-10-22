import re

# Practical TON raw / user-friendly address check (not full CRC validation).
_TON_ADDRESS = re.compile(r"^(EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}$|^[A-Za-z0-9+/=_-]{48,66}$")

# TON user-friendly prefixes: mainnet uses EQ (bounceable) / UQ (non-bounceable),
# testnet uses kQ (bounceable) / 0Q (non-bounceable).
_MAINNET_PREFIXES = ("EQ", "UQ")


def is_plausible_payout_address(address: str) -> bool:
    cleaned = address.strip()
    if len(cleaned) < 10 or len(cleaned) > 128:
        return False
    return bool(_TON_ADDRESS.match(cleaned)) or cleaned.startswith("0:")


def is_mainnet_ton_address(address: str) -> bool:
    return address.strip().startswith(_MAINNET_PREFIXES)
