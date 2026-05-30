"""webhook → バックグラウンド reconcile → ntfy 通知の配線テスト (issue #57 Phase 2)。

moomoo の発注成功後に reconcile_and_notify がスケジュールされ、最終 order status が
通知されることを network-free に検証する（order_router / provider / notifier をモック）。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from alpha_strike.event_logger import JsonlEventLogger
from alpha_strike.services.fill_service import FillEventService
from alpha_strike.services.idempotency import IdempotencyStore
from alpha_strike.services.status_service import (
    AccountStatus,
    AccountSummary,
    OrderRecord,
)
from alpha_strike.webhook_server import app


class _FakeRouter:
    def route(self, payload):
        return {"order_id": "366675"}


class _FakeProvider:
    def get_status(self, *, trd_env=None):
        return AccountStatus(
            broker="moomoo",
            trd_env="SIMULATE",
            account=AccountSummary(),
            positions=[],
            recent_orders=[
                OrderRecord(
                    code="US.GLD",
                    trd_side="SELL",
                    order_status="CANCELLED_ALL",
                    dealt_qty=0.0,
                    order_id="366675",
                )
            ],
        )


class _FakeNotifier:
    def __init__(self):
        self.calls = []
        self.enabled = True

    def notify(self, title, message, *, tags=(), priority=None, opener=None):
        self.calls.append({"title": title, "message": message, "tags": list(tags)})
        return True


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeLogger:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)

    def reconciled(self):
        return [e for e in self.events if getattr(e, "event_type", "") == "order_reconciled"]


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("WEBHOOK_PASSPHRASE", "test-secret")
    fake_logger = _FakeLogger()
    # webhook が使う module global event_logger を fake に差し替え（永続化を捕捉）
    monkeypatch.setattr("alpha_strike.webhook_server.event_logger", fake_logger)
    app.state.order_router = _FakeRouter()
    app.state.fill_service = FillEventService(JsonlEventLogger())
    app.state.idempotency = IdempotencyStore(ttl_seconds=600)
    app.state.status_provider = _FakeProvider()
    app.state.notifier = _FakeNotifier()
    app.state.reconcile_delay = 0  # テストは即時照合
    app.state._fake_logger = fake_logger
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    # テスト間 state リーク防止
    for attr in ("notifier", "status_provider", "reconcile_delay", "_fake_logger"):
        try:
            delattr(app.state, attr)
        except (AttributeError, KeyError):
            pass


@pytest.mark.anyio
async def test_moomoo_order_persists_reconciled_event_and_notifies(client):
    payload = {
        "passphrase": "test-secret",
        "broker": "moomoo",
        "asset_class": "US",
        "action": "sell",
        "ticker": "US.GLD",
        "quantity": 1,
        "signal_id": "sig_reconcile_001",
    }
    resp = await client.post("/webhook", json=payload)
    assert resp.status_code == 200
    # BackgroundTask は ASGITransport のレスポンス完了までに実行される
    # 権威イベント OrderReconciledEvent が永続化される（実 fill = CANCELLED_ALL）
    reconciled = app.state._fake_logger.reconciled()
    assert len(reconciled) == 1
    assert reconciled[0].order_status == "CANCELLED_ALL"
    assert reconciled[0].is_filled is False
    assert reconciled[0].broker_order_id == "366675"
    # notifier 有効なので通知も発火
    notifier = app.state.notifier
    assert len(notifier.calls) == 1
    assert "CANCELLED_ALL" in notifier.calls[0]["message"]


@pytest.mark.anyio
async def test_notifier_disabled_still_persists_reconciled_event(client):
    """ntfy 無効でも reconcile イベントは永続化される（データ正確性は通知に依存しない）。"""
    app.state.notifier.enabled = False
    payload = {
        "passphrase": "test-secret",
        "broker": "moomoo",
        "asset_class": "US",
        "action": "sell",
        "ticker": "US.GLD",
        "quantity": 1,
        "signal_id": "sig_reconcile_002",
    }
    resp = await client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert app.state.notifier.calls == []  # 通知なし
    reconciled = app.state._fake_logger.reconciled()  # 永続化はされる
    assert len(reconciled) == 1
    assert reconciled[0].order_status == "CANCELLED_ALL"
