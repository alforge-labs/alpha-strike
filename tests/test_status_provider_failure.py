"""OpenD 障害（クエリ失敗）を「建玉ゼロ」と読み替えないことのテスト。

WHY: OpenD 呼び出しの有界化で、OpenD が死ぬとクエリは無限ハングではなく
``(RET_ERROR, "Connect timeout")`` を返すようになった。status_service がこれを
空リスト＝「建玉ゼロ」と読むと、sell guard / target reconcile が決済シグナルを
``skipped`` にし、発注途絶 watchdog も ``skipped`` を正常と判定して沈黙する
（2026-10-12 の発注欠落と同じ「黙った欠落」の再来）。

ここで固定するのは「status が使えないとき、決済シグナルは決して ``skipped`` に
ならない」こと。webhook 側のガード例外は fail-closed（照会に失敗したら発注しない）なので、
全面障害でも参照用だけ死んだ部分障害でも ``failed`` になる。部分障害で ``accepted``
（建玉チェック無しの発注）を許すと空売りになりうるため、ユーザーの判断で fail-open から
fail-closed に変更した。
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from alpha_strike.event_logger import JsonlEventLogger
from alpha_strike.services.fill_service import FillEventService
from alpha_strike.services.idempotency import IdempotencyStore
from alpha_strike.services.order_service import build_default_router
from alpha_strike.services.status_service import MoomooStatusProvider
from alpha_strike.webhook_server import app

_RET_OK = 0
_RET_ERROR = -1
_CONNECT_TIMEOUT = "Connect timeout"


class _FakeTradeContext:
    """futu.OpenSecTradeContext の偽物。3 つのクエリの戻り値をクラス属性で切り替える。

    bounded_context_class が要求する private メンバ（``_init_connect_sync`` /
    ``_wait_reconnect``）も模す。
    """

    accinfo_result: tuple = (_RET_OK, pd.DataFrame())
    position_result: tuple = (_RET_OK, pd.DataFrame())
    order_result: tuple = (_RET_OK, pd.DataFrame())
    instances: list["_FakeTradeContext"] = []

    def __init__(self, **kwargs: object) -> None:
        self.closed = False
        _FakeTradeContext.instances.append(self)

    def _init_connect_sync(self):  # noqa: ANN202
        return _RET_OK

    def _wait_reconnect(self, wait_reconnect_interval: int = 6) -> None:
        return None

    def set_sync_query_connect_timeout(self, timeout: float) -> None:
        return None

    def accinfo_query(self, **kwargs: object) -> tuple:
        return self.accinfo_result

    def position_list_query(self, **kwargs: object) -> tuple:
        return self.position_result

    def order_list_query(self, **kwargs: object) -> tuple:
        return self.order_result

    def close(self) -> None:
        self.closed = True


def _install_fake_futu(monkeypatch, ctx_cls: type) -> None:
    """status_service の遅延 import が拾う futu を差し替える。"""
    fake = MagicMock()
    fake.RET_OK = _RET_OK
    fake.RET_ERROR = _RET_ERROR
    fake.OpenSecTradeContext = ctx_cls
    monkeypatch.setitem(sys.modules, "futu", fake)


def _dead_context_class() -> type:
    """OpenD 死亡: 3 つのクエリすべてが Connect timeout を返す。"""

    class _Dead(_FakeTradeContext):
        accinfo_result = (_RET_ERROR, _CONNECT_TIMEOUT)
        position_result = (_RET_ERROR, _CONNECT_TIMEOUT)
        order_result = (_RET_ERROR, _CONNECT_TIMEOUT)

    return _Dead


class TestProviderRaisesOnQueryFailure:
    def setup_method(self) -> None:
        _FakeTradeContext.instances.clear()

    @pytest.mark.parametrize("failing", ["accinfo", "position", "order"])
    def test_クエリ失敗はRuntimeErrorになる(self, monkeypatch, failing):
        """どのクエリの失敗も空として返さない（一部成功のスナップショットは照合を誤らせる）。"""

        class _OneFails(_FakeTradeContext):
            pass

        setattr(_OneFails, f"{failing}_result", (_RET_ERROR, _CONNECT_TIMEOUT))
        _install_fake_futu(monkeypatch, _OneFails)

        with pytest.raises(RuntimeError, match=_CONNECT_TIMEOUT):
            MoomooStatusProvider(host="127.0.0.1", port=11111).get_status()

    def test_RET_OKかつ空は建玉ゼロとして返す(self, monkeypatch):
        """「建玉が本当にゼロ」の正当なケースを例外にしない（既存挙動の回帰防止）。"""
        _install_fake_futu(monkeypatch, _FakeTradeContext)

        status = MoomooStatusProvider(host="127.0.0.1", port=11111).get_status()

        assert status.positions == []
        assert status.recent_orders == []
        assert status.account.total_assets is None

    def test_クエリが失敗してもcloseする(self, monkeypatch):
        """例外経路でも ctx を閉じる（閉じないと futu の再接続タイマーが漏れる）。"""
        _install_fake_futu(monkeypatch, _dead_context_class())

        with pytest.raises(RuntimeError):
            MoomooStatusProvider(host="127.0.0.1", port=11111).get_status()

        (ctx,) = _FakeTradeContext.instances
        assert ctx.closed


# --- webhook レベル -----------------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_PASSPHRASE", "test-secret")
    monkeypatch.setenv("LIVE_EVENTS_PATH", str(tmp_path))
    monkeypatch.setenv("MOOMOO_SELL_POSITION_GUARD", "1")
    app.state.order_router = build_default_router()
    app.state.fill_service = FillEventService(JsonlEventLogger())
    app.state.idempotency = IdempotencyStore(ttl_seconds=600)
    # 本物の provider に「死んだ OpenD」を差し込む。偽 provider で例外を投げると
    # status_service の読み替え（本テストの対象）を通らない
    _install_fake_futu(monkeypatch, _dead_context_class())
    app.state.status_provider = MoomooStatusProvider(host="127.0.0.1", port=11111)
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


def _moomoo_sell() -> dict:
    return {
        "passphrase": "test-secret",
        "broker": "moomoo",
        "asset_class": "US",
        "action": "sell",
        "ticker": "US.TQQQ",
        "quantity": 1.0,
    }


@pytest.mark.anyio
async def test_全面障害の決済シグナルはfailedで記録される(client):
    """status も発注も死んでいる → failed（watchdog が鳴る経路）。skipped にしない。

    fail-closed 以降は照会の段階で止まるため、発注用の接続（``_broker_dead``）には到達しない。
    それでも残すのは、全面障害で「黙って skipped になる」C-1 の退行を固定するため。
    """

    def _broker_dead(self, payload):  # noqa: ANN001
        raise RuntimeError(_CONNECT_TIMEOUT)

    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute", _broker_dead
    ):
        await client.post("/webhook", json=_moomoo_sell())

    orders = _order_events(client._tmp_path)
    assert [(o["status"], o.get("error_type")) for o in orders] == [
        ("failed", "StatusUnavailable")
    ]


@pytest.mark.anyio
async def test_参照用だけ死んだら決済シグナルはfail_closedで発注しない(client):
    """status だけ死んで発注は通る → fail-closed で broker へ送らず failed。skipped にしない。

    ユーザーの判断で fail-open から fail-closed に変更した。部分障害で建玉チェック無しの
    発注を通すと、Pine の数量計算が実際の建玉とずれていれば空売りになりうるため。
    """
    calls: list = []

    def _execute(self, payload):  # noqa: ANN001
        calls.append(payload)
        return {"order_id": "42", "ret_code": 0, "filled_qty": None, "filled_price": None}

    with patch("alpha_strike.handlers.moomoo_handler.MoomooHandler.execute", _execute):
        resp = await client.post("/webhook", json=_moomoo_sell())

    assert resp.status_code == 502
    assert calls == []
    orders = _order_events(client._tmp_path)
    assert [(o["status"], o["error_type"]) for o in orders] == [
        ("failed", "StatusUnavailable")
    ]


@pytest.mark.anyio
async def test_OpenD死亡時の_status_は502でConnect_timeoutを返す(client, monkeypatch):
    """発注途絶の通知本文が案内する切り分け（/status が 502・Connect timeout）の根拠。

    有界化前は無限ハング（Cloudflare 経由で 524）、C-1 修正前は 200 で建玉ゼロを
    返していた。どちらに戻っても運用者の切り分けが誤るので値ごと固定する。
    """
    monkeypatch.setenv("STATUS_API_TOKEN", "test-token")

    resp = await client.get("/status", headers={"Authorization": "Bearer test-token"})

    assert resp.status_code == 502
    assert _CONNECT_TIMEOUT in resp.json()["detail"]
