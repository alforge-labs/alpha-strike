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
from models import OrderEvent, OrderResult, SignalEvent, WebhookPayload

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


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    expected_passphrase = os.getenv("WEBHOOK_PASSPHRASE", "")
    if not hmac.compare_digest(payload.passphrase, expected_passphrase):
        logger.warning("不正なパスフレーズでアクセスがありました")
        raise HTTPException(status_code=401, detail="Unauthorized")

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
        broker_order_id = (
            str(result.get("order_id")) if isinstance(result, dict) and result.get("order_id") else None
        )
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
    moomoo_port = int(os.getenv("MOOMOO_PORT", "11111"))
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8080, reload=False)
