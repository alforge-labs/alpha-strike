"""TradingView Webhook サーバー

TradingViewからのアラート（JSON）を受け取り、OANDA証券またはmoomoo証券へ注文をルーティングします。

起動:
    uv run uvicorn webhook_server:app --host 0.0.0.0 --port 8080 --reload
"""

import hmac
import logging
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from handlers import moomoo_order_handler, oanda_order_handler
from models import OrderResult, WebhookPayload

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時に必須環境変数を検証する。"""
    passphrase = os.getenv("WEBHOOK_PASSPHRASE", "")
    if not passphrase:
        logger.critical("WEBHOOK_PASSPHRASE が設定されていません。サーバーを起動できません。")
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
async def receive_webhook(request: Request, payload: WebhookPayload) -> OrderResult:  # noqa: ARG001
    """TradingViewからのWebhookを受け取り、指定ブローカーへ注文を送信する。

    - passphrase が環境変数と一致しない場合: 401 Unauthorized
    - 設定エラー（APIキー未設定等）: 500 Internal Server Error
    - 注文失敗（ネットワーク、API拒否等）: 502 Bad Gateway
    """
    expected_passphrase = os.getenv("WEBHOOK_PASSPHRASE", "")
    if not hmac.compare_digest(payload.passphrase, expected_passphrase):
        logger.warning("不正なパスフレーズでアクセスがありました")
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info(
        "Webhook受信: broker=%s ticker=%s action=%s qty=%s",
        payload.broker, payload.ticker, payload.action, payload.quantity,
    )

    try:
        if payload.broker == "oanda":
            result = oanda_order_handler(payload)
        else:  # "moomoo" — Literalで保証済み
            result = moomoo_order_handler(payload)

        logger.info(
            "注文成功: broker=%s ticker=%s action=%s qty=%s",
            payload.broker, payload.ticker, payload.action, payload.quantity,
        )
        return OrderResult(
            status="success",
            broker=payload.broker,
            ticker=payload.ticker,
            message=str(result),
        )

    except HTTPException:
        raise
    except (ValueError, ImportError) as e:
        logger.error("設定エラー: %s", e)
        raise HTTPException(status_code=500, detail="設定エラーが発生しました。管理者にお問い合わせください。") from e
    except Exception as e:
        logger.error(
            "注文失敗: broker=%s ticker=%s error=%s",
            payload.broker, payload.ticker, e,
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail="注文の実行に失敗しました。しばらくしてから再試行してください。") from e


@app.get("/health")
async def health_check() -> dict:
    """サーバーの死活確認用エンドポイント。"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8080, reload=False)
