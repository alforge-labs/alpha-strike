"""発注後の order status reconcile + 通知 (issue #57 Phase 2)。

webhook は broker への submission 受理（accepted）時点で 200 を返すが、実際の約定
（fill）/ キャンセルはその後に確定する。本モジュールは発注後にバックグラウンドで
broker（OpenD）の最終 order status を照合し、ntfy に通知する。これにより
「注文成功ログなのに実は CANCELLED_ALL」のような乖離をプッシュで即座に気づける。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 約定成功とみなす order_status。
_FILLED_STATUSES = {"FILLED_ALL", "FILLED_PART"}
# 異常終了（submission 成功でも未約定）とみなす order_status。
_FAILED_STATUSES = {
    "CANCELLED_ALL",
    "CANCELLED_PART",
    "CANCELED_ALL",
    "CANCELED_PART",
    "FAILED",
    "DELETED",
    "DISABLED",
    "SUBMIT_FAILED",
}


def _classify(status: str) -> tuple[str, str | None]:
    """order_status から (ntfy tag, priority) を決める。"""
    s = (status or "").upper()
    if s in _FILLED_STATUSES:
        return "white_check_mark", None
    if s in _FAILED_STATUSES:
        return "warning", "high"
    # pending 系（WAITING_SUBMIT / SUBMITTING / SUBMITTED 等）
    return "hourglass", None


async def reconcile_and_notify(
    *,
    provider: Any,
    notifier: Any,
    broker_order_id: str,
    ticker: str,
    action: str,
    quantity: float,
    delay_seconds: float = 5.0,
    trd_env: str | None = None,
) -> None:
    """発注後 ``delay_seconds`` 待ってから order status を照合し ntfy 通知する。

    バックグラウンドタスクとして呼ばれる前提。例外は握りつぶしてログに残す
    （通知は副次機能であり、サーバーを落とさない）。
    """
    if not getattr(notifier, "enabled", False):
        return
    try:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        # provider.get_status は OpenD への blocking I/O。イベントループを塞がない
        # ようスレッドへ退避する。
        status = await asyncio.to_thread(provider.get_status)

        order = next(
            (o for o in status.recent_orders if str(o.order_id) == str(broker_order_id)),
            None,
        )
        side = (action or "").upper()
        if order is None:
            title = f"⏳ 注文照合不可: {ticker}"
            message = (
                f"{ticker} {side} qty={quantity} (order_id={broker_order_id}) の"
                " 最終ステータスを照合できませんでした（履歴に見つからず）。"
            )
            notifier.notify(title, message, tags=["question"])
            return

        tag, priority = _classify(order.order_status)
        emoji = {
            "white_check_mark": "✅",
            "warning": "⚠️",
            "hourglass": "⏳",
        }.get(tag, "ℹ️")
        title = f"{emoji} 注文 {order.order_status}: {ticker}"
        message = (
            f"{ticker} {side} qty={quantity}\n"
            f"status={order.order_status} dealt_qty={order.dealt_qty}"
            f" avg={order.dealt_avg_price}\n"
            f"order_id={broker_order_id}"
        )
        notifier.notify(title, message, tags=[tag], priority=priority)
    except Exception as exc:  # noqa: BLE001 - 通知/照合失敗はサーバーを落とさない
        logger.warning("order reconcile に失敗しました: %s", exc)
