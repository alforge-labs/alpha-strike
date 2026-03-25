from typing import Literal

from pydantic import BaseModel, Field


class WebhookPayload(BaseModel):
    passphrase: str
    broker: Literal["ig", "moomoo"]
    asset_class: str
    action: Literal["buy", "sell"]
    ticker: str
    quantity: float = Field(gt=0, description="注文数量（株数またはロット数）")


class OrderResult(BaseModel):
    status: str
    broker: str
    ticker: str
    message: str
