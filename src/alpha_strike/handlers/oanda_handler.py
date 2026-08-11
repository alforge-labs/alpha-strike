"""OANDA証券 REST API v20 を使用した注文ハンドラー"""

import logging
import os

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from alpha_strike.models import WebhookPayload

logger = logging.getLogger(__name__)

OANDA_PRACTICE_URL = "https://api-fxpractice.oanda.com"
OANDA_LIVE_URL = "https://api-fxtrade.oanda.com"

def resolve_oanda_env() -> str:
    """環境変数 OANDA_ENV を正規化して返す。

    発注時（OandaHandler.execute）と readiness probe（/health/ready）の双方から
    呼ばれる。両者で判定が割れると「readiness は ok なのに発注が全部落ちる」状態を
    見逃すため、解釈は必ずこの 1 箇所に集約する。

    Returns:
        "PRACTICE" または "LIVE"

    Raises:
        ValueError: PRACTICE / LIVE 以外の値が設定されている場合
    """
    oanda_env = os.getenv("OANDA_ENV", "PRACTICE").upper()
    if oanda_env not in ("PRACTICE", "LIVE"):
        raise ValueError(
            f"OANDA_ENV は PRACTICE または LIVE である必要があります: {oanda_env!r}"
        )
    return oanda_env


def _is_retryable_oanda_error(exc: Exception) -> bool:
    """5xx エラーおよびネットワーク一時障害のみリトライ対象とする。4xx（設定ミス等）はリトライしない。"""
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code >= 500
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


@retry(
    retry=retry_if_exception(_is_retryable_oanda_error),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _call_oanda_api(url: str, body: dict, headers: dict) -> dict:
    """OANDA REST API を呼び出す。一時障害（5xx、接続エラー）は最大3回リトライする。"""
    response = requests.post(url, json=body, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def _to_oanda_instrument(ticker: str, asset_class: str) -> str:
    """TradingViewのティッカーをOANDA instrument形式に変換する。

    asset_class に応じた変換ルール:
    - "FX" / "COMMODITY": 6文字の場合 "USDJPY" → "USD_JPY"
    - "US" / "INDEX": アンダースコアなしの場合 "AAPL" → "AAPL_USD"
    - その他: そのまま使用（OANDA形式で直接指定）
    """
    if asset_class in ("FX", "COMMODITY") and len(ticker) == 6 and "_" not in ticker:
        return f"{ticker[:3]}_{ticker[3:]}"
    if asset_class in ("US", "INDEX") and "_" not in ticker:
        return f"{ticker}_USD"
    if asset_class in ("FX", "COMMODITY") and len(ticker) != 6:
        logger.warning(
            "FX/COMMODITYの ticker が6文字ではありません。変換せずに送信します: %s",
            ticker,
        )
    return ticker


class OandaHandler:
    """OANDA証券への注文を実行するハンドラー。"""

    def execute(self, payload: WebhookPayload) -> dict:
        """OANDA証券に成行注文を送信する。

        Returns:
            {"order_id": str, "instrument": str, ...}

        Raises:
            ValueError: 環境変数が不足または不正な場合
            requests.RequestException: API呼び出しに失敗した場合
        """
        api_key = os.getenv("OANDA_API_KEY", "")
        account_id = os.getenv("OANDA_ACCOUNT_ID", "")

        if not api_key:
            raise ValueError("環境変数 OANDA_API_KEY が設定されていません")
        if not account_id:
            raise ValueError("環境変数 OANDA_ACCOUNT_ID が設定されていません")
        oanda_env = resolve_oanda_env()

        base_url = OANDA_PRACTICE_URL if oanda_env == "PRACTICE" else OANDA_LIVE_URL
        instrument = _to_oanda_instrument(payload.ticker, payload.asset_class)

        # SELL は負の units で表現する
        units = payload.quantity if payload.action == "buy" else -payload.quantity

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
            }
        }

        url = f"{base_url}/v3/accounts/{account_id}/orders"
        logger.info(
            "OANDA注文送信: instrument=%s units=%s env=%s", instrument, units, oanda_env
        )

        data = _call_oanda_api(url, body, headers)
        order_id = data.get("orderCreateTransaction", {}).get("id", "unknown")
        fill_tx = data.get("orderFillTransaction", {})
        filled_qty = abs(float(fill_tx["units"])) if fill_tx.get("units") is not None else None
        filled_price = (
            float(fill_tx["price"]) if fill_tx.get("price") is not None else None
        )
        fill_id = str(fill_tx["id"]) if fill_tx.get("id") is not None else None

        logger.info("OANDA注文成功: order_id=%s instrument=%s", order_id, instrument)
        return {
            "order_id": order_id,
            "instrument": instrument,
            "fill_id": fill_id,
            "filled_qty": filled_qty,
            "filled_price": filled_price,
        }
