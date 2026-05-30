"""発注後の order status reconcile → イベント永続化 + 通知 (issue #57 Phase 2)。

webhook は broker への submission 受理（accepted）時点で 200 を返すが、実際の約定
（fill）/ キャンセルはその後に確定する。本モジュールは発注後にバックグラウンドで
broker（OpenD）の最終 order status を照合し、

1. 権威イベント ``OrderReconciledEvent`` を **常に** JSONL に永続化する（下流の
   forge live 等が submission≠fill の盲点無しに live を集計できるようにする）。
2. notifier が有効なら ntfy に通知する（副次機能）。

「注文成功ログなのに実は CANCELLED_ALL」のような乖離を source（alpha-strike）で確定する。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from alpha_strike.models import OrderReconciledEvent
from alpha_strike.services.fill_service import _generate_id

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


async def reconcile_order(
    *,
    provider: Any,
    event_logger: Any,
    notifier: Any = None,
    broker_order_id: str,
    signal_id: str,
    order_id: str,
    broker: str,
    asset_class: str,
    ticker: str,
    action: str,
    quantity: float,
    strategy_id: str | None = None,
    strategy_version: str | None = None,
    snapshot_id: str | None = None,
    run_mode: str = "live",
    portfolio_id: str | None = None,
    sub_strategy_id: str | None = None,
    delay_seconds: float = 5.0,
    trd_env: str | None = None,
) -> None:
    """発注後 ``delay_seconds`` 待ってから OpenD の最終 order status を照合する。

    照合結果を ``OrderReconciledEvent`` として **常に** 永続化し（データ正確性は通知の
    有効/無効に依存しない）、``notifier`` が有効なら ntfy 通知する。

    バックグラウンドタスクとして呼ばれる前提。例外は握りつぶしてログに残す
    （reconcile/通知の失敗でサーバーを落とさない）。
    """
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
        if order is None:
            order_status = "NOT_FOUND"
            dealt_qty = 0.0
            dealt_avg_price = 0.0
        else:
            order_status = order.order_status or ""
            dealt_qty = float(order.dealt_qty or 0.0)
            dealt_avg_price = float(order.dealt_avg_price or 0.0)
        is_filled = order_status.upper() in _FILLED_STATUSES and dealt_qty > 0

        # 1. 権威イベントを常に永続化
        try:
            event_logger.append(
                OrderReconciledEvent(
                    event_id=_generate_id("evt"),
                    signal_id=signal_id,
                    order_id=order_id,
                    occurred_at=datetime.now(),
                    broker=broker,  # type: ignore[arg-type]
                    asset_class=asset_class,  # type: ignore[arg-type]
                    action=action,  # type: ignore[arg-type]
                    ticker=ticker,
                    quantity=quantity,
                    order_status=order_status,
                    dealt_qty=dealt_qty,
                    dealt_avg_price=dealt_avg_price,
                    is_filled=is_filled,
                    broker_order_id=broker_order_id,
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                    snapshot_id=snapshot_id,
                    run_mode=run_mode,  # type: ignore[arg-type]
                    portfolio_id=portfolio_id,
                    sub_strategy_id=sub_strategy_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 永続化失敗もサーバーを落とさない
            logger.warning("OrderReconciledEvent の永続化に失敗しました: %s", exc)

        # 2. notifier が有効なら通知
        if getattr(notifier, "enabled", False):
            side = (action or "").upper()
            if order is None:
                notifier.notify(
                    f"⏳ 注文照合不可: {ticker}",
                    f"{ticker} {side} qty={quantity} (order_id={broker_order_id}) の"
                    " 最終ステータスを照合できませんでした（履歴に見つからず）。",
                    tags=["question"],
                )
            else:
                tag, priority = _classify(order_status)
                emoji = {"white_check_mark": "✅", "warning": "⚠️", "hourglass": "⏳"}.get(
                    tag, "ℹ️"
                )
                notifier.notify(
                    f"{emoji} 注文 {order_status}: {ticker}",
                    f"{ticker} {side} qty={quantity}\n"
                    f"status={order_status} dealt_qty={dealt_qty} avg={dealt_avg_price}\n"
                    f"order_id={broker_order_id}",
                    tags=[tag],
                    priority=priority,
                )
    except Exception as exc:  # noqa: BLE001 - reconcile/通知失敗はサーバーを落とさない
        logger.warning("order reconcile に失敗しました: %s", exc)
