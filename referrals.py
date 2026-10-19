import hashlib
import hmac
import os
import re
from urllib.parse import quote, urlencode


REFERRAL_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 6
_CODE_SPACE = len(REFERRAL_ALPHABET) ** CODE_LENGTH


def generate_user_referral_code(tg_user_id: int, attempt: int = 0) -> str:
    if tg_user_id <= 0:
        raise ValueError("Telegram user ID must be a positive integer")
    if attempt < 0:
        raise ValueError("Referral-code attempt must not be negative")
    salt = os.getenv("REFERRAL_SALT", "").strip()
    if len(salt) < 32:
        raise RuntimeError("REFERRAL_SALT must contain at least 32 characters")
    digest = hmac.new(
        salt.encode("utf-8"),
        f"{tg_user_id}:{attempt}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    value = int.from_bytes(digest, "big") % _CODE_SPACE
    code = []
    for _ in range(CODE_LENGTH):
        value, index = divmod(value, len(REFERRAL_ALPHABET))
        code.append(REFERRAL_ALPHABET[index])
    return "".join(code)


def build_referral_link(code: str, bot_username: str | None = None) -> str:
    username = (bot_username or os.getenv("TELEGRAM_BOT_USERNAME", "")).strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise RuntimeError("TELEGRAM_BOT_USERNAME must be a valid Telegram bot username")
    app_short_name = os.getenv("TELEGRAM_MINI_APP_SHORT_NAME", "").strip()
    if app_short_name:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", app_short_name):
            raise RuntimeError("TELEGRAM_MINI_APP_SHORT_NAME is invalid")
        return (
            f"https://t.me/{username}/{quote(app_short_name, safe='')}"
            f"?startapp={quote(code, safe='')}"
        )
    query = urlencode({"start": code})
    return f"https://t.me/{username}?{query}"
