"""target_qty closed-loop 数量解決（#80）の webhook 統合テスト。

WHY: target_qty 付き payload は broker 実保有との差分で発注内容が再解決され、
target 到達済みなら broker へ送られないこと、target_qty なし（既存 Pine）や
kill-switch 無効時は従来の delta 解釈のままであること（後方互換）、
再解決後の SELL にも sell_guard が最終防衛線として効くこと——
broker へ渡る数量・記録イベント・HTTP 応答の 3 点を固定して退行を防ぐ。
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from alpha_strike.event_logger import JsonlEventLogger
from alpha_strike.services.fill_service import FillEventService
from alpha_strike.services.idempotency import IdempotencyStore
from alpha_strike.services.order_service import build_default_router
from alpha_strike.services.status_service import (
    AccountStatus,
    AccountSummary,
    PositionRecord,
)
from alpha_strike.webhook_server import app

_ACCEPTED_RESULT = {
    "order_id": "42",
    "ret_code": 0,
    "filled_qty": None,
    "filled_price": None,
}


def _provider_with(*positions: PositionRecord):
    status = AccountStatus(
        broker="moomoo",
        trd_env="SIMULATE",
        account=AccountSummary(),
        positions=list(positions),
        recent_orders=[],
    )

    class _FakeProvider:
        def get_status(self, *, trd_env: str | None = None) -> AccountStatus:
            return status

    return _FakeProvider()


def _capturing_execute(calls: list):
    def _execute(self, payload):  # noqa: ANN001
        calls.append(payload)
        return _ACCEPTED_RESULT

    return _execute


def _moomoo_payload(
    action: str = "buy",
    qty: float = 12.0,
    target_qty: float | None = 47.0,
    ticker: str = "US.TQQQ",
) -> dict:
    body = {
        "passphrase": "test-secret",
        "broker": "moomoo",
        "asset_class": "US",
        "action": action,
        "ticker": ticker,
        "quantity": qty,
    }
    if target_qty is not None:
        body["target_qty"] = target_qty
    return body


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_PASSPHRASE", "test-secret")
    monkeypatch.setenv("LIVE_EVENTS_PATH", str(tmp_path))
    app.state.order_router = build_default_router()
    app.state.fill_service = FillEventService(JsonlEventLogger())
    app.state.idempotency = IdempotencyStore(ttl_seconds=600)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        ac._tmp_path = tmp_path  # type: ignore[attr-defined]
        yield ac


def _events(tmp_path, event_type: str, suffix: str = "*.moomoo.jsonl") -> list[dict]:
    files = list(tmp_path.glob(suffix))
    if not files:
        return []
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    return [e for e in events if e["event_type"] == event_type]


@pytest.mark.anyio
async def test_target_buy_resolved_against_holding(client, monkeypatch):
    """Pine delta=12 / target=47 / 実保有 40 → broker には buy 7 が渡る。"""
    monkeypatch.delenv("MOOMOO_TARGET_QTY_RECONCILE", raising=False)
    app.state.status_provider = _provider_with(
        PositionRecord(code="US.TQQQ", qty=40.0, can_sell_qty=40.0)
    )
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=_moomoo_payload())

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0].action == "buy"
    assert calls[0].quantity == 7.0
    orders = _events(client._tmp_path, "order_recorded")
    assert len(orders) == 1
    assert orders[0]["status"] == "accepted"
    assert orders[0]["quantity"] == 7.0


@pytest.mark.anyio
async def test_target_reached_is_skipped(client, monkeypatch):
    """実保有が既に target → broker へ送らず skipped を記録・応答。"""
    monkeypatch.delenv("MOOMOO_TARGET_QTY_RECONCILE", raising=False)
    app.state.status_provider = _provider_with(
        PositionRecord(code="US.TQQQ", qty=47.0, can_sell_qty=47.0)
    )
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=_moomoo_payload())

    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    assert calls == []
    orders = _events(client._tmp_path, "order_recorded")
    assert len(orders) == 1
    assert orders[0]["status"] == "skipped"


@pytest.mark.anyio
async def test_action_flipped_when_holding_exceeds_target(client, monkeypatch):
    """Pine が buy と言っても実保有 50 > target 47 → sell 3 に補正される。"""
    monkeypatch.delenv("MOOMOO_TARGET_QTY_RECONCILE", raising=False)
    app.state.status_provider = _provider_with(
        PositionRecord(code="US.TQQQ", qty=50.0, can_sell_qty=50.0)
    )
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=_moomoo_payload(action="buy"))

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0].action == "sell"
    assert calls[0].quantity == 3.0


@pytest.mark.anyio
async def test_reconciled_sell_is_still_guarded(client, monkeypatch):
    """再解決後の SELL にも sell_guard が効く（多層防御の検証）。

    target 0 / 実保有 qty=5 だが can_sell_qty=3（凍結等）
    → reconcile は sell 5 を出すが sell_guard が 3 に clamp する。
    """
    monkeypatch.delenv("MOOMOO_TARGET_QTY_RECONCILE", raising=False)
    monkeypatch.setenv("MOOMOO_SELL_POSITION_GUARD", "1")
    app.state.status_provider = _provider_with(
        PositionRecord(code="US.TQQQ", qty=5.0, can_sell_qty=3.0)
    )
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post(
            "/webhook",
            json=_moomoo_payload(action="sell", qty=5.0, target_qty=0.0),
        )

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0].action == "sell"
    assert calls[0].quantity == 3.0


@pytest.mark.anyio
async def test_without_target_qty_behaves_as_delta(client, monkeypatch):
    """target_qty なし（既存 Pine）→ 従来どおり quantity をそのまま発注。"""
    monkeypatch.delenv("MOOMOO_TARGET_QTY_RECONCILE", raising=False)
    app.state.status_provider = _provider_with(
        PositionRecord(code="US.TQQQ", qty=40.0, can_sell_qty=40.0)
    )
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post(
            "/webhook", json=_moomoo_payload(target_qty=None)
        )

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0].quantity == 12.0


@pytest.mark.anyio
async def test_reconcile_disabled_passes_delta_through(client, monkeypatch):
    """kill-switch 無効時は target_qty があっても delta のまま発注（ロールバック手段）。"""
    monkeypatch.setenv("MOOMOO_TARGET_QTY_RECONCILE", "0")
    app.state.status_provider = _provider_with(
        PositionRecord(code="US.TQQQ", qty=40.0, can_sell_qty=40.0)
    )
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=_moomoo_payload())

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0].quantity == 12.0


@pytest.mark.anyio
async def test_provider_failure_fails_open_to_delta(client, monkeypatch):
    """StatusProvider 障害（OpenD down 等）→ fail-open で delta のまま発注継続。"""
    monkeypatch.delenv("MOOMOO_TARGET_QTY_RECONCILE", raising=False)

    class _BrokenProvider:
        def get_status(self, *, trd_env: str | None = None):
            raise RuntimeError("OpenD down")

    app.state.status_provider = _BrokenProvider()
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=_moomoo_payload())

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0].quantity == 12.0


@pytest.mark.anyio
async def test_oanda_target_falls_back_to_delta(client, monkeypatch):
    """OANDA はポジション照会未実装 → target_qty があっても delta フォールバック。"""
    monkeypatch.delenv("MOOMOO_TARGET_QTY_RECONCILE", raising=False)
    calls: list = []
    payload = {
        "passphrase": "test-secret",
        "broker": "oanda",
        "asset_class": "FX",
        "action": "buy",
        "ticker": "USDJPY",
        "quantity": 100.0,
        "target_qty": 300.0,
    }
    with patch(
        "alpha_strike.handlers.oanda_handler.OandaHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=payload)

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0].quantity == 100.0


@pytest.mark.anyio
async def test_negative_target_qty_is_rejected(client):
    """target_qty < 0 はスキーマバリデーションで 422。"""
    resp = await client.post("/webhook", json=_moomoo_payload(target_qty=-1.0))
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_signal_event_records_target_qty(client, monkeypatch):
    """SignalEvent に target_qty が記録される（alert replay の観測性）。"""
    monkeypatch.delenv("MOOMOO_TARGET_QTY_RECONCILE", raising=False)
    app.state.status_provider = _provider_with(
        PositionRecord(code="US.TQQQ", qty=40.0, can_sell_qty=40.0)
    )
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute([]),
    ):
        await client.post("/webhook", json=_moomoo_payload())

    signals = _events(client._tmp_path, "signal_received")
    assert len(signals) == 1
    assert signals[0]["target_qty"] == 47.0
    # 原シグナルの quantity は再解決前の値のまま（記録の透明性）
    assert signals[0]["quantity"] == 12.0
