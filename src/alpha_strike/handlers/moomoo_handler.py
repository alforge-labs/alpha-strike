"""moomoo証券（Futu OpenAPI）アダプター

ローカルで稼働するOpenDゲートウェイ経由で注文を実行します。

テスト時は MOOMOO_TRD_ENV=SIMULATE（デモ環境）を使用してください。
OpenD を先に起動してからサーバーを起動してください。
"""

import logging
import os
import socket
from typing import TYPE_CHECKING

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from alpha_strike.models import WebhookPayload

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
        pass  # 接続確認のみ。コンテキスト終了時に自動クローズ


_MARKET_MAP: "dict[str, str]" = {
    "HK": "HK",
    "CRYPTO": "CRYPTO",
}


def _get_trade_context(
    asset_class: str,
    host: str,
    port: int,
) -> "futu.OpenSecTradeContext":
    """asset_class に基づいて統一トレードコンテキスト OpenSecTradeContext を返す。

    futu/moomoo SDK 10.5.6508 以降は OpenUSTradeContext / OpenHKTradeContext が
    廃止され、OpenSecTradeContext + filter_trdmarket に統一された。

    対応:
      - "HK"     → filter_trdmarket=TrdMarket.HK
      - "CRYPTO" → filter_trdmarket=TrdMarket.CRYPTO
      - その他   → filter_trdmarket=TrdMarket.US (US / INDEX / COMMODITY / FX）

    security_firm はすべての市場で SecurityFirm.NONE をデフォルト指定する。
    REAL 取引で broker 固有の firm が必要な場合は呼び出し側で上書きする。
    """
    ac = asset_class.upper()
    market_name = _MARKET_MAP.get(ac, "US")
    market = getattr(futu.TrdMarket, market_name)
    return futu.OpenSecTradeContext(
        filter_trdmarket=market,
        host=host,
        port=port,
        security_firm=futu.SecurityFirm.NONE,
    )


class MoomooHandler:
    """moomoo証券（Futu OpenAPI）への注文を実行するハンドラー。"""

    def execute(self, payload: WebhookPayload) -> dict:
        """moomoo証券（Futu OpenAPI）へ注文を送信する。

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
        try:
            port = int(os.getenv("MOOMOO_PORT", "11111"))
        except ValueError as e:
            raise ValueError("MOOMOO_PORT に不正な値が設定されています") from e
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

        # moomoo crypto API は live only。SIMULATE では SDK が
        # "the type of environment param is wrong" を返すため、
        # OpenD 接続前にアクション可能なメッセージで早期拒否する。
        if payload.asset_class.upper() == "CRYPTO" and trd_env_str == "SIMULATE":
            raise ValueError(
                "moomoo crypto は SIMULATE 環境を受け付けません（live only）。"
                "paper 運用したい場合は BTC ETF (US.IBIT / US.FBTC / US.BITO 等) を "
                "asset_class=US で発注するか、MOOMOO_TRD_ENV=REAL で実 money 運用してください。"
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

                filled_qty = None
                filled_price = None
                if hasattr(data, "empty") and not data.empty:
                    if "dealt_qty" in data.columns:
                        filled_qty = float(data["dealt_qty"].iloc[0])
                    if "dealt_avg_price" in data.columns:
                        filled_price = float(data["dealt_avg_price"].iloc[0])

                logger.info("moomoo注文成功: order_id=%s", order_id)
                return {
                    "order_id": order_id,
                    "ret_code": ret_code,
                    "filled_qty": filled_qty,
                    "filled_price": filled_price,
                }

        except Exception as e:
            if isinstance(e, (ImportError, ValueError, RuntimeError)):
                raise
            logger.error(
                "moomoo注文で予期しないエラー: ticker=%s error=%s",
                payload.ticker,
                e,
            )
            raise RuntimeError(f"moomoo注文失敗: {e}") from e
