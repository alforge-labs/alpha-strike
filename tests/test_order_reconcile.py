"""order reconcile → イベント永続化 + 通知のテスト (issue #57 Phase 2 / source reconcile)。

発注後に broker(OpenD) の最終 order status を照合し、
- 権威データとして OrderReconciledEvent を **常に** 永続化（ntfy 無効でも）
- notifier 有効時のみ ntfy 通知
することを network-free に検証する。
"""

from __future__ import annotations

import pytest

from alpha_strike.services.order_reconcile import reconcile_order
from alpha_strike.services.status_service import (
    AccountStatus,
    AccountSummary,
    OrderRecord,
)


class _FakeNotifier:
    def __init__(self, enabled=True):
        self.calls: list[dict] = []
        self.enabled = enabled

    def notify(self, title, message, *, tags=(), priority=None, opener=None):
        self.calls.append({"title": title, "message": message, "tags": list(tags)})
        return True


class _FakeLogger:
    def __init__(self):
        self.events: list = []

    def append(self, event):
        self.events.append(event)


def _provider_with_order(order: OrderRecord | None):
    orders = [order] if order is not None else []
    status = AccountStatus(
        broker="moomoo",
        trd_env="SIMULATE",
        account=AccountSummary(),
        positions=[],
        recent_orders=orders,
    )

    class _P:
        def get_status(self, *, trd_env=None):
            return status

    return _P()


def _common_kwargs():
    return dict(
        broker_order_id="366675",
        signal_id="sig_001",
        order_id="ord_001",
        broker="moomoo",
        asset_class="US",
        ticker="US.GLD",
        action="sell",
        quantity=1.0,
        delay_seconds=0,
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_filled_persists_is_filled_event_and_notifies():
    logger = _FakeLogger()
    notifier = _FakeNotifier(enabled=True)
    provider = _provider_with_order(
        OrderRecord(
            code="US.TQQQ", trd_side="BUY", order_status="FILLED_ALL",
            dealt_qty=1.0, dealt_avg_price=84.47, order_id="366348",
        )
    )
    kw = _common_kwargs()
    kw.update(broker_order_id="366348", ticker="US.TQQQ", action="buy")
    await reconcile_order(provider=provider, event_logger=logger, notifier=notifier, **kw)

    assert len(logger.events) == 1
    ev = logger.events[0]
    assert ev.event_type == "order_reconciled"
    assert ev.order_status == "FILLED_ALL"
    assert ev.dealt_qty == 1.0
    assert ev.is_filled is True
    assert ev.broker_order_id == "366348"
    assert len(notifier.calls) == 1


@pytest.mark.anyio
async def test_cancelled_persists_not_filled_event():
    """CANCELLED_ALL（submission 成功でも未約定）は is_filled=False で永続化される。"""
    logger = _FakeLogger()
    notifier = _FakeNotifier(enabled=True)
    provider = _provider_with_order(
        OrderRecord(
            code="US.GLD", trd_side="SELL", order_status="CANCELLED_ALL",
            dealt_qty=0.0, order_id="366675",
        )
    )
    await reconcile_order(provider=provider, event_logger=logger, notifier=notifier, **_common_kwargs())

    assert len(logger.events) == 1
    ev = logger.events[0]
    assert ev.order_status == "CANCELLED_ALL"
    assert ev.is_filled is False
    assert ev.dealt_qty == 0.0
    assert "warning" in notifier.calls[0]["tags"]


@pytest.mark.anyio
async def test_persists_even_when_notifier_disabled():
    """ntfy 無効でも権威イベントは永続化される（データ正確性は通知に依存しない）。"""
    logger = _FakeLogger()
    notifier = _FakeNotifier(enabled=False)
    provider = _provider_with_order(
        OrderRecord(code="US.GLD", order_status="CANCELLED_ALL", dealt_qty=0.0, order_id="366675")
    )
    await reconcile_order(provider=provider, event_logger=logger, notifier=notifier, **_common_kwargs())

    assert len(logger.events) == 1
    assert logger.events[0].order_status == "CANCELLED_ALL"
    assert notifier.calls == []


@pytest.mark.anyio
async def test_order_not_found_persists_not_found_status():
    """照合で order が見つからなくても、その事実を NOT_FOUND として永続化する。"""
    logger = _FakeLogger()
    notifier = _FakeNotifier(enabled=False)
    provider = _provider_with_order(
        OrderRecord(code="US.OTHER", order_id="999", order_status="FILLED_ALL")
    )
    await reconcile_order(provider=provider, event_logger=logger, notifier=notifier, **_common_kwargs())

    assert len(logger.events) == 1
    ev = logger.events[0]
    assert ev.order_status == "NOT_FOUND"
    assert ev.is_filled is False


@pytest.mark.anyio
async def test_no_notifier_still_persists():
    """notifier=None でも永続化される。"""
    logger = _FakeLogger()
    provider = _provider_with_order(
        OrderRecord(code="US.GLD", order_status="FILLED_ALL", dealt_qty=1.0, order_id="366675")
    )
    await reconcile_order(provider=provider, event_logger=logger, notifier=None, **_common_kwargs())
    assert len(logger.events) == 1
    assert logger.events[0].is_filled is True
