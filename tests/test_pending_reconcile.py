"""GTC 注文の遅延再照合（#79）のユニットテスト。

WHY: #57 の reconcile_order は発注 5 秒後の単発照合のため、クローズ後着の
GTC 注文（#76/#77）は SUBMITTED・dealt_qty=0 のままイベントが確定し、翌営業日の
実約定がログに反映されない（→ forge live replay の equity が永遠フラット）。
本スイープが「未終端注文の検出 → 状態変化時のみ order_reconciled 追記」を
正しく行うことを固定する。毎サイクル無差別に追記するとイベントログが
スパム化するため、差分検出での抑制も仕様として encode する。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from alpha_strike.services.pending_reconcile import (
    find_pending_reconciles,
    is_pending_reconcile_enabled,
    pending_reconcile_loop,
    run_pending_reconcile_once,
)
from alpha_strike.services.status_service import (
    AccountStatus,
    AccountSummary,
    OrderRecord,
)


def _reconciled_event(
    order_id: str = "ord_1",
    broker_order_id: str | None = "370528",
    order_status: str = "SUBMITTED",
    dealt_qty: float = 0.0,
    occurred_at: datetime | None = None,
    ticker: str = "US.GLD",
) -> dict:
    return {
        "event_type": "order_reconciled",
        "event_id": f"evt_{order_id}",
        "signal_id": "sig_1",
        "order_id": order_id,
        "occurred_at": (occurred_at or datetime.now()).isoformat(),
        "broker": "moomoo",
        "asset_class": "US",
        "action": "buy",
        "ticker": ticker,
        "quantity": 2.0,
        "order_status": order_status,
        "dealt_qty": dealt_qty,
        "dealt_avg_price": 0.0,
        "is_filled": False,
        "broker_order_id": broker_order_id,
        "strategy_id": "beat_v1",
        "strategy_version": None,
        "snapshot_id": None,
        "run_mode": "paper",
        "portfolio_id": "beat_v1",
        "sub_strategy_id": "gld_v1",
    }


def _logger_with(events: list[dict]) -> MagicMock:
    logger = MagicMock()
    logger.load_events.return_value = events
    return logger


def _provider_with(*orders: OrderRecord) -> MagicMock:
    status = AccountStatus(
        broker="moomoo",
        trd_env="SIMULATE",
        account=AccountSummary(),
        positions=[],
        recent_orders=list(orders),
    )
    provider = MagicMock()
    provider.get_status.return_value = status
    return provider


class TestFindPendingReconciles:
    def test_submitted_is_pending(self):
        logger = _logger_with([_reconciled_event(order_status="SUBMITTED")])
        assert len(find_pending_reconciles(logger)) == 1

    def test_filled_part_is_pending(self):
        """部分約定は残量が約定し得る → 未終端として再照合を続ける。"""
        logger = _logger_with(
            [_reconciled_event(order_status="FILLED_PART", dealt_qty=1.0)]
        )
        assert len(find_pending_reconciles(logger)) == 1

    @pytest.mark.parametrize(
        "status", ["FILLED_ALL", "CANCELLED_ALL", "FAILED", "DELETED"]
    )
    def test_terminal_statuses_are_not_pending(self, status):
        logger = _logger_with([_reconciled_event(order_status=status)])
        assert find_pending_reconciles(logger) == []

    def test_latest_event_per_order_id_wins(self):
        """同一 order_id は最新イベントの状態で判定（load_events は新しい順）。"""
        logger = _logger_with(
            [
                _reconciled_event(order_status="FILLED_ALL", dealt_qty=2.0),
                _reconciled_event(order_status="SUBMITTED"),
            ]
        )
        assert find_pending_reconciles(logger) == []

    def test_old_events_beyond_lookback_are_excluded(self):
        """lookback 超の注文は OpenD の order 窓からも消えるため再照合しない。"""
        old = datetime.now() - timedelta(days=10)
        logger = _logger_with([_reconciled_event(occurred_at=old)])
        assert find_pending_reconciles(logger, lookback_days=7) == []

    def test_missing_broker_order_id_is_excluded(self):
        """broker_order_id なしでは OpenD 照合キーがない → 対象外。"""
        logger = _logger_with([_reconciled_event(broker_order_id=None)])
        assert find_pending_reconciles(logger) == []


class TestRunPendingReconcileOnce:
    def test_fill_detected_appends_updated_event(self):
        """SUBMITTED → broker で FILLED_ALL を検出 → 権威イベントを追記。"""
        logger = _logger_with([_reconciled_event(order_status="SUBMITTED")])
        provider = _provider_with(
            OrderRecord(
                code="US.GLD",
                order_id="370528",
                order_status="FILLED_ALL",
                qty=2.0,
                dealt_qty=2.0,
                dealt_avg_price=310.5,
            )
        )
        updated = run_pending_reconcile_once(
            provider=provider, event_logger=logger
        )
        assert updated == 1
        appended = logger.append.call_args[0][0]
        assert appended.event_type == "order_reconciled"
        assert appended.order_id == "ord_1"
        assert appended.broker_order_id == "370528"
        assert appended.order_status == "FILLED_ALL"
        assert appended.dealt_qty == 2.0
        assert appended.dealt_avg_price == 310.5
        assert appended.is_filled is True
        # 下流の position 再構築に必要な紐付けメタデータが引き継がれる
        assert appended.signal_id == "sig_1"
        assert appended.portfolio_id == "beat_v1"
        assert appended.sub_strategy_id == "gld_v1"
        assert appended.run_mode == "paper"

    def test_unchanged_order_appends_nothing(self):
        """状態不変（SUBMITTED のまま）なら追記しない（ログのスパム防止）。"""
        logger = _logger_with([_reconciled_event(order_status="SUBMITTED")])
        provider = _provider_with(
            OrderRecord(
                code="US.GLD",
                order_id="370528",
                order_status="SUBMITTED",
                qty=2.0,
                dealt_qty=0.0,
            )
        )
        updated = run_pending_reconcile_once(
            provider=provider, event_logger=logger
        )
        assert updated == 0
        logger.append.assert_not_called()

    def test_partial_fill_progress_is_recorded(self):
        """FILLED_PART でも dealt_qty が進んでいれば追記する。"""
        logger = _logger_with(
            [_reconciled_event(order_status="FILLED_PART", dealt_qty=1.0)]
        )
        provider = _provider_with(
            OrderRecord(
                code="US.GLD",
                order_id="370528",
                order_status="FILLED_PART",
                qty=2.0,
                dealt_qty=1.5,
                dealt_avg_price=310.0,
            )
        )
        updated = run_pending_reconcile_once(
            provider=provider, event_logger=logger
        )
        assert updated == 1
        appended = logger.append.call_args[0][0]
        assert appended.dealt_qty == 1.5
        # 部分約定は is_filled=True（FILLED 系かつ dealt_qty>0、#57 の定義踏襲）
        assert appended.is_filled is True

    def test_order_not_in_broker_window_is_skipped(self):
        """OpenD の order 窓に見えない注文は変化情報なし → 追記せず次回へ。"""
        logger = _logger_with([_reconciled_event(order_status="SUBMITTED")])
        provider = _provider_with()  # recent_orders 空
        updated = run_pending_reconcile_once(
            provider=provider, event_logger=logger
        )
        assert updated == 0
        logger.append.assert_not_called()

    def test_no_pending_skips_broker_query(self):
        """pending ゼロなら OpenD へ問い合わせない（無駄な接続を避ける）。"""
        logger = _logger_with([])
        provider = MagicMock()
        updated = run_pending_reconcile_once(
            provider=provider, event_logger=logger
        )
        assert updated == 0
        provider.get_status.assert_not_called()

    def test_notifier_notified_on_fill(self):
        """約定検出時は ntfy 通知（有効時のみ）。"""
        logger = _logger_with([_reconciled_event(order_status="SUBMITTED")])
        provider = _provider_with(
            OrderRecord(
                code="US.GLD",
                order_id="370528",
                order_status="FILLED_ALL",
                qty=2.0,
                dealt_qty=2.0,
                dealt_avg_price=310.5,
            )
        )
        notifier = MagicMock()
        notifier.enabled = True
        run_pending_reconcile_once(
            provider=provider, event_logger=logger, notifier=notifier
        )
        assert notifier.notify.called

    def test_provider_failure_does_not_raise(self):
        """OpenD 障害でもスイープは落ちない（次サイクルで再試行）。"""
        logger = _logger_with([_reconciled_event(order_status="SUBMITTED")])
        provider = MagicMock()
        provider.get_status.side_effect = RuntimeError("OpenD down")
        updated = run_pending_reconcile_once(
            provider=provider, event_logger=logger
        )
        assert updated == 0


class TestIsPendingReconcileEnabled:
    def test_default_is_enabled(self, monkeypatch):
        monkeypatch.delenv("PENDING_RECONCILE_ENABLED", raising=False)
        assert is_pending_reconcile_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "off"])
    def test_falsy_values_disable(self, monkeypatch, value):
        monkeypatch.setenv("PENDING_RECONCILE_ENABLED", value)
        assert is_pending_reconcile_enabled() is False


class TestPendingReconcileLoop:
    @pytest.mark.anyio
    async def test_loop_runs_immediately_and_repeats_until_cancelled(
        self, monkeypatch
    ):
        """起動直後に 1 回目を実行（サーバー停止中の約定を即回収）し、
        interval ごとに繰り返し、cancel で素直に終了する。"""
        calls: list[int] = []

        def _fake_once(**kwargs):
            calls.append(1)
            return 0

        monkeypatch.setattr(
            "alpha_strike.services.pending_reconcile.run_pending_reconcile_once",
            _fake_once,
        )
        task = asyncio.create_task(
            pending_reconcile_loop(
                provider=MagicMock(),
                event_logger=MagicMock(),
                interval_seconds=0.01,
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) >= 2  # 即時 1 回 + interval 反復

    @pytest.fixture
    def anyio_backend(self):
        return "asyncio"
