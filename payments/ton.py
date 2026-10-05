import hashlib
import hmac
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class TonTransferEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=128)
    intent_id: str = Field(min_length=36, max_length=36)
    tx_hash: str = Field(min_length=32, max_length=128)
    tx_log_index: int = Field(ge=0)
    amount: Decimal = Field(gt=0, max_digits=24, decimal_places=8)
    currency: str = Field(pattern="^(TON|USDT)$")
    memo: str = Field(min_length=1, max_length=64)
    to_address: str = Field(min_length=10, max_length=128)
    token_master: str | None = Field(default=None, max_length=128)
    confirmations: int = Field(ge=0)
    network: str = Field(min_length=1, max_length=16)


class TonIndexerAdapter:
    def __init__(self, webhook_secret: str, minimum_confirmations: int = 1) -> None:
        self.webhook_secret = webhook_secret.encode("utf-8")
        self.minimum_confirmations = minimum_confirmations

    def verify_signature(self, body: bytes, supplied_signature: str) -> bool:
        if not self.webhook_secret or not supplied_signature:
            return False
        digest = hmac.new(self.webhook_secret, body, hashlib.sha256).hexdigest()
        signature = supplied_signature.removeprefix("sha256=").strip()
        return hmac.compare_digest(digest, signature)

    def parse_event(self, body: bytes) -> TonTransferEvent:
        return TonTransferEvent.model_validate_json(body)

    def validate_event(
        self,
        event: TonTransferEvent,
        *,
        network: str,
        deposit_address: str,
        usdt_jetton_master: str,
    ) -> None:
        if event.network.lower() != network.lower():
            raise ValueError("TON event is for the wrong network")
        if event.confirmations < self.minimum_confirmations:
            raise ValueError("TON transfer has not reached the required confirmations")
        if event.to_address != deposit_address:
            raise ValueError("TON transfer destination does not match the configured deposit address")
        if event.currency == "USDT":
            if not usdt_jetton_master or event.token_master != usdt_jetton_master:
                raise ValueError("USDT transfer is not from the configured jetton master")
        elif event.token_master is not None:
            raise ValueError("Native TON transfer must not specify a jetton master")
