"""TradingView Webhook サーバー

TradingViewからのアラート（JSON）を受け取り、OANDA証券またはmoomoo証券へ注文をルーティングします。

起動 (PyPI インストール後):
    alpha-strike --host 0.0.0.0 --port 8080

開発時 (uvicorn 直接):
    uv run uvicorn alpha_strike.webhook_server:app --host 0.0.0.0 --port 8080 --reload
"""

import hmac
import logging
import os
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from time import perf_counter

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from alpha_strike.event_logger import JsonlEventLogger
from alpha_strike.models import (
    EventIngestResult,
    OrderEvent,
    OrderResult,
    SignalEvent,
    TradeClosedEvent,
    TradeClosedPayload,
    WebhookPayload,
)
from alpha_strike.services.fill_service import FillEventService, _generate_id
from alpha_strike.services.idempotency import IdempotencyStore
from alpha_strike.services.order_service import OrderRouter, build_default_router

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
event_logger = JsonlEventLogger()


DEFAULT_MAINTENANCE_FILE = "/etc/alpha-strike/MAINTENANCE"


def _check_maintenance_mode() -> None:
    """Kill switch チェック。発注を受付停止状態にしたいときに 503 を返す。

    起動方法 (2 通り):

    1. **環境変数** `MAINTENANCE_MODE=1`
       - systemd 起動時に `.env` 経由で固定したい場合に使用
       - 切替えには `systemctl restart` が必要

    2. **ファイルフラグ** `${MAINTENANCE_FILE:-/etc/alpha-strike/MAINTENANCE}`
       - 即時切替えしたい場合に使用（restart 不要）
       - 停止: `echo "理由" | sudo tee /etc/alpha-strike/MAINTENANCE`
       - 解除: `sudo rm /etc/alpha-strike/MAINTENANCE`
       - ファイル内容を 503 detail に含めて TradingView 側のエラーログに理由を残せる

    `/health` には影響しない（外部ヘルスチェック / Cloudflare Tunnel 維持のため）。
    `_verify_passphrase` より前に呼ばれるため、maintenance 中の passphrase 試行はログに残らない。
    """
    if os.getenv("MAINTENANCE_MODE", "0") == "1":
        logger.warning("maintenance mode (env): orders not accepted")
        raise HTTPException(
            status_code=503,
            detail="alpha-strike maintenance mode — orders not accepted",
        )

    flag_path = Path(os.getenv("MAINTENANCE_FILE", DEFAULT_MAINTENANCE_FILE))
    if flag_path.exists():
        try:
            reason = flag_path.read_text().strip()
        except OSError as e:
            logger.warning("MAINTENANCE_FILE 読み取り失敗: %s", e)
            reason = ""
        reason = reason or "alpha-strike maintenance mode"
        logger.warning("maintenance mode (file=%s): %s", flag_path, reason)
        raise HTTPException(status_code=503, detail=f"maintenance: {reason}")


def _verify_passphrase(passphrase: str) -> None:
    expected_passphrase = os.getenv("WEBHOOK_PASSPHRASE", "")
    if not hmac.compare_digest(passphrase, expected_passphrase):
        logger.warning("不正なパスフレーズでアクセスがありました")
        raise HTTPException(status_code=401, detail="Unauthorized")


_REQUIRED_ENV_VARS = ["WEBHOOK_PASSPHRASE"]
_WARN_IF_UNSET_ENV_VARS = ["OANDA_API_KEY", "OANDA_ACCOUNT_ID"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """起動時に必須環境変数を検証し、サービスを初期化する。"""
    missing = [v for v in _REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        for var in missing:
            logger.critical(f"{var} が設定されていません。サーバーを起動できません。")
        sys.exit(1)

    unset = [v for v in _WARN_IF_UNSET_ENV_VARS if not os.getenv(v)]
    if unset:
        logger.warning(
            "以下の環境変数が未設定です（該当ブローカー使用時に失敗します）: %s",
            ", ".join(unset),
        )

    app.state.order_router = build_default_router()
    app.state.fill_service = FillEventService(event_logger)

    # Idempotency store: signal_id ベースの重複拒否（issue #41）
    try:
        ttl = float(os.getenv("IDEMPOTENCY_TTL_SECONDS", "600"))
    except ValueError:
        logger.warning("IDEMPOTENCY_TTL_SECONDS が数値ではありません、既定の 600 秒を使用")
        ttl = 600.0
    app.state.idempotency = IdempotencyStore(ttl_seconds=ttl)
    logger.info("idempotency store 初期化 (ttl=%s 秒)", ttl)

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

    - maintenance mode (env or file flag): 503 Service Unavailable
    - passphrase が環境変数と一致しない場合: 401 Unauthorized
    - 設定エラー（APIキー未設定等）: 500 Internal Server Error
    - 注文失敗（ネットワーク、API拒否等）: 502 Bad Gateway
    """
    _check_maintenance_mode()
    _verify_passphrase(payload.passphrase)

    order_router: OrderRouter = request.app.state.order_router
    fill_service: FillEventService = request.app.state.fill_service

    # Idempotency: payload.signal_id が指定されていれば重複検知（issue #41）。
    # 同じ signal_id が TTL 内に再到達した場合は broker に流さず 200 を返して
    # TradingView 側の自動リトライを止める（409 にすると無限リトライの危険）。
    if payload.signal_id:
        idem: IdempotencyStore = request.app.state.idempotency
        if not idem.check_and_record(payload.signal_id):
            logger.warning(
                "idempotency: duplicate signal_id rejected (signal_id=%s broker=%s ticker=%s)",
                payload.signal_id,
                payload.broker,
                payload.ticker,
            )
            return OrderResult(
                status="success",
                broker=payload.broker,
                ticker=payload.ticker,
                message="duplicate signal_id — already processed",
                signal_id=payload.signal_id,
            )

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
        result = order_router.route(payload)

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

        fill_event = fill_service.build(
            payload=payload,
            result=result if isinstance(result, dict) else {},
            signal_id=signal_id,
            internal_order_id=internal_order_id,
            broker_order_id=broker_order_id,
        )
        if fill_event is not None:
            for allocated_fill_event in fill_service.allocate(fill_event):
                event_logger.append(allocated_fill_event)
                trade_closed_event = fill_service.build_trade_closed(allocated_fill_event)
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
    _check_maintenance_mode()
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

    moomoo_host = os.getenv("MOOMOO_HOST", "127.0.0.1")
    try:
        moomoo_port = int(os.getenv("MOOMOO_PORT", "11111"))
    except ValueError:
        checks["moomoo"] = {"status": "error", "detail": "MOOMOO_PORT が不正な値です"}
        moomoo_port = None  # type: ignore[assignment]

    if moomoo_port is not None:
        try:
            with socket.create_connection((moomoo_host, moomoo_port), timeout=3):
                pass
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


if __name__ == "__main__":  # pragma: no cover - python -m alpha_strike.webhook_server 用
    from alpha_strike.cli import main as _cli_main

    _cli_main()
