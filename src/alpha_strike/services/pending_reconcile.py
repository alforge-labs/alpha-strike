"""GTC 注文の翌営業日約定をイベントログへ反映する遅延再照合 (#79)。

背景: #57 の ``reconcile_order`` は発注 5 秒後の単発照合のため、クローズ後着の
GTC 注文（#76/#77）は照合時点で未約定（SUBMITTED・dealt_qty=0）のままイベントが
確定し、翌営業日の実約定がログに反映されない。結果、下流の forge live replay /
alpha-visualizer Live の equity curve が永遠に初期資金フラットになる。

本モジュールは起動時＋一定間隔で、未終端（pending）の ``order_reconciled`` を
走査し、OpenD の最新 order status と突き合わせて **状態変化があったときだけ**
``OrderReconciledEvent`` を追記する（毎サイクル無差別に追記するとイベントログが
スパム化するため）。OpenD への照会はサイクルあたり 1 回（``get_status`` の
order 窓は過去 ``history_days=7`` 日分を返す）。

下流（alpha-forge live replay / portfolio_alert_replay）は同一 order_id の
``order_reconciled`` のうち**最後のイベントを権威**として扱うため、追記で正しく
上書きされる。``fill_received`` は追記しない（``order_reconciled`` が存在する
order_id の ``fill_received`` は下流で無視されるため不要かつ二重計上リスク）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from alpha_strike.models import OrderReconciledEvent
from alpha_strike.services.fill_service import _generate_id
from alpha_strike.services.order_reconcile import (
    _classify,
    _FILLED_STATUSES,
    TERMINAL_STATUSES,
)

logger = logging.getLogger(__name__)

_ENABLED_ENV_VAR = "PENDING_RECONCILE_ENABLED"
_INTERVAL_ENV_VAR = "PENDING_RECONCILE_INTERVAL_SECONDS"
_TRUTHY = {"1", "true", "yes", "on"}

DEFAULT_INTERVAL_SECONDS = 600.0
# OpenD の order 窓（status_service の history_days=7）と揃える。
# これより古い注文は OpenD からも見えないため再照合を打ち切る。
DEFAULT_LOOKBACK_DAYS = 7
# 走査するイベント数の上限（日数 lookback と二重の安全弁）。
_SCAN_LIMIT = 500


def is_pending_reconcile_enabled() -> bool:
    """遅延再照合スイープの有効可否。

    既定 ON。GTC 化（#76/#77)以降、クローズ後着注文の約定捕捉に必須のため、
    明示的に無効化したい場合のみ ``PENDING_RECONCILE_ENABLED`` に偽値を設定する。
    """
    return os.getenv(_ENABLED_ENV_VAR, "1").strip().lower() in _TRUTHY


def get_pending_reconcile_interval() -> float:
    """スイープ間隔（秒）。既定 600 秒。不正値は既定にフォールバック。"""
    raw = os.getenv(_INTERVAL_ENV_VAR, str(DEFAULT_INTERVAL_SECONDS))
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s が数値ではありません、既定の %s 秒を使用",
            _INTERVAL_ENV_VAR,
            DEFAULT_INTERVAL_SECONDS,
        )
        return DEFAULT_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


def find_pending_reconciles(
    event_logger: Any,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[dict]:
    """未終端の ``order_reconciled`` イベント（order_id ごとに最新）を返す。

    - 同一 order_id は最新イベントの状態で判定（``load_events`` は新しい順）
    - 終端 status（FILLED_ALL / キャンセル・失敗系）は対象外
    - ``lookback_days`` 超は OpenD の order 窓からも消えるため対象外
    - ``broker_order_id`` なしは照合キーがないため対象外
    """
    events = event_logger.load_events(
        broker="moomoo", event_type="order_reconciled", limit=_SCAN_LIMIT
    )
    latest: dict[str, dict] = {}
    for ev in events:  # 新しい順 → 最初に見えたものが最新
        order_id = ev.get("order_id")
        if order_id and order_id not in latest:
            latest[order_id] = ev

    cutoff = datetime.now() - timedelta(days=lookback_days)
    pending: list[dict] = []
    for ev in latest.values():
        status = str(ev.get("order_status", "")).upper()
        if status in TERMINAL_STATUSES:
            continue
        if not ev.get("broker_order_id"):
            continue
        try:
            occurred_at = datetime.fromisoformat(str(ev.get("occurred_at")))
        except (TypeError, ValueError):
            continue
        if occurred_at < cutoff:
            continue
        pending.append(ev)
    return pending


def run_pending_reconcile_once(
    *,
    provider: Any,
    event_logger: Any,
    notifier: Any = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> int:
    """未終端注文を OpenD の最新 status と突き合わせ、変化分だけ追記する。

    Returns:
        追記した ``OrderReconciledEvent`` の件数。

    OpenD 障害等の例外は握り潰してログに残す（次サイクルで再試行）。
    """
    pending = find_pending_reconciles(event_logger, lookback_days=lookback_days)
    if not pending:
        return 0

    try:
        status = provider.get_status()
    except Exception as exc:  # noqa: BLE001 — スイープは落とさず次サイクルへ
        logger.warning("pending reconcile: broker status 取得失敗: %s", exc)
        return 0

    orders_by_id = {str(o.order_id): o for o in status.recent_orders}
    updated = 0
    for ev in pending:
        order = orders_by_id.get(str(ev["broker_order_id"]))
        if order is None:
            # order 窓にまだ/もう見えない → 変化情報なし、次サイクルへ
            continue
        new_status = (order.order_status or "").upper()
        new_dealt = float(order.dealt_qty or 0.0)
        prev_status = str(ev.get("order_status", "")).upper()
        prev_dealt = float(ev.get("dealt_qty") or 0.0)
        if new_status == prev_status and new_dealt == prev_dealt:
            continue

        is_filled = new_status in _FILLED_STATUSES and new_dealt > 0
        try:
            event_logger.append(
                OrderReconciledEvent(
                    event_id=_generate_id("evt"),
                    signal_id=str(ev.get("signal_id", "")),
                    order_id=str(ev["order_id"]),
                    occurred_at=datetime.now(),
                    broker=ev.get("broker", "moomoo"),
                    asset_class=ev.get("asset_class", "US"),
                    action=ev.get("action", "buy"),
                    ticker=str(ev.get("ticker", "")),
                    quantity=float(ev.get("quantity") or 0.0),
                    order_status=order.order_status or "",
                    dealt_qty=new_dealt,
                    dealt_avg_price=float(order.dealt_avg_price or 0.0),
                    is_filled=is_filled,
                    broker_order_id=str(ev["broker_order_id"]),
                    strategy_id=ev.get("strategy_id"),
                    strategy_version=ev.get("strategy_version"),
                    snapshot_id=ev.get("snapshot_id"),
                    run_mode=ev.get("run_mode", "live"),
                    portfolio_id=ev.get("portfolio_id"),
                    sub_strategy_id=ev.get("sub_strategy_id"),
                )
            )
        except Exception as exc:  # noqa: BLE001 — 1 件の失敗で全体を止めない
            logger.warning(
                "pending reconcile: イベント追記失敗 (order_id=%s): %s",
                ev.get("order_id"),
                exc,
            )
            continue
        updated += 1
        logger.info(
            "pending reconcile: %s %s → %s dealt_qty=%s (broker_order_id=%s)",
            ev.get("ticker"),
            prev_status,
            new_status,
            new_dealt,
            ev.get("broker_order_id"),
        )

        if getattr(notifier, "enabled", False):
            tag, priority = _classify(new_status)
            emoji = {"white_check_mark": "✅", "warning": "⚠️", "hourglass": "⏳"}.get(
                tag, "ℹ️"
            )
            notifier.notify(
                f"{emoji} 遅延照合 {new_status}: {ev.get('ticker')}",
                f"{ev.get('ticker')} {str(ev.get('action', '')).upper()} "
                f"qty={ev.get('quantity')}\n"
                f"status={prev_status} → {new_status} dealt_qty={new_dealt} "
                f"avg={float(order.dealt_avg_price or 0.0)}\n"
                f"order_id={ev.get('broker_order_id')}",
                tags=[tag],
                priority=priority,
            )
    return updated


async def pending_reconcile_loop(
    *,
    provider: Any,
    event_logger: Any,
    notifier: Any = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """遅延再照合の常駐ループ。lifespan の background task として起動する。

    起動直後に 1 回目を実行（サーバー停止中に発生した約定を即回収）し、以後
    ``interval_seconds`` ごとに繰り返す。例外はログに残して継続し、
    ``asyncio.CancelledError``（shutdown）でのみ終了する。
    """
    while True:
        try:
            # OpenD への blocking I/O をイベントループから退避
            updated = await asyncio.to_thread(
                run_pending_reconcile_once,
                provider=provider,
                event_logger=event_logger,
                notifier=notifier,
            )
            if updated:
                logger.info("pending reconcile: %d 件の order を更新", updated)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — ループは止めない
            logger.warning("pending reconcile loop でエラー: %s", exc)
        await asyncio.sleep(interval_seconds)
