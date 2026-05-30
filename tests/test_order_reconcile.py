"""order reconcile のテスト (issue #57 Phase 2)。

発注後に broker の最終 order status を照合し、ntfy 通知文を組み立てることを検証する。
provider / notifier はモックで network-free。
"""

from __future__ import annotations

import pytest

from alpha_strike.services.order_reconcile import reconcile_and_notify
from alpha_strike.services.status_service import (
    AccountStatus,
    AccountSummary,
    OrderRecord,
)


class _FakeNotifier:
    def __init__(self):
        self.calls: list[dict] = []
        self.enabled = True

    def notify(self, title, message, *, tags=(), priority=None, opener=None):
        self.calls.append({"title": title, "message": message, "tags": list(tags)})
        return True


def _provider_with_order(order: OrderRecord):
    status = AccountStatus(
        broker="moomoo",
        trd_env="SIMULATE",
        account=AccountSummary(),
        positions=[],
        recent_orders=[order],
    )

    class _P:
        def get_status(self, *, trd_env=None):
            return status

    return _P()


@pytest.mark.anyio
async def test_reconcile_filled_notifies_success():
    notifier = _FakeNotifier()
    provider = _provider_with_order(
        OrderRecord(
            code="US.TQQQ",
            trd_side="BUY",
            order_status="FILLED_ALL",
            dealt_qty=1.0,
            order_id="366348",
        )
    )
    await reconcile_and_notify(
        provider=provider,
        notifier=notifier,
        broker_order_id="366348",
        ticker="US.TQQQ",
        action="buy",
        quantity=1.0,
        delay_seconds=0,
    )
    assert len(notifier.calls) == 1
    msg = notifier.calls[0]["message"]
    assert "FILLED_ALL" in msg and "US.TQQQ" in msg


@pytest.mark.anyio
async def test_reconcile_cancelled_notifies_warning():
    """CANCELLED_ALL（submission 成功でも未約定）を warning として通知する。"""
    notifier = _FakeNotifier()
    provider = _provider_with_order(
        OrderRecord(
            code="US.GLD",
            trd_side="SELL",
            order_status="CANCELLED_ALL",
            dealt_qty=0.0,
            order_id="366675",
        )
    )
    await reconcile_and_notify(
        provider=provider,
        notifier=notifier,
        broker_order_id="366675",
        ticker="US.GLD",
        action="sell",
        quantity=1.0,
        delay_seconds=0,
    )
    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert "CANCELLED_ALL" in call["message"]
    assert "warning" in call["tags"]


@pytest.mark.anyio
async def test_reconcile_order_not_found_still_notifies():
    """照合で order が見つからなくても、その旨を通知する（沈黙させない）。"""
    notifier = _FakeNotifier()
    provider = _provider_with_order(
        OrderRecord(code="US.OTHER", order_id="999", order_status="FILLED_ALL")
    )
    await reconcile_and_notify(
        provider=provider,
        notifier=notifier,
        broker_order_id="366675",
        ticker="US.GLD",
        action="sell",
        quantity=1.0,
        delay_seconds=0,
    )
    assert len(notifier.calls) == 1
    assert "US.GLD" in notifier.calls[0]["message"]


@pytest.mark.anyio
async def test_reconcile_disabled_notifier_no_call():
    notifier = _FakeNotifier()
    notifier.enabled = False
    provider = _provider_with_order(
        OrderRecord(code="US.GLD", order_id="1", order_status="FILLED_ALL")
    )
    await reconcile_and_notify(
        provider=provider,
        notifier=notifier,
        broker_order_id="1",
        ticker="US.GLD",
        action="sell",
        quantity=1.0,
        delay_seconds=0,
    )
    assert notifier.calls == []


@pytest.fixture
def anyio_backend():
    return "asyncio"
