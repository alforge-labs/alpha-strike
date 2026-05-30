"""read-only status API のテスト (issue #57 Phase 1)。

/status と /status/events の認証 (Bearer + fail-safe) と応答を network-free に検証する。
OpenD への実接続は FakeStatusProvider 注入で回避する。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from alpha_strike.event_logger import JsonlEventLogger
from alpha_strike.services.fill_service import FillEventService
from alpha_strike.services.idempotency import IdempotencyStore
from alpha_strike.services.order_service import build_default_router
from alpha_strike.services.status_service import (
    AccountStatus,
    AccountSummary,
    OrderRecord,
    PositionRecord,
)
from alpha_strike.webhook_server import app

_FAKE_STATUS = AccountStatus(
    broker="moomoo",
    trd_env="SIMULATE",
    account=AccountSummary(
        total_assets=1000005.58, cash=999557.31, power=1999562.88, market_val=448.27
    ),
    positions=[
        PositionRecord(
            code="US.AAPL",
            qty=1.0,
            can_sell_qty=1.0,
            cost_price=300.04,
            nominal_price=312.06,
            market_val=312.06,
            pl_val=12.02,
            pl_ratio=4.0,
        )
    ],
    recent_orders=[
        OrderRecord(
            code="US.GLD",
            trd_side="SELL",
            order_type="MARKET",
            qty=1.0,
            order_status="CANCELLED_ALL",
            dealt_qty=0.0,
            dealt_avg_price=0.0,
            order_id="366675",
            create_time="2026-05-29 16:01:21",
        )
    ],
)


class _FakeStatusProvider:
    def get_status(self, *, trd_env: str | None = None) -> AccountStatus:
        return _FAKE_STATUS


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("WEBHOOK_PASSPHRASE", "test-secret")
    app.state.order_router = build_default_router()
    app.state.fill_service = FillEventService(JsonlEventLogger())
    app.state.idempotency = IdempotencyStore(ttl_seconds=600)
    app.state.status_provider = _FakeStatusProvider()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ==================== 認証 ====================


@pytest.mark.anyio
async def test_status_disabled_when_token_unset_returns_503(client, monkeypatch):
    """STATUS_API_TOKEN 未設定なら fail-safe で 503（デフォルト非公開）。"""
    monkeypatch.delenv("STATUS_API_TOKEN", raising=False)
    resp = await client.get("/status")
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_status_missing_token_returns_401(client, monkeypatch):
    monkeypatch.setenv("STATUS_API_TOKEN", "secret-token")
    resp = await client.get("/status")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_status_wrong_token_returns_401(client, monkeypatch):
    monkeypatch.setenv("STATUS_API_TOKEN", "secret-token")
    resp = await client.get(
        "/status", headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401


# ==================== 応答 ====================


@pytest.mark.anyio
async def test_status_ok_returns_account_positions_orders(client, monkeypatch):
    monkeypatch.setenv("STATUS_API_TOKEN", "secret-token")
    resp = await client.get(
        "/status", headers={"Authorization": "Bearer secret-token"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["broker"] == "moomoo"
    assert body["trd_env"] == "SIMULATE"
    assert body["account"]["total_assets"] == pytest.approx(1000005.58)
    assert any(p["code"] == "US.AAPL" for p in body["positions"])
    # submission != fill の盲点を潰す: 実 order_status (CANCELLED_ALL) が見える
    gld = next(o for o in body["recent_orders"] if o["code"] == "US.GLD")
    assert gld["order_status"] == "CANCELLED_ALL"
    assert gld["dealt_qty"] == 0.0


@pytest.mark.anyio
async def test_status_events_returns_recent_events(client, monkeypatch):
    monkeypatch.setenv("STATUS_API_TOKEN", "secret-token")

    def _fake_load_events(**kwargs):
        return [
            {"event_type": "FillEvent", "ticker": "US.TQQQ", "broker": "moomoo"},
            {"event_type": "SignalEvent", "ticker": "US.GLD", "broker": "moomoo"},
        ]

    monkeypatch.setattr(
        "alpha_strike.webhook_server.event_logger.load_events", _fake_load_events
    )
    resp = await client.get(
        "/status/events?limit=10",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["events"], list)
    assert body["events"][0]["event_type"] == "FillEvent"


@pytest.mark.anyio
async def test_status_events_requires_token(client, monkeypatch):
    monkeypatch.setenv("STATUS_API_TOKEN", "secret-token")
    resp = await client.get("/status/events")
    assert resp.status_code == 401


def test_status_service_uses_futu_not_moomoo():
    """回帰防止 (#57): status_service は本体と同じ `futu` を import する。

    `moomoo` を import すると、本体 handlers の `import futu` と同一 SDK が二重ロードされ、
    protobuf 記述子の重複登録 (duplicate file name Trd_Common.proto) でサーバープロセス内の
    broker クエリが 502 になる。
    """
    import alpha_strike.services.status_service as ss
    from pathlib import Path

    src = Path(ss.__file__).read_text(encoding="utf-8")
    assert "import futu" in src
    assert "import moomoo" not in src
