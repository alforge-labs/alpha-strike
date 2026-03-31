from datetime import datetime
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
    strategy_id: str | None = Field(
        default=None,
        pattern=r"^[a-zA-Z0-9_.-]{1,64}$",
        description="alpha-forge 側の strategy_id",
    )
    strategy_version: str | None = Field(
        default=None,
        max_length=32,
        description="戦略バージョン",
    )
    snapshot_id: str | None = Field(
        default=None,
        pattern=r"^snap_[0-9]{20}$",
        description="alpha-forge journal snapshot_id",
    )
    signal_id: str | None = Field(
        default=None,
        max_length=80,
        description="シグナル一意ID。未指定なら alpha-strike 側で採番",
    )
    timeframe: str | None = Field(
        default=None,
        max_length=16,
        description="例: 1m, 5m, 1h, 1d",
    )
    alert_timestamp: datetime | None = Field(
        default=None,
        description="TradingView 側でシグナルが発火した時刻",
    )
    run_mode: Literal["paper", "live"] = Field(
        default="live",
        description="paper は模擬運用・ログ用途",
    )
    alert_name: str | None = Field(
        default=None,
        max_length=128,
        description="TradingView アラート名",
    )
    order_comment: str | None = Field(
        default=None,
        max_length=256,
        description="任意メモ",
    )


class OrderResult(BaseModel):
    status: Literal["success", "error"]
    broker: Literal["oanda", "moomoo"]
    ticker: str
    message: str
    signal_id: str | None = None
    order_id: str | None = None
    broker_order_id: str | None = None
    event_id: str | None = None


class SignalEvent(BaseModel):
    event_type: Literal["signal_received"] = "signal_received"
    event_id: str
    signal_id: str
    occurred_at: datetime
    broker: Literal["oanda", "moomoo"]
    asset_class: Literal["FX", "COMMODITY", "US", "HK", "INDEX"]
    action: Literal["buy", "sell"]
    ticker: str
    quantity: float
    strategy_id: str | None = None
    strategy_version: str | None = None
    snapshot_id: str | None = None
    timeframe: str | None = None
    alert_timestamp: datetime | None = None
    run_mode: Literal["paper", "live"] = "live"
    alert_name: str | None = None


class OrderEvent(BaseModel):
    event_type: Literal["order_recorded"] = "order_recorded"
    event_id: str
    signal_id: str
    order_id: str
    occurred_at: datetime
    broker: Literal["oanda", "moomoo"]
    asset_class: Literal["FX", "COMMODITY", "US", "HK", "INDEX"]
    action: Literal["buy", "sell"]
    ticker: str
    quantity: float
    status: Literal["accepted", "failed"]
    request_latency_ms: int | None = None
    broker_order_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    snapshot_id: str | None = None
    run_mode: Literal["paper", "live"] = "live"
    error_type: str | None = None
