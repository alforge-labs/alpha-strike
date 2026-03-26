from typing import Literal

from pydantic import BaseModel, Field


class WebhookPayload(BaseModel):
    passphrase: str = Field(repr=False)
    broker: Literal["oanda", "moomoo"]
    asset_class: Literal["FX", "COMMODITY", "US", "HK", "INDEX"]
    action: Literal["buy", "sell"]
    ticker: str = Field(
        pattern=r"^[A-Z0-9_.]{1,20}$",
        description="ティッカーシンボル（英大文字・数字・ドット・アンダースコアのみ、20文字以内）",
    )
    quantity: float = Field(gt=0, description="注文数量（株数またはロット数）")


class OrderResult(BaseModel):
    status: str
    broker: str
    ticker: str
    message: str
