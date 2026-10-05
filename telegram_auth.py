import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl


class TelegramAuthError(ValueError):
    pass


def validate_telegram_init_data(init_data: str, bot_token: str) -> dict[str, object]:
    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        supplied_hash = values.pop("hash")
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TelegramAuthError("Malformed Telegram init data") from exc

    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_hash, expected_hash):
        raise TelegramAuthError("Invalid Telegram init data signature")
    max_age = int(os.getenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "86400"))
    if auth_date > int(time.time()) + 60 or int(time.time()) - auth_date > max_age:
        raise TelegramAuthError("Telegram init data has expired")
    if not isinstance(user, dict) or not isinstance(user.get("id"), int) or user["id"] <= 0:
        raise TelegramAuthError("Telegram init data contains no valid user")
    return user
