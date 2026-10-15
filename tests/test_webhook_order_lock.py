"""発注ロックのタイムアウトが記録されることのテスト。

WHY: 2026-10-12 の障害では、OpenD が固まってロック保持スレッドが戻らなくなり、
後続のシグナルは 30 秒のタイムアウト後 503 になった。その 503 は try ブロックの
外で raise されていたため **イベントログに何も残らず**、7 営業日ぶん「シグナルは
記録されているのに発注の痕跡が無い」という読み取りしかできなかった。

黙って消える経路を 1 つも残さないため、503 も OrderEvent(status=failed) として残す。

fixture と payload は tests/test_webhook_carryover.py と同じ形にそろえている
（このリポジトリの webhook テストは async の AsyncClient + @pytest.mark.anyio）。
"""

from __future__ import annotations

import json
import threading

import pytest
from httpx import ASGITransport, AsyncClient

from alpha_strike import webhook_server
from alpha_strike.event_logger import JsonlEventLogger
from alpha_strike.services.fill_service import FillEventService
from alpha_strike.services.idempotency import IdempotencyStore
from alpha_strike.services.order_service import build_default_router
from alpha_strike.webhook_server import app


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


def _events(tmp_path, event_type: str) -> list[dict]:
    out: list[dict] = []
    for f in tmp_path.glob("*.moomoo.jsonl"):
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
    app.state.order_router = build_default_router()
    app.state.fill_service = FillEventService(JsonlEventLogger())
    app.state.idempotency = IdempotencyStore(ttl_seconds=600)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        ac._tmp_path = tmp_path  # type: ignore[attr-defined]
        yield ac


def _hold_order_lock(monkeypatch) -> None:
    """ロックを掴んだまま解放しない状態を作る（OpenD ハングの模擬）。"""
    monkeypatch.setattr(webhook_server, "_ORDER_LOCK_TIMEOUT_SECONDS", 0.05)
    held = threading.Lock()
    held.acquire()
    monkeypatch.setattr(webhook_server, "_ORDER_LOCK", held)


@pytest.mark.anyio
async def test_ロック取得失敗が失敗イベントとして残る(client, monkeypatch):
    _hold_order_lock(monkeypatch)

    resp = await client.post("/webhook", json=_moomoo_buy())

    assert resp.status_code == 503
    failed = [
        e
        for e in _events(client._tmp_path, "order_recorded")
        if e.get("status") == "failed" and e.get("error_type") == "OrderLockTimeout"
    ]
    assert len(failed) == 1, "503 が記録なしで消えている"
    # 後続の発注途絶 watchdog は (signal_id, ticker) の複合キーでシグナルと発注を
    # 照合する。失敗イベントのキーが取り違えられていないことをここで固定する。
    assert failed[0]["signal_id"] == "20261013-093000"
    assert failed[0]["ticker"] == "US.TLT"


@pytest.mark.anyio
async def test_シグナル受信の記録はロックより前に残る(client, monkeypatch):
    """ロックは signal_received の記録より後に置く設計（v1.3.0）の回帰テスト。"""
    _hold_order_lock(monkeypatch)

    resp = await client.post("/webhook", json=_moomoo_buy())

    # ロックが実際に効いて 503 になった上での記録であることを確かめる。
    # 通常経路に入っていても signal_received は存在するので、これが無いと素通りする。
    assert resp.status_code == 503
    assert _events(client._tmp_path, "signal_received"), "ロックより前の記録が消えている"
