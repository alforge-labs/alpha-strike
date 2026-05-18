"""Webhook サーバーのインテグレーションテスト"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from alpha_strike.event_logger import JsonlEventLogger
from alpha_strike.services.fill_service import FillEventService
from alpha_strike.services.idempotency import IdempotencyStore
from alpha_strike.services.order_service import build_default_router
from alpha_strike.webhook_server import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("WEBHOOK_PASSPHRASE", "test-secret")
    # lifespan を経由せず直接 app.state を初期化する
    app.state.order_router = build_default_router()
    app.state.fill_service = FillEventService(JsonlEventLogger())
    # idempotency store: 各テストごとに新規（テスト間で signal_id 衝突しないよう）
    app.state.idempotency = IdempotencyStore(ttl_seconds=600)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


BASE_PAYLOAD = {
    "passphrase": "test-secret",
    "broker": "oanda",
    "asset_class": "FX",
    "action": "buy",
    "ticker": "USDJPY",
    "quantity": 1000,
}

TRADE_CLOSED_PAYLOAD = {
    "passphrase": "test-secret",
    "signal_id": "sig_manual_001",
    "trade_id": "trd_001",
    "closed_at": "2026-03-29T11:00:00+09:00",
    "broker": "oanda",
    "asset_class": "FX",
    "action": "buy",
    "ticker": "USDJPY",
    "quantity": 1000,
    "entry_price": 149.235,
    "exit_price": 149.800,
    "gross_pnl": 570.0,
    "net_pnl": 565.0,
    "strategy_id": "sma_crossover_v1",
    "strategy_version": "1.2.0",
    "snapshot_id": "snap_20260329190300123456",
    "run_mode": "live",
    "commission": 5.0,
    "exit_reason": "signal_exit",
}


@pytest.mark.anyio
async def test_health_check(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_wrong_passphrase_returns_401(client):
    payload = {**BASE_PAYLOAD, "passphrase": "wrong"}
    response = await client.post("/webhook", json=payload)
    assert response.status_code == 401


@pytest.mark.anyio
async def test_invalid_broker_returns_422(client):
    payload = {**BASE_PAYLOAD, "broker": "ig"}
    response = await client.post("/webhook", json=payload)
    assert response.status_code == 422


@pytest.mark.anyio
async def test_invalid_asset_class_returns_422(client):
    payload = {**BASE_PAYLOAD, "asset_class": "UNKNOWN"}
    response = await client.post("/webhook", json=payload)
    assert response.status_code == 422


@pytest.mark.anyio
async def test_invalid_ticker_pattern_returns_422(client):
    payload = {**BASE_PAYLOAD, "ticker": "usd/jpy"}  # 小文字・スラッシュは不可
    response = await client.post("/webhook", json=payload)
    assert response.status_code == 422


@pytest.mark.anyio
async def test_zero_quantity_returns_422(client):
    payload = {**BASE_PAYLOAD, "quantity": 0}
    response = await client.post("/webhook", json=payload)
    assert response.status_code == 422


@pytest.mark.anyio
async def test_oanda_handler_called_for_oanda_broker(client, monkeypatch):
    from unittest.mock import patch, MagicMock

    mock_response = MagicMock()
    mock_response.json.return_value = {"orderCreateTransaction": {"id": "42"}}
    mock_response.raise_for_status = MagicMock()

    monkeypatch.setenv("OANDA_API_KEY", "test-key")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("OANDA_ENV", "PRACTICE")

    with patch("alpha_strike.handlers.oanda_handler.requests.post", return_value=mock_response):
        response = await client.post("/webhook", json=BASE_PAYLOAD)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["broker"] == "oanda"
    assert data["signal_id"].startswith("sig_")
    assert data["order_id"].startswith("ord_")
    assert data["event_id"].startswith("evt_")


@pytest.mark.anyio
async def test_oanda_config_error_returns_500(client, monkeypatch):
    monkeypatch.delenv("OANDA_API_KEY", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)

    response = await client.post("/webhook", json=BASE_PAYLOAD)
    assert response.status_code == 500
    # 内部情報が漏洩していないことを確認
    assert "OANDA_API_KEY" not in response.json().get("detail", "")


@pytest.mark.anyio
async def test_error_detail_does_not_leak_internals(client, monkeypatch):
    """502エラー時にスタックトレースや内部情報が漏洩しないことを確認"""
    from unittest.mock import patch
    import requests as req_lib

    monkeypatch.setenv("OANDA_API_KEY", "key")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "acc")
    monkeypatch.setenv("OANDA_ENV", "PRACTICE")

    with patch("alpha_strike.handlers.oanda_handler.requests.post", side_effect=req_lib.ConnectionError("internal host detail")):
        response = await client.post("/webhook", json=BASE_PAYLOAD)

    assert response.status_code == 502
    assert "internal host detail" not in response.json().get("detail", "")


@pytest.mark.anyio
async def test_moomoo_handler_called_for_moomoo_broker(client):
    from unittest.mock import patch

    moomoo_payload = {
        **BASE_PAYLOAD,
        "broker": "moomoo",
        "asset_class": "US",
        "ticker": "US.AAPL",
        "quantity": 10,
    }
    with patch("alpha_strike.handlers.moomoo_handler.MoomooHandler.execute", return_value={"order_id": "42", "ret_code": 0}):
        response = await client.post("/webhook", json=moomoo_payload)

    assert response.status_code == 200
    data = response.json()
    assert data["broker"] == "moomoo"


@pytest.mark.anyio
async def test_payload_accepts_live_tracking_metadata(client, monkeypatch, tmp_path):
    from unittest.mock import patch

    monkeypatch.setenv("LIVE_EVENTS_PATH", str(tmp_path))
    monkeypatch.setenv("OANDA_API_KEY", "test-key")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("OANDA_ENV", "PRACTICE")

    payload = {
        **BASE_PAYLOAD,
        "strategy_id": "sma_crossover_v1",
        "strategy_version": "1.2.0",
        "snapshot_id": "snap_20260329190300123456",
        "signal_id": "sig_manual_001",
        "timeframe": "1h",
        "run_mode": "live",
        "alert_name": "SMA Long",
    }

    with patch("alpha_strike.handlers.oanda_handler.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {"orderCreateTransaction": {"id": "42"}}
        mock_post.return_value.raise_for_status.return_value = None
        response = await client.post("/webhook", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["signal_id"] == "sig_manual_001"
    assert data["broker_order_id"] == "42"


@pytest.mark.anyio
async def test_webhook_writes_signal_and_order_events(client, monkeypatch, tmp_path):
    from unittest.mock import patch

    monkeypatch.setenv("LIVE_EVENTS_PATH", str(tmp_path))
    monkeypatch.setenv("OANDA_API_KEY", "test-key")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("OANDA_ENV", "PRACTICE")

    with patch("alpha_strike.handlers.oanda_handler.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {
            "orderCreateTransaction": {"id": "42"},
            "orderFillTransaction": {"id": "314", "units": "1000", "price": "149.235"},
        }
        mock_post.return_value.raise_for_status.return_value = None
        response = await client.post("/webhook", json=BASE_PAYLOAD)

    assert response.status_code == 200

    event_files = list(tmp_path.glob("*.oanda.jsonl"))
    assert len(event_files) == 1
    lines = event_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    signal_event = json.loads(lines[0])
    order_event = json.loads(lines[1])
    fill_event = json.loads(lines[2])
    assert signal_event["event_type"] == "signal_received"
    assert order_event["event_type"] == "order_recorded"
    assert order_event["status"] == "accepted"
    assert fill_event["event_type"] == "fill_received"
    assert fill_event["trade_id"] == "trd_314"
    assert fill_event["filled_qty"] == 1000.0
    assert fill_event["filled_price"] == 149.235


@pytest.mark.anyio
async def test_trade_closed_endpoint_requires_auth(client):
    payload = {**TRADE_CLOSED_PAYLOAD, "passphrase": "wrong"}
    response = await client.post("/events/trade-closed", json=payload)
    assert response.status_code == 401


@pytest.mark.anyio
async def test_trade_closed_endpoint_writes_event(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_EVENTS_PATH", str(tmp_path))

    response = await client.post("/events/trade-closed", json=TRADE_CLOSED_PAYLOAD)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert data["event_id"].startswith("evt_")

    event_files = list(tmp_path.glob("*.oanda.jsonl"))
    assert len(event_files) == 1
    lines = event_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_type"] == "trade_closed"
    assert event["trade_id"] == "trd_001"
    assert event["net_pnl"] == 565.0
    assert event["exit_reason"] == "signal_exit"


@pytest.mark.anyio
async def test_moomoo_opposite_fill_emits_trade_closed(client, monkeypatch, tmp_path):
    from unittest.mock import patch

    monkeypatch.setenv("LIVE_EVENTS_PATH", str(tmp_path))

    buy_payload = {
        **BASE_PAYLOAD,
        "broker": "moomoo",
        "asset_class": "US",
        "ticker": "US.AAPL",
        "quantity": 10,
        "strategy_id": "meanrev_v1",
        "strategy_version": "1.0.0",
        "snapshot_id": "snap_20260329190300123456",
    }
    sell_payload = {
        **buy_payload,
        "action": "sell",
        "signal_id": "sig_exit_001",
    }

    side_effect = [
        {"order_id": "42", "ret_code": 0, "fill_id": "fill_buy_001", "filled_qty": 10, "filled_price": 100.0},
        {"order_id": "43", "ret_code": 0, "fill_id": "fill_sell_001", "filled_qty": 10, "filled_price": 110.0},
    ]

    with patch("alpha_strike.handlers.moomoo_handler.MoomooHandler.execute", side_effect=side_effect):
        buy_response = await client.post("/webhook", json=buy_payload)
        sell_response = await client.post("/webhook", json=sell_payload)

    assert buy_response.status_code == 200
    assert sell_response.status_code == 200

    event_files = list(tmp_path.glob("*.moomoo.jsonl"))
    assert len(event_files) == 1
    lines = event_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 7
    exit_fill_event = json.loads(lines[5])
    trade_closed_event = json.loads(lines[6])
    assert exit_fill_event["event_type"] == "fill_received"
    assert trade_closed_event["event_type"] == "trade_closed"
    assert trade_closed_event["trade_id"] == exit_fill_event["trade_id"]
    assert trade_closed_event["entry_price"] == 100.0
    assert trade_closed_event["exit_price"] == 110.0
    assert trade_closed_event["gross_pnl"] == 100.0
    assert trade_closed_event["net_pnl"] == 100.0
    assert trade_closed_event["exit_reason"] == "opposite_fill"


@pytest.mark.anyio
async def test_oanda_opposite_fill_emits_trade_closed(client, monkeypatch, tmp_path):
    from unittest.mock import patch
    from alpha_strike.webhook_server import limiter

    monkeypatch.setenv("LIVE_EVENTS_PATH", str(tmp_path))

    buy_payload = {
        **BASE_PAYLOAD,
        "strategy_id": "fx_rev_v1",
        "strategy_version": "1.0.0",
        "snapshot_id": "snap_20260329190300123456",
    }
    sell_payload = {
        **buy_payload,
        "action": "sell",
        "signal_id": "sig_exit_002",
    }

    side_effect = [
        {"order_id": "42", "fill_id": "fill_buy_001", "filled_qty": 1000, "filled_price": 149.235},
        {"order_id": "43", "fill_id": "fill_sell_001", "filled_qty": 1000, "filled_price": 149.8},
    ]

    with patch("alpha_strike.handlers.oanda_handler.OandaHandler.execute", side_effect=side_effect):
        limiter._storage.reset()
        buy_response = await client.post("/webhook", json=buy_payload)
        limiter._storage.reset()
        sell_response = await client.post("/webhook", json=sell_payload)

    assert buy_response.status_code == 200
    assert sell_response.status_code == 200

    event_files = list(tmp_path.glob("*.oanda.jsonl"))
    assert len(event_files) == 1
    lines = event_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 7
    exit_fill_event = json.loads(lines[5])
    trade_closed_event = json.loads(lines[6])
    assert exit_fill_event["event_type"] == "fill_received"
    assert trade_closed_event["event_type"] == "trade_closed"
    assert trade_closed_event["trade_id"] == exit_fill_event["trade_id"]
    assert trade_closed_event["entry_price"] == 149.235
    assert trade_closed_event["exit_price"] == 149.8
    assert trade_closed_event["gross_pnl"] == 565.0
    assert trade_closed_event["net_pnl"] == 565.0
    assert trade_closed_event["exit_reason"] == "opposite_fill"


@pytest.mark.anyio
async def test_moomoo_split_exit_emits_trade_closed_after_final_fill(client, monkeypatch, tmp_path):
    from unittest.mock import patch

    monkeypatch.setenv("LIVE_EVENTS_PATH", str(tmp_path))

    buy_payload = {
        **BASE_PAYLOAD,
        "broker": "moomoo",
        "asset_class": "US",
        "ticker": "US.AAPL",
        "quantity": 10,
        "strategy_id": "meanrev_v2",
        "strategy_version": "1.1.0",
        "snapshot_id": "snap_20260329190300123456",
    }
    partial_sell_payload = {
        **buy_payload,
        "action": "sell",
        "quantity": 4,
        "signal_id": "sig_exit_partial_001",
    }
    final_sell_payload = {
        **buy_payload,
        "action": "sell",
        "quantity": 6,
        "signal_id": "sig_exit_final_001",
    }

    side_effect = [
        {"order_id": "42", "ret_code": 0, "fill_id": "fill_buy_010", "filled_qty": 10, "filled_price": 100.0},
        {"order_id": "43", "ret_code": 0, "fill_id": "fill_sell_011", "filled_qty": 4, "filled_price": 105.0},
        {"order_id": "44", "ret_code": 0, "fill_id": "fill_sell_012", "filled_qty": 6, "filled_price": 110.0},
    ]

    with patch("alpha_strike.handlers.moomoo_handler.MoomooHandler.execute", side_effect=side_effect):
        buy_response = await client.post("/webhook", json=buy_payload)
        partial_response = await client.post("/webhook", json=partial_sell_payload)
        final_response = await client.post("/webhook", json=final_sell_payload)

    assert buy_response.status_code == 200
    assert partial_response.status_code == 200
    assert final_response.status_code == 200

    event_files = list(tmp_path.glob("*.moomoo.jsonl"))
    assert len(event_files) == 1
    lines = event_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 10
    entry_fill_event = json.loads(lines[2])
    partial_exit_fill_event = json.loads(lines[5])
    final_exit_fill_event = json.loads(lines[8])
    trade_closed_event = json.loads(lines[9])
    assert partial_exit_fill_event["trade_id"] == entry_fill_event["trade_id"]
    assert final_exit_fill_event["trade_id"] == entry_fill_event["trade_id"]
    assert trade_closed_event["event_type"] == "trade_closed"
    assert trade_closed_event["trade_id"] == entry_fill_event["trade_id"]
    assert trade_closed_event["quantity"] == 10.0
    assert trade_closed_event["entry_price"] == 100.0
    assert trade_closed_event["exit_price"] == 108.0
    assert trade_closed_event["gross_pnl"] == 80.0
    assert trade_closed_event["net_pnl"] == 80.0
    assert trade_closed_event["exit_reason"] == "opposite_fill"


@pytest.mark.anyio
async def test_moomoo_multi_open_lots_exit_emits_trade_closed_for_each_lot(client, monkeypatch, tmp_path):
    from unittest.mock import patch

    monkeypatch.setenv("LIVE_EVENTS_PATH", str(tmp_path))

    buy_one_payload = {
        **BASE_PAYLOAD,
        "broker": "moomoo",
        "asset_class": "US",
        "ticker": "US.AAPL",
        "quantity": 5,
        "strategy_id": "ladder_v1",
        "strategy_version": "1.0.0",
        "snapshot_id": "snap_20260329190300123456",
        "signal_id": "sig_ladder_entry_001",
    }
    buy_two_payload = {
        **buy_one_payload,
        "signal_id": "sig_ladder_entry_002",
    }
    sell_payload = {
        **buy_one_payload,
        "action": "sell",
        "quantity": 10,
        "signal_id": "sig_ladder_exit_001",
    }

    side_effect = [
        {"order_id": "51", "ret_code": 0, "fill_id": "fill_buy_101", "filled_qty": 5, "filled_price": 100.0},
        {"order_id": "52", "ret_code": 0, "fill_id": "fill_buy_102", "filled_qty": 5, "filled_price": 102.0},
        {"order_id": "53", "ret_code": 0, "fill_id": "fill_sell_103", "filled_qty": 10, "filled_price": 110.0},
    ]

    with patch("alpha_strike.handlers.moomoo_handler.MoomooHandler.execute", side_effect=side_effect):
        buy_one_response = await client.post("/webhook", json=buy_one_payload)
        buy_two_response = await client.post("/webhook", json=buy_two_payload)
        sell_response = await client.post("/webhook", json=sell_payload)

    assert buy_one_response.status_code == 200
    assert buy_two_response.status_code == 200
    assert sell_response.status_code == 200

    event_files = list(tmp_path.glob("*.moomoo.jsonl"))
    assert len(event_files) == 1
    lines = event_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 12
    first_entry_fill = json.loads(lines[2])
    second_entry_fill = json.loads(lines[5])
    first_exit_fill = json.loads(lines[8])
    first_trade_closed = json.loads(lines[9])
    second_exit_fill = json.loads(lines[10])
    second_trade_closed = json.loads(lines[11])
    assert first_exit_fill["filled_qty"] == 5
    assert second_exit_fill["filled_qty"] == 5
    assert first_exit_fill["trade_id"] == first_entry_fill["trade_id"]
    assert second_exit_fill["trade_id"] == second_entry_fill["trade_id"]
    assert first_trade_closed["trade_id"] == first_entry_fill["trade_id"]
    assert second_trade_closed["trade_id"] == second_entry_fill["trade_id"]
    assert first_trade_closed["quantity"] == 5.0
    assert second_trade_closed["quantity"] == 5.0
    assert first_trade_closed["entry_price"] == 100.0
    assert second_trade_closed["entry_price"] == 102.0
    assert first_trade_closed["exit_price"] == 110.0
    assert second_trade_closed["exit_price"] == 110.0
    assert first_trade_closed["gross_pnl"] == 50.0
    assert second_trade_closed["gross_pnl"] == 40.0
    assert first_trade_closed["exit_reason"] == "opposite_fill"
    assert second_trade_closed["exit_reason"] == "opposite_fill"


@pytest.mark.anyio
async def test_moomoo_reversal_exit_keeps_residual_fill_as_new_trade(client, monkeypatch, tmp_path):
    from unittest.mock import patch

    monkeypatch.setenv("LIVE_EVENTS_PATH", str(tmp_path))

    buy_payload = {
        **BASE_PAYLOAD,
        "broker": "moomoo",
        "asset_class": "US",
        "ticker": "US.AAPL",
        "quantity": 10,
        "strategy_id": "flip_v1",
        "strategy_version": "1.0.0",
        "snapshot_id": "snap_20260329190300123456",
        "signal_id": "sig_flip_entry_001",
    }
    sell_payload = {
        **buy_payload,
        "action": "sell",
        "quantity": 15,
        "signal_id": "sig_flip_exit_001",
    }

    side_effect = [
        {"order_id": "61", "ret_code": 0, "fill_id": "fill_buy_201", "filled_qty": 10, "filled_price": 100.0},
        {"order_id": "62", "ret_code": 0, "fill_id": "fill_sell_202", "filled_qty": 15, "filled_price": 110.0},
    ]

    with patch("alpha_strike.handlers.moomoo_handler.MoomooHandler.execute", side_effect=side_effect):
        buy_response = await client.post("/webhook", json=buy_payload)
        sell_response = await client.post("/webhook", json=sell_payload)

    assert buy_response.status_code == 200
    assert sell_response.status_code == 200

    event_files = list(tmp_path.glob("*.moomoo.jsonl"))
    assert len(event_files) == 1
    lines = event_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 8
    entry_fill = json.loads(lines[2])
    close_fill = json.loads(lines[5])
    trade_closed = json.loads(lines[6])
    assert close_fill["trade_id"] == entry_fill["trade_id"]
    assert close_fill["filled_qty"] == 10
    assert trade_closed["trade_id"] == entry_fill["trade_id"]

    sell_payloads = [
        json.loads(line)
        for line in lines
        if json.loads(line).get("event_type") == "fill_received"
        and json.loads(line).get("action") == "sell"
    ]
    assert len(sell_payloads) == 2
    residual_fill = sell_payloads[1]
    assert residual_fill["filled_qty"] == 5
    assert residual_fill["trade_id"] == "trd_fill_sell_202_reversal"
    assert residual_fill["signal_id"] == "sig_flip_exit_001"


# --- /health/ready エンドポイントのテスト ---

@pytest.mark.anyio
async def test_health_ready_all_ok(client, monkeypatch):
    """/health/ready: 全依存性が正常な場合 HTTP 200 を返す。"""
    from unittest.mock import patch

    monkeypatch.setenv("OANDA_API_KEY", "test-key")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "test-account")

    with patch("alpha_strike.webhook_server.socket.create_connection"):
        response = await client.get("/health/ready")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["checks"]["oanda"]["status"] == "ok"
    assert data["checks"]["moomoo"]["status"] == "ok"


@pytest.mark.anyio
async def test_health_ready_oanda_missing_env(client, monkeypatch):
    """/health/ready: OANDA 環境変数未設定時は degraded (HTTP 503) を返す。"""
    from unittest.mock import patch

    monkeypatch.delenv("OANDA_API_KEY", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)

    with patch("alpha_strike.webhook_server.socket.create_connection"):
        response = await client.get("/health/ready")

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["checks"]["oanda"]["status"] == "error"


@pytest.mark.anyio
async def test_health_ready_moomoo_opend_unreachable(client, monkeypatch):
    """/health/ready: OpenD が起動していない場合は degraded (HTTP 503) を返す。"""
    from unittest.mock import patch

    monkeypatch.setenv("OANDA_API_KEY", "test-key")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "test-account")

    with patch("alpha_strike.webhook_server.socket.create_connection", side_effect=OSError("接続拒否")):
        response = await client.get("/health/ready")

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["checks"]["moomoo"]["status"] == "error"


# ============================================================
# Kill switch / maintenance mode tests (issue #40)
# ============================================================
# 各テストの先頭で slowapi の rate limiter をリセットして、
# 他テストとの相乗りで 429 が出るのを避ける。


@pytest.mark.anyio
async def test_maintenance_mode_env_returns_503(client, monkeypatch):
    """MAINTENANCE_MODE=1 環境変数で /webhook が 503 を返す"""
    from alpha_strike.webhook_server import limiter

    limiter._storage.reset()
    monkeypatch.setenv("MAINTENANCE_MODE", "1")
    response = await client.post("/webhook", json=BASE_PAYLOAD)
    assert response.status_code == 503
    body = response.json()
    assert "maintenance" in body["detail"].lower()


@pytest.mark.anyio
async def test_maintenance_file_returns_503_with_reason(client, monkeypatch, tmp_path):
    """MAINTENANCE_FILE が存在すると 503 を返し、ファイル内容が detail に含まれる"""
    from alpha_strike.webhook_server import limiter

    limiter._storage.reset()
    flag = tmp_path / "MAINTENANCE"
    flag.write_text("emergency stop: ticker XYZ runaway")
    monkeypatch.setenv("MAINTENANCE_FILE", str(flag))

    response = await client.post("/webhook", json=BASE_PAYLOAD)
    assert response.status_code == 503
    assert "emergency stop" in response.json()["detail"]


@pytest.mark.anyio
async def test_maintenance_file_returns_default_reason_when_empty(client, monkeypatch, tmp_path):
    """MAINTENANCE_FILE が空でも 503 を返す（デフォルト理由でフォールバック）"""
    from alpha_strike.webhook_server import limiter

    limiter._storage.reset()
    flag = tmp_path / "MAINTENANCE"
    flag.write_text("")  # 空ファイル
    monkeypatch.setenv("MAINTENANCE_FILE", str(flag))

    response = await client.post("/webhook", json=BASE_PAYLOAD)
    assert response.status_code == 503
    assert response.json()["detail"]  # 何らかの detail が返る


@pytest.mark.anyio
async def test_health_endpoint_unaffected_by_maintenance(client, monkeypatch, tmp_path):
    """maintenance mode 中も /health は 200 を返す（外部ヘルスチェック維持のため）"""
    flag = tmp_path / "MAINTENANCE"
    flag.write_text("maintenance for test")
    monkeypatch.setenv("MAINTENANCE_FILE", str(flag))
    monkeypatch.setenv("MAINTENANCE_MODE", "1")

    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_maintenance_checked_before_passphrase(client, monkeypatch):
    """passphrase 検証より maintenance チェックが先に行われる (誤 passphrase でも 503 が優先)"""
    from alpha_strike.webhook_server import limiter

    limiter._storage.reset()
    monkeypatch.setenv("MAINTENANCE_MODE", "1")
    payload = {**BASE_PAYLOAD, "passphrase": "WRONG-PASSPHRASE"}
    response = await client.post("/webhook", json=payload)
    # maintenance が先にチェックされるので 503 (401 ではない)
    assert response.status_code == 503


@pytest.mark.anyio
async def test_trade_closed_endpoint_also_returns_503_in_maintenance(client, monkeypatch):
    """/events/trade-closed も maintenance mode で 503"""
    from alpha_strike.webhook_server import limiter

    limiter._storage.reset()
    monkeypatch.setenv("MAINTENANCE_MODE", "1")
    response = await client.post("/events/trade-closed", json=TRADE_CLOSED_PAYLOAD)
    assert response.status_code == 503


# ============================================================
# Idempotency tests (issue #41)
# ============================================================
# signal_id を idempotency key として、同一 signal_id の重複到達を
# 拒否する。broker への二重発注を防ぐ最終防御層。


@pytest.mark.anyio
async def test_duplicate_signal_id_not_routed_to_broker(client, monkeypatch):
    """同一 signal_id の 2 回目 POST は broker handler を呼び出さない"""
    from unittest.mock import patch
    from alpha_strike.webhook_server import limiter

    limiter._storage.reset()
    monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
    payload = {
        **BASE_PAYLOAD,
        "broker": "moomoo",
        "asset_class": "US",
        "ticker": "US.AAPL",
        "quantity": 1,
        "signal_id": "sig_dup_test_001",
    }

    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        return_value={"order_id": "100", "ret_code": 0},
    ) as mock_exec:
        first = await client.post("/webhook", json=payload)
        limiter._storage.reset()
        second = await client.post("/webhook", json=payload)

    assert first.status_code == 200
    assert first.json()["status"] == "success"
    # 2 回目は 200 を返す（TradingView 自動リトライ抑止のため 409 ではない）
    assert second.status_code == 200
    # ただし broker handler は 1 回しか呼ばれていない
    assert mock_exec.call_count == 1


@pytest.mark.anyio
async def test_duplicate_signal_id_message_indicates_duplicate(client, monkeypatch):
    """重複時のレスポンス message が duplicate を示す"""
    from unittest.mock import patch
    from alpha_strike.webhook_server import limiter

    limiter._storage.reset()
    monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
    payload = {
        **BASE_PAYLOAD,
        "broker": "moomoo",
        "asset_class": "US",
        "ticker": "US.AAPL",
        "quantity": 1,
        "signal_id": "sig_dup_msg_test",
    }

    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        return_value={"order_id": "200", "ret_code": 0},
    ):
        await client.post("/webhook", json=payload)
        limiter._storage.reset()
        second = await client.post("/webhook", json=payload)

    body = second.json()
    assert body["signal_id"] == "sig_dup_msg_test"
    assert "duplicate" in body["message"].lower()


@pytest.mark.anyio
async def test_payload_without_signal_id_is_not_deduplicated(client, monkeypatch):
    """signal_id 未指定の payload は idempotency 対象外（後方互換）"""
    from unittest.mock import patch
    from alpha_strike.webhook_server import limiter

    limiter._storage.reset()
    monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
    # signal_id を含まない payload
    payload = {
        **BASE_PAYLOAD,
        "broker": "moomoo",
        "asset_class": "US",
        "ticker": "US.AAPL",
        "quantity": 1,
    }
    payload.pop("signal_id", None)

    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        return_value={"order_id": "300", "ret_code": 0},
    ) as mock_exec:
        await client.post("/webhook", json=payload)
        limiter._storage.reset()
        await client.post("/webhook", json=payload)

    # 2 回とも broker を呼ぶ（signal_id ないので idempotency 効かない）
    assert mock_exec.call_count == 2


@pytest.mark.anyio
async def test_distinct_signal_ids_both_routed(client, monkeypatch):
    """異なる signal_id は両方とも broker に流れる"""
    from unittest.mock import patch
    from alpha_strike.webhook_server import limiter

    limiter._storage.reset()
    monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
    base = {
        **BASE_PAYLOAD,
        "broker": "moomoo",
        "asset_class": "US",
        "ticker": "US.AAPL",
        "quantity": 1,
    }

    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        return_value={"order_id": "400", "ret_code": 0},
    ) as mock_exec:
        await client.post("/webhook", json={**base, "signal_id": "sig_A"})
        limiter._storage.reset()
        await client.post("/webhook", json={**base, "signal_id": "sig_B"})

    assert mock_exec.call_count == 2


@pytest.mark.anyio
async def test_idempotency_ttl_env_variable_respected(client, monkeypatch):
    """IDEMPOTENCY_TTL_SECONDS=0 を設定すると TTL 即時切れで毎回受理される"""
    from unittest.mock import patch

    from alpha_strike.services.idempotency import IdempotencyStore
    from alpha_strike.webhook_server import app, limiter

    limiter._storage.reset()
    # 直接 store を差し替え（lifespan を介さずテスト容易性のため）
    app.state.idempotency = IdempotencyStore(ttl_seconds=0.0)

    monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
    payload = {
        **BASE_PAYLOAD,
        "broker": "moomoo",
        "asset_class": "US",
        "ticker": "US.AAPL",
        "quantity": 1,
        "signal_id": "sig_ttl_zero",
    }

    with patch(
        "alpha_strike.handlers.moomoo_handler.MoomooHandler.execute",
        return_value={"order_id": "500", "ret_code": 0},
    ) as mock_exec:
        await client.post("/webhook", json=payload)
        limiter._storage.reset()
        await client.post("/webhook", json=payload)

    # TTL=0 なので 2 回とも broker を呼ぶ
    assert mock_exec.call_count == 2
