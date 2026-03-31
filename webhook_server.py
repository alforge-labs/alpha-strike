"""TradingView Webhook サーバー

TradingViewからのアラート（JSON）を受け取り、OANDA証券またはmoomoo証券へ注文をルーティングします。

起動:
    uv run uvicorn webhook_server:app --host 0.0.0.0 --port 8080 --reload
"""

import hmac
import logging
import os
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from time import perf_counter

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from event_logger import JsonlEventLogger
from handlers import moomoo_order_handler, oanda_order_handler
from models import (
    EventIngestResult,
    FillEvent,
    OrderEvent,
    OrderResult,
    SignalEvent,
    TradeClosedEvent,
    TradeClosedPayload,
    WebhookPayload,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
event_logger = JsonlEventLogger()


def _generate_id(prefix: str) -> str:
    """タイムスタンプベースの識別子を生成する。"""
    return f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"


def _verify_passphrase(passphrase: str) -> None:
    expected_passphrase = os.getenv("WEBHOOK_PASSPHRASE", "")
    if not hmac.compare_digest(passphrase, expected_passphrase):
        logger.warning("不正なパスフレーズでアクセスがありました")
        raise HTTPException(status_code=401, detail="Unauthorized")


def _build_fill_event(
    payload: WebhookPayload,
    result: dict,
    signal_id: str,
    internal_order_id: str,
    broker_order_id: str | None,
) -> FillEvent | None:
    filled_qty_raw = result.get("filled_qty")
    filled_price_raw = result.get("filled_price")
    if filled_qty_raw is None or filled_price_raw is None:
        return None

    fill_id = str(result.get("fill_id") or _generate_id("fill"))
    trade_id = str(result.get("trade_id") or f"trd_{fill_id}")
    return FillEvent(
        event_id=_generate_id("evt"),
        signal_id=signal_id,
        order_id=internal_order_id,
        fill_id=fill_id,
        occurred_at=datetime.now(),
        broker=payload.broker,
        asset_class=payload.asset_class,
        action=payload.action,
        ticker=payload.ticker,
        quantity=payload.quantity,
        filled_qty=float(filled_qty_raw),
        filled_price=float(filled_price_raw),
        broker_order_id=broker_order_id,
        trade_id=trade_id,
        strategy_id=payload.strategy_id,
        strategy_version=payload.strategy_version,
        snapshot_id=payload.snapshot_id,
        run_mode=payload.run_mode,
        commission=float(result["commission"]) if result.get("commission") is not None else None,
        slippage_bps=float(result["slippage_bps"]) if result.get("slippage_bps") is not None else None,
    )


def _resolve_trade_id(event: dict) -> str:
    return str(event.get("trade_id") or f"trd_{event.get('fill_id')}")


def _load_recent_fill_events(fill_event: FillEvent) -> list[dict]:
    return event_logger.load_events(
        broker=fill_event.broker,
        event_type="fill_received",
        ticker=fill_event.ticker,
        strategy_id=fill_event.strategy_id,
        limit=200,
    )


def _allocate_fill_events(fill_event: FillEvent) -> list[FillEvent]:
    if fill_event.broker not in {"moomoo", "oanda"}:
        return [fill_event]

    recent_fills = _load_recent_fill_events(fill_event)

    opening_action = "sell" if fill_event.action == "buy" else "buy"
    entry_by_trade_id: dict[str, dict] = {}
    for event in recent_fills:
        trade_id = _resolve_trade_id(event)
        if event.get("run_mode") != fill_event.run_mode:
            continue
        if event.get("action") != opening_action:
            continue
        if event.get("filled_qty") is None:
            continue
        summary = entry_by_trade_id.setdefault(
            trade_id,
            {
                "trade_id": trade_id,
                "signal_id": event.get("signal_id"),
                "filled_qty": 0.0,
                "first_occurred_at": event.get("occurred_at"),
            },
        )
        summary["filled_qty"] += float(event.get("filled_qty") or 0.0)
        if summary["first_occurred_at"] is None or (
            event.get("occurred_at") is not None
            and event["occurred_at"] < summary["first_occurred_at"]
        ):
            summary["first_occurred_at"] = event.get("occurred_at")

    matched_exit_qty_by_trade_id: dict[str, float] = {}
    for event in recent_fills:
        trade_id = _resolve_trade_id(event)
        if event.get("action") != fill_event.action:
            continue
        if event.get("filled_qty") is None:
            continue
        matched_exit_qty_by_trade_id[trade_id] = (
            matched_exit_qty_by_trade_id.get(trade_id, 0.0)
            + float(event.get("filled_qty") or 0.0)
        )

    candidate_entries: list[dict] = []
    for summary in entry_by_trade_id.values():
        remaining_qty = summary["filled_qty"] - matched_exit_qty_by_trade_id.get(
            summary["trade_id"],
            0.0,
        )
        if remaining_qty <= 0:
            continue
        candidate_entries.append(
            {
                **summary,
                "remaining_qty": remaining_qty,
            }
        )

    candidate_entries.sort(key=lambda item: item["first_occurred_at"] or "")
    if not candidate_entries:
        return [fill_event]

    remaining_close_qty = float(fill_event.filled_qty)
    allocated_events: list[FillEvent] = []
    for index, candidate in enumerate(candidate_entries, start=1):
        if remaining_close_qty <= 0:
            break
        allocated_qty = min(remaining_close_qty, float(candidate["remaining_qty"]))
        if allocated_qty <= 0:
            continue
        allocated_events.append(
            fill_event.model_copy(
                update={
                    "event_id": fill_event.event_id if index == 1 else _generate_id("evt"),
                    "fill_id": fill_event.fill_id if index == 1 else f"{fill_event.fill_id}_{index}",
                    "trade_id": candidate["trade_id"],
                    "signal_id": candidate.get("signal_id") or fill_event.signal_id,
                    "quantity": allocated_qty,
                    "filled_qty": allocated_qty,
                }
            )
        )
        remaining_close_qty -= allocated_qty

    if remaining_close_qty > 0:
        allocated_events.append(
            fill_event.model_copy(
                update={
                    "event_id": _generate_id("evt") if allocated_events else fill_event.event_id,
                    "fill_id": (
                        f"{fill_event.fill_id}_residual"
                        if allocated_events
                        else fill_event.fill_id
                    ),
                    "trade_id": f"trd_{fill_event.fill_id}_reversal",
                    "quantity": remaining_close_qty,
                    "filled_qty": remaining_close_qty,
                }
            )
        )

    return allocated_events or [fill_event]


def _build_trade_closed_from_opposite_fill(fill_event: FillEvent) -> TradeClosedEvent | None:
    if fill_event.broker not in {"moomoo", "oanda"}:
        return None
    if fill_event.trade_id is None:
        return None

    recent_trade_closed = event_logger.load_events(
        broker=fill_event.broker,
        event_type="trade_closed",
        ticker=fill_event.ticker,
        strategy_id=fill_event.strategy_id,
        limit=200,
    )
    closed_trade_ids = {
        str(event["trade_id"])
        for event in recent_trade_closed
        if event.get("trade_id") is not None
    }
    if fill_event.trade_id in closed_trade_ids:
        return None

    recent_fills = _load_recent_fill_events(fill_event)
    trade_fills = [
        event for event in recent_fills if _resolve_trade_id(event) == fill_event.trade_id
    ]
    if not trade_fills:
        return None

    entry_fills = [event for event in trade_fills if event.get("action") != fill_event.action]
    if not entry_fills:
        return None

    opening_action = str(entry_fills[0]["action"])
    exit_fills = [event for event in trade_fills if event.get("action") == fill_event.action]
    entry_qty = sum(float(event.get("filled_qty") or 0.0) for event in entry_fills)
    total_exit_qty = sum(float(event.get("filled_qty") or 0.0) for event in exit_fills)
    if total_exit_qty < entry_qty or entry_qty <= 0:
        return None

    entry_notional = sum(
        float(event["filled_price"]) * float(event["filled_qty"])
        for event in entry_fills
        if event.get("filled_price") is not None and event.get("filled_qty") is not None
    )
    exit_notional = sum(
        float(event["filled_price"]) * float(event["filled_qty"])
        for event in exit_fills
        if event.get("filled_price") is not None and event.get("filled_qty") is not None
    )
    entry_price = entry_notional / entry_qty
    exit_price = exit_notional / total_exit_qty
    qty = entry_qty

    gross_pnl = (
        (exit_price - entry_price) * qty
        if opening_action == "buy"
        else (entry_price - exit_price) * qty
    )
    gross_pnl = round(gross_pnl, 10)
    total_commission = sum(
        float(event["commission"]) for event in trade_fills if event.get("commission") is not None
    )
    net_pnl = round(gross_pnl - total_commission, 10)
    first_entry_fill = min(
        entry_fills,
        key=lambda event: event.get("occurred_at") or "",
    )

    return TradeClosedEvent(
        event_id=_generate_id("evt"),
        signal_id=str(first_entry_fill["signal_id"]),
        trade_id=fill_event.trade_id,
        occurred_at=datetime.now(),
        closed_at=fill_event.occurred_at,
        broker=fill_event.broker,
        asset_class=fill_event.asset_class,
        action=opening_action,
        ticker=fill_event.ticker,
        quantity=qty,
        entry_price=entry_price,
        exit_price=exit_price,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        strategy_id=fill_event.strategy_id,
        strategy_version=fill_event.strategy_version,
        snapshot_id=fill_event.snapshot_id,
        run_mode=fill_event.run_mode,
        commission=total_commission,
        exit_reason="opposite_fill",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """起動時に必須環境変数を検証する。"""
    passphrase = os.getenv("WEBHOOK_PASSPHRASE", "")
    if not passphrase:
        logger.critical(
            "WEBHOOK_PASSPHRASE が設定されていません。サーバーを起動できません。"
        )
        sys.exit(1)
    logger.info("Alpha-Strike Webhook サーバー起動完了")
    yield
    logger.info("Alpha-Strike Webhook サーバー停止")


app = FastAPI(
    title="Alpha-Strike Webhook Server",
    description="TradingViewアラートをOANDA証券・moomoo証券へ自動ルーティングするWebhookサーバー",
    version="0.1.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.post("/webhook", response_model=OrderResult, status_code=200)
@limiter.limit("10/minute")
async def receive_webhook(
    request: Request, payload: WebhookPayload
) -> OrderResult:  # noqa: ARG001
    """TradingViewからのWebhookを受け取り、指定ブローカーへ注文を送信する。

    - passphrase が環境変数と一致しない場合: 401 Unauthorized
    - 設定エラー（APIキー未設定等）: 500 Internal Server Error
    - 注文失敗（ネットワーク、API拒否等）: 502 Bad Gateway
    """
    _verify_passphrase(payload.passphrase)

    signal_id = payload.signal_id or _generate_id("sig")
    signal_event = SignalEvent(
        event_id=_generate_id("evt"),
        signal_id=signal_id,
        occurred_at=datetime.now(),
        broker=payload.broker,
        asset_class=payload.asset_class,
        action=payload.action,
        ticker=payload.ticker,
        quantity=payload.quantity,
        strategy_id=payload.strategy_id,
        strategy_version=payload.strategy_version,
        snapshot_id=payload.snapshot_id,
        timeframe=payload.timeframe,
        alert_timestamp=payload.alert_timestamp,
        run_mode=payload.run_mode,
        alert_name=payload.alert_name,
    )
    event_logger.append(signal_event)

    logger.info(
        "Webhook受信: broker=%s ticker=%s action=%s qty=%s",
        payload.broker,
        payload.ticker,
        payload.action,
        payload.quantity,
    )

    started_at = perf_counter()
    internal_order_id = _generate_id("ord")

    try:
        if payload.broker == "oanda":
            result = oanda_order_handler(payload)
        else:  # "moomoo" — Literalで保証済み
            result = moomoo_order_handler(payload)

        latency_ms = int((perf_counter() - started_at) * 1000)
        _oid = result.get("order_id") if isinstance(result, dict) else None
        broker_order_id = str(_oid) if _oid is not None else None
        order_event = OrderEvent(
            event_id=_generate_id("evt"),
            signal_id=signal_id,
            order_id=internal_order_id,
            occurred_at=datetime.now(),
            broker=payload.broker,
            asset_class=payload.asset_class,
            action=payload.action,
            ticker=payload.ticker,
            quantity=payload.quantity,
            status="accepted",
            request_latency_ms=latency_ms,
            broker_order_id=broker_order_id,
            strategy_id=payload.strategy_id,
            strategy_version=payload.strategy_version,
            snapshot_id=payload.snapshot_id,
            run_mode=payload.run_mode,
        )
        event_logger.append(order_event)
        fill_event = _build_fill_event(
            payload=payload,
            result=result if isinstance(result, dict) else {},
            signal_id=signal_id,
            internal_order_id=internal_order_id,
            broker_order_id=broker_order_id,
        )
        if fill_event is not None:
            allocated_fill_events = _allocate_fill_events(fill_event)
            for allocated_fill_event in allocated_fill_events:
                event_logger.append(allocated_fill_event)
                trade_closed_event = _build_trade_closed_from_opposite_fill(
                    allocated_fill_event
                )
                if trade_closed_event is not None:
                    event_logger.append(trade_closed_event)

        logger.info(
            "注文成功: broker=%s ticker=%s action=%s qty=%s",
            payload.broker,
            payload.ticker,
            payload.action,
            payload.quantity,
        )
        return OrderResult(
            status="success",
            broker=payload.broker,
            ticker=payload.ticker,
            message=str(result),
            signal_id=signal_id,
            order_id=internal_order_id,
            broker_order_id=broker_order_id,
            event_id=order_event.event_id,
        )

    except HTTPException:
        raise
    except (ValueError, ImportError) as e:
        latency_ms = int((perf_counter() - started_at) * 1000)
        event_logger.append(
            OrderEvent(
                event_id=_generate_id("evt"),
                signal_id=signal_id,
                order_id=internal_order_id,
                occurred_at=datetime.now(),
                broker=payload.broker,
                asset_class=payload.asset_class,
                action=payload.action,
                ticker=payload.ticker,
                quantity=payload.quantity,
                status="failed",
                request_latency_ms=latency_ms,
                strategy_id=payload.strategy_id,
                strategy_version=payload.strategy_version,
                snapshot_id=payload.snapshot_id,
                run_mode=payload.run_mode,
                error_type=type(e).__name__,
            )
        )
        logger.error("設定エラー: %s", e)
        raise HTTPException(
            status_code=500,
            detail="設定エラーが発生しました。管理者にお問い合わせください。",
        ) from e
    except Exception as e:
        latency_ms = int((perf_counter() - started_at) * 1000)
        event_logger.append(
            OrderEvent(
                event_id=_generate_id("evt"),
                signal_id=signal_id,
                order_id=internal_order_id,
                occurred_at=datetime.now(),
                broker=payload.broker,
                asset_class=payload.asset_class,
                action=payload.action,
                ticker=payload.ticker,
                quantity=payload.quantity,
                status="failed",
                request_latency_ms=latency_ms,
                strategy_id=payload.strategy_id,
                strategy_version=payload.strategy_version,
                snapshot_id=payload.snapshot_id,
                run_mode=payload.run_mode,
                error_type=type(e).__name__,
            )
        )
        logger.error(
            "注文失敗: broker=%s ticker=%s error=%s",
            payload.broker,
            payload.ticker,
            e,
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail="注文の実行に失敗しました。しばらくしてから再試行してください。",
        ) from e


@app.post("/events/trade-closed", response_model=EventIngestResult, status_code=200)
@limiter.limit("30/minute")
async def ingest_trade_closed_event(
    request: Request, payload: TradeClosedPayload
) -> EventIngestResult:  # noqa: ARG001
    """broker poller / callback 由来の trade_closed を保存する。"""
    _verify_passphrase(payload.passphrase)

    event = TradeClosedEvent(
        event_id=_generate_id("evt"),
        signal_id=payload.signal_id,
        trade_id=payload.trade_id,
        occurred_at=datetime.now(),
        closed_at=payload.closed_at,
        broker=payload.broker,
        asset_class=payload.asset_class,
        action=payload.action,
        ticker=payload.ticker,
        quantity=payload.quantity,
        entry_price=payload.entry_price,
        exit_price=payload.exit_price,
        gross_pnl=payload.gross_pnl,
        net_pnl=payload.net_pnl,
        strategy_id=payload.strategy_id,
        strategy_version=payload.strategy_version,
        snapshot_id=payload.snapshot_id,
        run_mode=payload.run_mode,
        commission=payload.commission,
        exit_reason=payload.exit_reason,
    )
    event_logger.append(event)

    logger.info(
        "trade_closed 保存: broker=%s ticker=%s trade_id=%s",
        payload.broker,
        payload.ticker,
        payload.trade_id,
    )
    return EventIngestResult(
        status="accepted",
        event_id=event.event_id,
        message="trade_closed event recorded",
    )


@app.get("/health")
async def health_check() -> dict:
    """サーバーの死活確認用エンドポイント（liveness probe）。"""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> dict:
    """依存サービスの疎通を確認する readiness probe。

    - OANDA: 環境変数の存在を確認
    - moomoo: OpenD への TCP 接続を確認

    全チェック通過時: HTTP 200 {"status": "ready", ...}
    いずれか失敗時: HTTP 503 {"status": "degraded", ...}
    """
    checks: dict[str, dict] = {}

    # OANDA チェック: 環境変数の存在確認（詳細は内部ログのみ、レスポンスには含めない）
    oanda_key = os.getenv("OANDA_API_KEY", "")
    oanda_account = os.getenv("OANDA_ACCOUNT_ID", "")
    if oanda_key and oanda_account:
        checks["oanda"] = {"status": "ok"}
    else:
        logger.warning(
            "OANDA 設定が不完全です: API_KEY=%s ACCOUNT_ID=%s",
            bool(oanda_key),
            bool(oanda_account),
        )
        checks["oanda"] = {"status": "error", "detail": "OANDA の設定が不完全です"}

    # moomoo チェック: OpenD への TCP 接続確認（詳細は内部ログのみ）
    moomoo_host = os.getenv("MOOMOO_HOST", "127.0.0.1")
    try:
        moomoo_port = int(os.getenv("MOOMOO_PORT", "11111"))
    except ValueError:
        checks["moomoo"] = {"status": "error", "detail": "MOOMOO_PORT が不正な値です"}
        moomoo_port = None  # type: ignore[assignment]

    if moomoo_port is not None:
        try:
            with socket.create_connection((moomoo_host, moomoo_port), timeout=3):
                pass  # 接続確認のみ。コンテキスト終了時に自動クローズ
            checks["moomoo"] = {"status": "ok"}
        except (OSError, socket.timeout) as e:
            logger.warning("OpenD への接続確認に失敗: %s", e)
            checks["moomoo"] = {"status": "error", "detail": "OpenD に接続できません"}

    all_ok = all(c["status"] == "ok" for c in checks.values())
    status = "ready" if all_ok else "degraded"
    http_status = 200 if all_ok else 503

    return JSONResponse(
        status_code=http_status, content={"status": status, "checks": checks}
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8080, reload=False)
