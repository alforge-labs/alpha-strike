from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class WebhookPayload(BaseModel):
    passphrase: str = Field(repr=False)
    broker: Literal["oanda", "moomoo"]
    asset_class: Literal["FX", "COMMODITY", "US", "HK", "INDEX", "CRYPTO"]
    action: Literal["buy", "sell"]
    ticker: str = Field(
        pattern=r"^[A-Z0-9_.]{1,20}$",
        description="ティッカーシンボル（英大文字・数字・ドット・アンダースコアのみ、20文字以内）",
    )
    quantity: float = Field(gt=0, description="注文数量（株数またはロット数）")
    target_qty: float | None = Field(
        default=None,
        ge=0,
        description=(
            "目標絶対保有量（closed-loop 数量解決、#80）。指定時（moomoo のみ）は "
            "broker 実保有との差分から発注数量・方向を再解決する。"
            "未指定なら quantity を従来どおり delta（増減量）として発注する。"
        ),
    )
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
    portfolio_id: str | None = Field(
        default=None,
        pattern=r"^[a-zA-Z0-9_.-]{1,64}$",
        description=(
            "combine portfolio 識別子。alpha-forge の combine portfolio Pine "
            "(--combine-strategies) から発火する webhook で使用される。"
            "alpha-forge issue #980 で alert log → metrics 再構築に必要。"
        ),
    )
    sub_strategy_id: str | None = Field(
        default=None,
        pattern=r"^[a-zA-Z0-9_.-]{1,64}$",
        description=(
            "combine portfolio 内の個別 sub-strategy 識別子。"
            "alpha-forge issue #980 でポジション推移再構築に必要。"
        ),
    )


class OrderResult(BaseModel):
    # skipped: over-sell ガードが broker へ送らず意図的にスキップした（#oversell-guard）
    status: Literal["success", "error", "skipped"]
    broker: Literal["oanda", "moomoo"]
    ticker: str
    message: str
    signal_id: str | None = None
    order_id: str | None = None
    broker_order_id: str | None = None
    event_id: str | None = None


class EventIngestResult(BaseModel):
    status: str
    event_id: str
    message: str


class SignalEvent(BaseModel):
    event_type: Literal["signal_received"] = "signal_received"
    event_id: str
    signal_id: str
    occurred_at: datetime
    broker: Literal["oanda", "moomoo"]
    asset_class: Literal["FX", "COMMODITY", "US", "HK", "INDEX", "CRYPTO"]
    action: Literal["buy", "sell"]
    ticker: str
    quantity: float
    # 目標絶対保有量（closed-loop 数量解決、#80）。alert replay の観測性のため記録する
    target_qty: float | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    snapshot_id: str | None = None
    timeframe: str | None = None
    alert_timestamp: datetime | None = None
    run_mode: Literal["paper", "live"] = "live"
    alert_name: str | None = None
    # alpha-forge issue #980: combine portfolio Pine からの発火を識別するため
    portfolio_id: str | None = None
    sub_strategy_id: str | None = None


class OrderEvent(BaseModel):
    event_type: Literal["order_recorded"] = "order_recorded"
    event_id: str
    signal_id: str
    order_id: str
    occurred_at: datetime
    broker: Literal["oanda", "moomoo"]
    asset_class: Literal["FX", "COMMODITY", "US", "HK", "INDEX", "CRYPTO"]
    action: Literal["buy", "sell"]
    ticker: str
    quantity: float
    # skipped: over-sell ガードが broker へ送らず意図的にスキップした（#oversell-guard）
    status: Literal["accepted", "failed", "skipped"]
    request_latency_ms: int | None = None
    broker_order_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    snapshot_id: str | None = None
    run_mode: Literal["paper", "live"] = "live"
    error_type: str | None = None
    # alpha-forge issue #980
    portfolio_id: str | None = None
    sub_strategy_id: str | None = None


class FillEvent(BaseModel):
    event_type: Literal["fill_received"] = "fill_received"
    event_id: str
    signal_id: str
    order_id: str
    fill_id: str
    occurred_at: datetime
    broker: Literal["oanda", "moomoo"]
    asset_class: Literal["FX", "COMMODITY", "US", "HK", "INDEX", "CRYPTO"]
    action: Literal["buy", "sell"]
    ticker: str
    quantity: float
    filled_qty: float
    filled_price: float
    broker_order_id: str | None = None
    trade_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    snapshot_id: str | None = None
    run_mode: Literal["paper", "live"] = "live"
    commission: float | None = None
    slippage_bps: float | None = None
    # alpha-forge issue #980
    portfolio_id: str | None = None
    sub_strategy_id: str | None = None


class TradeClosedEvent(BaseModel):
    event_type: Literal["trade_closed"] = "trade_closed"
    event_id: str
    signal_id: str
    trade_id: str
    occurred_at: datetime
    closed_at: datetime
    broker: Literal["oanda", "moomoo"]
    asset_class: Literal["FX", "COMMODITY", "US", "HK", "INDEX", "CRYPTO"]
    action: Literal["buy", "sell"]
    ticker: str
    quantity: float
    entry_price: float
    exit_price: float
    gross_pnl: float
    net_pnl: float
    strategy_id: str | None = None
    strategy_version: str | None = None
    snapshot_id: str | None = None
    run_mode: Literal["paper", "live"] = "live"
    # alpha-forge issue #980
    portfolio_id: str | None = None
    sub_strategy_id: str | None = None
    commission: float | None = None
    exit_reason: str | None = None


class OrderReconciledEvent(BaseModel):
    """発注後に broker(OpenD) で最終 order status を照合した権威イベント (#57)。

    ``fill_received``（submission 応答ベースで楽観的）と異なり、本イベントは OpenD の
    実 order status / dealt_qty を反映する。同一 order_id について本イベントが存在する場合、
    下流（forge live 等）は fill_received より本イベントを優先して扱う。
    """

    event_type: Literal["order_reconciled"] = "order_reconciled"
    event_id: str
    signal_id: str
    order_id: str
    occurred_at: datetime
    broker: Literal["oanda", "moomoo"]
    asset_class: Literal["FX", "COMMODITY", "US", "HK", "INDEX", "CRYPTO"]
    action: Literal["buy", "sell"]
    ticker: str
    quantity: float
    # broker(OpenD) 由来の最終 status（例: FILLED_ALL / CANCELLED_ALL / NOT_FOUND）
    order_status: str
    dealt_qty: float = 0.0
    dealt_avg_price: float = 0.0
    # 実約定したか（FILLED 系 かつ dealt_qty>0）。submission≠fill の盲点をここで確定する。
    is_filled: bool = False
    broker_order_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    snapshot_id: str | None = None
    run_mode: Literal["paper", "live"] = "live"
    # alpha-forge issue #980
    portfolio_id: str | None = None
    sub_strategy_id: str | None = None


class SignalCarryoverQueuedEvent(BaseModel):
    """クローズ後着の SIMULATE シグナルを次の市場オープンへ持ち越す保留イベント (#89)。

    moomoo SIMULATE は GTC を約定させられず DAY もクローズ後は失効するため、米国市場の
    クローズ後に届いたシグナルは broker へ投げず本イベントとして永続化し、
    carryover 再発注ループが次のオープンで ``order_router.route`` 経由で再発注する。

    ``carryover_state``:
      - ``queued``: 再発注待ち。後続に signal_id ``f"{signal_id}_co"`` の order_recorded
        (status=accepted) が現れれば「解消済み」とみなす（last-wins・append-only）。
      - ``abandoned``: 再発注上限超過 / stale などで打ち切り。

    下流（forge live replay / alpha-visualizer Live）は本イベントを **未約定の保留** として
    扱い equity に計上しない（約定の権威イベントは従来どおり order_reconciled）。
    """

    event_type: Literal["signal_carryover_queued"] = "signal_carryover_queued"
    event_id: str
    signal_id: str
    occurred_at: datetime
    broker: Literal["oanda", "moomoo"]
    asset_class: Literal["FX", "COMMODITY", "US", "HK", "INDEX", "CRYPTO"]
    action: Literal["buy", "sell"]
    ticker: str
    quantity: float
    target_qty: float | None = None
    carryover_state: Literal["queued", "abandoned"] = "queued"
    strategy_id: str | None = None
    strategy_version: str | None = None
    snapshot_id: str | None = None
    timeframe: str | None = None
    alert_timestamp: datetime | None = None
    run_mode: Literal["paper", "live"] = "live"
    alert_name: str | None = None
    # alpha-forge issue #980
    portfolio_id: str | None = None
    sub_strategy_id: str | None = None


class SignalOutageDetectedEvent(BaseModel):
    """TradingView シグナルの途絶検知 / 復旧イベント。

    TradingView のアラートは現行プランで最大 1 ヶ月しか設定できず、期限が切れると
    サイレントに配信を停止する。サーバも OpenD も正常なまま webhook だけ止まるため、
    ``/status`` では異常が見えない。本イベントは ntfy 通知と対で残す検知の記録で、
    後から「何営業日落ちたか」を追えるようにする。

    ``outage_state``:
      - ``detected``: 途絶を検知して通知を試みた
      - ``recovered``: シグナル受信が再開して復旧通知を試みた

    下流（forge live replay / alpha-visualizer Live）は本イベントを **約定・保留の
    いずれでもない運用イベント** として扱い、equity には一切計上しない。
    """

    event_type: Literal["signal_outage_detected"] = "signal_outage_detected"
    event_id: str
    occurred_at: datetime
    broker: Literal["oanda", "moomoo"]
    outage_state: Literal["detected", "recovered"] = "detected"
    last_signal_at: datetime | None = None
    last_signal_id: str | None = None
    effective_hours: float
    threshold_hours: float


class TradeClosedPayload(BaseModel):
    passphrase: str = Field(repr=False)
    signal_id: str
    trade_id: str
    closed_at: datetime
    broker: Literal["oanda", "moomoo"]
    asset_class: Literal["FX", "COMMODITY", "US", "HK", "INDEX", "CRYPTO"]
    action: Literal["buy", "sell"]
    ticker: str
    quantity: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    exit_price: float = Field(gt=0)
    gross_pnl: float
    net_pnl: float
    strategy_id: str | None = None
    strategy_version: str | None = None
    snapshot_id: str | None = None
    run_mode: Literal["paper", "live"] = "live"
    commission: float | None = None
    exit_reason: str | None = None
