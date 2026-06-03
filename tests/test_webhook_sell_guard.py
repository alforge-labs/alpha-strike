"""over-sell ガードの webhook 統合テスト。

moomoo の SELL が broker の実保有 (can_sell_qty) を超えないよう
clamp / skip されること、ガード無効時は従来通り素通しすること、
BUY には作用しないことを network-free に検証する。

WHY: 本ガードは「Pine→webhook→broker の open-loop desync による over-sell
（RuntimeError: Not enough positions）」を broker 境界で根絶するためのもの。
broker へ渡る数量・記録イベント・HTTP 応答の 3 点を固定して退行を防ぐ。
"""

from __future__ import annotations

import json

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


def _moomoo_sell(qty: float, ticker: str = "US.TQQQ") -> dict:
    return {
        "passphrase": "test-secret",
        "broker": "moomoo",
        "asset_class": "US",
        "action": "sell",
        "ticker": ticker,
        "quantity": qty,
    }


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


def _order_events(tmp_path) -> list[dict]:
    files = list(tmp_path.glob("*.moomoo.jsonl"))
    if not files:
        return []
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    return [e for e in events if e["event_type"] == "order_recorded"]


@pytest.mark.anyio
async def test_oversell_is_clamped_to_position(client, monkeypatch):
    """実保有 1 に対し SELL 3 → broker には 1 だけ渡り、accepted で記録される。"""
    from unittest.mock import patch

    monkeypatch.setenv("MOOMOO_SELL_POSITION_GUARD", "1")
    app.state.status_provider = _provider_with(
        PositionRecord(code="US.TQQQ", qty=1.0, can_sell_qty=1.0)
    )
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=_moomoo_sell(3.0))

    assert resp.status_code == 200
    # broker へ渡った数量が実保有まで clamp されている
    assert len(calls) == 1
    assert calls[0].quantity == 1.0
    # order_recorded は clamp 後の数量・accepted
    orders = _order_events(client._tmp_path)
    assert len(orders) == 1
    assert orders[0]["status"] == "accepted"
    assert orders[0]["quantity"] == 1.0


@pytest.mark.anyio
async def test_sell_without_position_is_skipped(client, monkeypatch):
    """対象建玉なし → broker へ送らず status=skipped を記録し応答も skipped。"""
    from unittest.mock import patch

    monkeypatch.setenv("MOOMOO_SELL_POSITION_GUARD", "1")
    app.state.status_provider = _provider_with(
        PositionRecord(code="US.TQQQ", qty=1.0, can_sell_qty=1.0)
    )
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=_moomoo_sell(1.0, ticker="US.GLD"))

    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
    # broker は呼ばれない
    assert calls == []
    # order_recorded は status=skipped、数量は要求値のまま（記録の透明性）
    orders = _order_events(client._tmp_path)
    assert len(orders) == 1
    assert orders[0]["status"] == "skipped"
    assert orders[0]["quantity"] == 1.0


@pytest.mark.anyio
async def test_guard_disabled_passes_oversell_through(client, monkeypatch):
    """ガード無効時は従来通り over-sell をそのまま broker へ渡す（後方互換）。"""
    from unittest.mock import patch

    monkeypatch.setenv("MOOMOO_SELL_POSITION_GUARD", "0")
    app.state.status_provider = _provider_with(
        PositionRecord(code="US.TQQQ", qty=1.0, can_sell_qty=1.0)
    )
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=_moomoo_sell(3.0))

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0].quantity == 3.0  # clamp されない


@pytest.mark.anyio
async def test_buy_is_not_affected_by_guard(client, monkeypatch):
    """BUY はガード対象外（建玉が無くても素通し）。"""
    from unittest.mock import patch

    monkeypatch.setenv("MOOMOO_SELL_POSITION_GUARD", "1")
    app.state.status_provider = _provider_with()  # 建玉ゼロ
    calls: list = []
    buy = {**_moomoo_sell(3.0), "action": "buy"}
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=buy)

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0].quantity == 3.0
