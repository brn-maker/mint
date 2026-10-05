import hashlib
import os
from urllib.parse import urlencode


REFERRAL_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 6
_CODE_SPACE = len(REFERRAL_ALPHABET) ** CODE_LENGTH


def generate_user_referral_code(tg_user_id: int) -> str:
    if tg_user_id <= 0:
        raise ValueError("Telegram user ID must be a positive integer")

    salt = os.getenv("REFERRAL_SALT", "mintai-local-development-referral-salt")
    digest = hashlib.sha256(f"{salt}:{tg_user_id}".encode("utf-8")).digest()
    value = int.from_bytes(digest, "big") % _CODE_SPACE
    code = []
    for _ in range(CODE_LENGTH):
        value, index = divmod(value, len(REFERRAL_ALPHABET))
        code.append(REFERRAL_ALPHABET[index])
    return "".join(code)


def build_referral_link(code: str, bot_username: str | None = None) -> str:
    username = (bot_username or os.getenv("TELEGRAM_BOT_USERNAME", "mintai_demo_bot")).lstrip("@")
    query = urlencode({"start": code})
    return f"https://t.me/{username}?{query}"
