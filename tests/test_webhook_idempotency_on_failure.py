"""発注に失敗した webhook の再送が、冪等キーで黙って捨てられないことのテスト。

WHY: 冪等キーはロック取得より前に記録される。失敗パス（ロックタイムアウトの 503・
route 例外の 502・設定エラーの 500）でキーを残すと、TTL（既定 10 分）のあいだ同じ
シグナルの再送は ``duplicate signal_id — already processed`` と HTTP 200 で返り、
**注文が出ないのに呼び出し側からは成功に見える**。2026-10-12 の発注途絶障害の
恒久対策（黙って注文が消える経路を残さない）と同じクラスの欠陥。

一方で route が成功したあとの後処理（イベント記録・fill 処理）で例外が出た場合は、
注文はブローカーに届いている。ここでキーを取り消すと再送で二重発注になるため、
キーを残す（carryover.py の再発注も route の失敗時だけキーを解放する）。

成功時にキーが残り続けること（重複 webhook を弾く本来の目的）は
tests/test_webhook_server.py::test_duplicate_signal_id_not_routed_to_broker が固定している。
"""

from __future__ import annotations

import threading

import pytest
from httpx import ASGITransport, AsyncClient

from alpha_strike import webhook_server
from alpha_strike.event_logger import JsonlEventLogger
from alpha_strike.services.fill_service import FillEventService
from alpha_strike.services.idempotency import IdempotencyStore
from alpha_strike.services.order_service import build_default_router
from alpha_strike.webhook_server import app, limiter


def _moomoo_buy() -> dict:
    return {
        "passphrase": "test-secret",
        "broker": "moomoo",
        "asset_class": "US",
        "action": "buy",
        "ticker": "US.TLT",
        "quantity": 2.0,
        "run_mode": "paper",
        "signal_id": "20261013-093000",
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
    # 他テストが app.state に残した provider で carry-over・照合に分岐しないようにする。
    monkeypatch.setattr(app.state, "market_state_provider", None, raising=False)
    monkeypatch.setattr(app.state, "status_provider", None, raising=False)
    limiter._storage.reset()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


class _Route:
    """1 回目は ``first`` を送出し、2 回目以降は受理する route の差し替え。"""

    def __init__(self, first: Exception | None = None) -> None:
        self.first = first
        self.calls = 0

    def __call__(self, payload):  # noqa: ANN001, ARG002
        self.calls += 1
        if self.calls == 1 and self.first is not None:
            raise self.first
        return {"order_id": f"bo-{self.calls}"}


async def _resend(client: AsyncClient):
    limiter._storage.reset()
    return await client.post("/webhook", json=_moomoo_buy())


def _assert_not_duplicate(resp) -> None:
    assert resp.status_code == 200
    assert "duplicate" not in resp.json()["message"].lower(), (
        "発注していないのに再送が重複として捨てられた"
    )


@pytest.mark.anyio
async def test_ロックタイムアウト503の後の再送は発注される(client, monkeypatch):
    route = _Route()
    monkeypatch.setattr(app.state.order_router, "route", route)
    # 1 回目: ロックを掴まれたまま（OpenD ハングの模擬）で 503。
    monkeypatch.setattr(webhook_server, "_ORDER_LOCK_TIMEOUT_SECONDS", 0.05)
    held = threading.Lock()
    held.acquire()
    monkeypatch.setattr(webhook_server, "_ORDER_LOCK", held)

    first = await client.post("/webhook", json=_moomoo_buy())
    assert first.status_code == 503

    # 2 回目: ロックが空いた後の再送（OpenD 復旧後の再送の模擬）。
    monkeypatch.setattr(webhook_server, "_ORDER_LOCK", threading.Lock())
    second = await _resend(client)

    _assert_not_duplicate(second)
    assert route.calls == 1


@pytest.mark.anyio
async def test_route例外502の後の再送は発注される(client, monkeypatch):
    route = _Route(first=RuntimeError("OpenD connection lost"))
    monkeypatch.setattr(app.state.order_router, "route", route)

    first = await client.post("/webhook", json=_moomoo_buy())
    assert first.status_code == 502

    second = await _resend(client)

    _assert_not_duplicate(second)
    assert route.calls == 2


@pytest.mark.anyio
async def test_設定エラー500の後の再送は発注される(client, monkeypatch):
    route = _Route(first=ValueError("MOOMOO_TRD_ENV が不正"))
    monkeypatch.setattr(app.state.order_router, "route", route)

    first = await client.post("/webhook", json=_moomoo_buy())
    assert first.status_code == 500

    second = await _resend(client)

    _assert_not_duplicate(second)
    assert route.calls == 2


@pytest.mark.anyio
async def test_route成功後の後処理で失敗した場合は再送を重複として弾く(client, monkeypatch):
    """注文はブローカーに届いている。キーを取り消すと再送で二重発注になる。"""
    route = _Route()
    monkeypatch.setattr(app.state.order_router, "route", route)
    fill_service = app.state.fill_service
    original_build = fill_service.build
    calls = {"n": 0}

    def _build_fails_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("fill 構築に失敗")
        return original_build(**kwargs)

    monkeypatch.setattr(fill_service, "build", _build_fails_once)

    first = await client.post("/webhook", json=_moomoo_buy())
    assert first.status_code == 502

    second = await _resend(client)

    assert second.status_code == 200
    assert "duplicate" in second.json()["message"].lower()
    assert route.calls == 1, "発注済みのシグナルが再送で二重発注された"
