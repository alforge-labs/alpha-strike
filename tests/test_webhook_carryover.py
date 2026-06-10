"""carry-over の webhook 統合テスト (#89)。

WHY: クローズ後着の SIMULATE moomoo US シグナルは broker へ投げると DAY が失効する。
受信時に「market closed なら broker へ送らず queued イベントを記録して 200」「開場中/REAL は
従来どおり即発注」という分岐を、broker 呼び出し・記録イベント・HTTP 応答の 3 点で固定する。
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from alpha_strike.event_logger import JsonlEventLogger
from alpha_strike.services.fill_service import FillEventService
from alpha_strike.services.idempotency import IdempotencyStore
from alpha_strike.services.order_service import build_default_router
from alpha_strike.webhook_server import app

_ACCEPTED_RESULT = {"order_id": "42", "ret_code": 0, "filled_qty": None, "filled_price": None}


class _FakeMarketState:
    def __init__(self, states: dict[str, str] | None = None, *, raises: bool = False):
        self._states = states or {}
        self.raises = raises

    def get_market_state(self, codes: list[str]) -> dict[str, str]:
        if self.raises:
            raise RuntimeError("OpenD quote 障害")
        return {c: self._states[c] for c in codes if c in self._states}


def _capturing_execute(calls: list):
    def _execute(self, payload):  # noqa: ANN001
        calls.append(payload)
        return _ACCEPTED_RESULT

    return _execute


def _moomoo_buy(qty: float = 2.0, ticker: str = "US.TLT") -> dict:
    return {
        "passphrase": "test-secret",
        "broker": "moomoo",
        "asset_class": "US",
        "action": "buy",
        "ticker": ticker,
        "quantity": qty,
        "run_mode": "paper",
        "signal_id": "20260608-093000",
    }


def _events(tmp_path, event_type: str) -> list[dict]:
    files = list(tmp_path.glob("*.moomoo.jsonl"))
    if not files:
        return []
    out: list[dict] = []
    for f in files:
        for line in f.read_text(encoding="utf-8").strip().splitlines():
            e = json.loads(line)
            if e.get("event_type") == event_type:
                out.append(e)
    return out


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_PASSPHRASE", "test-secret")
    monkeypatch.setenv("LIVE_EVENTS_PATH", str(tmp_path))
    monkeypatch.delenv("CARRYOVER_ENABLED", raising=False)
    app.state.order_router = build_default_router()
    app.state.fill_service = FillEventService(JsonlEventLogger())
    app.state.idempotency = IdempotencyStore(ttl_seconds=600)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        ac._tmp_path = tmp_path  # type: ignore[attr-defined]
        yield ac


@pytest.mark.anyio
async def test_simulate_closed_queues_and_skips_broker(client, monkeypatch):
    """SIMULATE×クローズ後 → broker を呼ばず signal_carryover_queued を記録し 200。

    これが効かないと post-close シグナルが DAY 失効で永久に約定しない（#89 の核心）。"""
    from unittest.mock import patch

    monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
    app.state.market_state_provider = _FakeMarketState({"US.TLT": "CLOSED"})
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=_moomoo_buy())

    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    # broker は呼ばれない
    assert calls == []
    # queued イベントが記録され、order_recorded は出ない
    queued = _events(client._tmp_path, "signal_carryover_queued")
    assert len(queued) == 1
    assert queued[0]["carryover_state"] == "queued"
    assert queued[0]["ticker"] == "US.TLT"
    assert _events(client._tmp_path, "order_recorded") == []


@pytest.mark.anyio
async def test_simulate_open_routes_normally(client, monkeypatch):
    """開場中(AFTERNOON)は carry-over せず従来どおり即発注する（レイテンシ退行/挙動変更を防ぐ）。"""
    from unittest.mock import patch

    monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
    app.state.market_state_provider = _FakeMarketState({"US.TLT": "AFTERNOON"})
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=_moomoo_buy())

    assert resp.status_code == 200
    assert len(calls) == 1  # broker へ即発注
    assert _events(client._tmp_path, "signal_carryover_queued") == []


@pytest.mark.anyio
async def test_real_does_not_carryover(client, monkeypatch):
    """REAL はクローズ後でも carry-over しない（GTC carry-over が効くため。二重ポジ防止）。"""
    from unittest.mock import patch

    monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
    app.state.market_state_provider = _FakeMarketState({"US.TLT": "CLOSED"})
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=_moomoo_buy())

    assert resp.status_code == 200
    assert len(calls) == 1  # 従来どおり即発注
    assert _events(client._tmp_path, "signal_carryover_queued") == []


@pytest.mark.anyio
async def test_market_unknown_routes_normally(client, monkeypatch):
    """market state 判定不能なら従来どおり即発注（誤キューで発注機会を逃さない fail-safe）。"""
    from unittest.mock import patch

    monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
    app.state.market_state_provider = _FakeMarketState(raises=True)
    calls: list = []
    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        _capturing_execute(calls),
    ):
        resp = await client.post("/webhook", json=_moomoo_buy())

    assert resp.status_code == 200
    assert len(calls) == 1
    assert _events(client._tmp_path, "signal_carryover_queued") == []
