"""moomoo証券（Futu OpenAPI）アダプター

ローカルで稼働するOpenDゲートウェイ経由で注文を実行します。

テスト時は MOOMOO_TRD_ENV=SIMULATE（デモ環境）を使用してください。
OpenD を先に起動してからサーバーを起動してください。
"""

import logging
import os
import socket
from typing import TYPE_CHECKING, Union

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from models import WebhookPayload

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import futu

try:
    import futu

    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    logger.warning(
        "futu-api がインポートできません。moomoo注文は実行時に失敗します。OpenDが起動しているか確認してください。"
    )


@retry(
    retry=retry_if_exception_type((OSError, socket.timeout)),
    wait=wait_fixed(2),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _check_opend_connection(host: str, port: int) -> None:
    """OpenD への TCP 接続確認。一時障害に対して最大3回リトライする。"""
    with socket.create_connection((host, port), timeout=3):
        pass


def _get_trade_context(
    asset_class: str,
    host: str,
    port: int,
) -> "Union[futu.OpenUSTradeContext, futu.OpenHKTradeContext]":
    """asset_class に基づいてトレードコンテキストを返す。

    対応: "HK" → OpenHKTradeContext, その他（"US"等）→ OpenUSTradeContext
    """
    if asset_class.upper() == "HK":
        return futu.OpenHKTradeContext(host=host, port=port)
    return futu.OpenUSTradeContext(host=host, port=port)


def moomoo_order_handler(payload: WebhookPayload) -> dict:
    """moomoo証券（Futu OpenAPI）へ注文を送信する。

    Args:
        payload: Webhookペイロード（broker="moomoo" 前提）

    Returns:
        注文結果を含む dict

    Raises:
        ImportError: futu-api が利用不可の場合
        ValueError: 必須設定が不足している場合
        RuntimeError: 注文APIがエラーを返した場合
    """
    if not FUTU_AVAILABLE:
        raise ImportError(
            "futu-api が利用できません。`uv add futu-api` でインストール後、OpenDを起動してください。"
        )

    host = os.getenv("MOOMOO_HOST", "127.0.0.1")
    port = int(os.getenv("MOOMOO_PORT", "11111"))
    trd_env_str = os.getenv("MOOMOO_TRD_ENV", "SIMULATE").upper()

    # TrdEnv マッピング（デフォルトはSIMULATEで安全側）
    trd_env_map = {
        "SIMULATE": futu.TrdEnv.SIMULATE,
        "REAL": futu.TrdEnv.REAL,
    }
    if trd_env_str not in trd_env_map:
        raise ValueError(
            f"MOOMOO_TRD_ENV は SIMULATE または REAL を指定してください（現在: {trd_env_str}）"
        )

    trd_env = trd_env_map[trd_env_str]

    trd_side = futu.TrdSide.BUY if payload.action == "buy" else futu.TrdSide.SELL

    logger.info(
        "moomoo注文開始: trd_env=%s ticker=%s action=%s qty=%s",
        trd_env_str,
        payload.ticker,
        payload.action,
        payload.quantity,
    )

    # OpenD への接続可否を事前確認（タイムアウト3秒、最大3回リトライ）
    try:
        _check_opend_connection(host, port)
    except (OSError, socket.timeout) as e:
        logger.error("OpenD に接続できません (%s:%s): %s", host, port, e)
        raise RuntimeError(
            f"OpenD ({host}:{port}) が起動していません。先にOpenDを起動してください。"
        ) from e

    try:
        ctx = _get_trade_context(payload.asset_class, host, port)
        with ctx:
            ret_code, data = ctx.place_order(
                price=0,
                qty=payload.quantity,
                code=payload.ticker,
                trd_side=trd_side,
                order_type=futu.OrderType.MARKET,
                trd_env=trd_env,
            )

            if ret_code != futu.RET_OK:
                logger.error(
                    "moomoo注文失敗: ticker=%s ret_code=%s data=%s",
                    payload.ticker,
                    ret_code,
                    data,
                )
                raise RuntimeError(f"moomoo注文エラー: {data}")

            try:
                if (
                    hasattr(data, "empty")
                    and not data.empty
                    and "order_id" in data.columns
                ):
                    order_id = str(data["order_id"].iloc[0])
                else:
                    order_id = str(data)
                    logger.warning(
                        "order_idの取得に失敗。レスポンス全体を使用: %s", data
                    )
            except (AttributeError, KeyError, IndexError) as e:
                logger.warning("order_idのパース失敗: %s。レスポンス全体を使用。", e)
                order_id = str(data)

            logger.info("moomoo注文成功: order_id=%s", order_id)
            return {"order_id": order_id, "ret_code": ret_code}

    except (AttributeError, KeyError) as e:
        logger.error("moomooレスポンスのパース失敗: %s", e)
        raise RuntimeError(f"moomooレスポンス解析エラー: {e}") from e
    except Exception as e:
        if isinstance(e, (ImportError, ValueError, RuntimeError)):
            raise
        logger.error(
            "moomoo注文で予期しないエラー: ticker=%s error=%s",
            payload.ticker,
            e,
            exc_info=True,
        )
        raise RuntimeError(f"moomoo注文失敗: {e}") from e
