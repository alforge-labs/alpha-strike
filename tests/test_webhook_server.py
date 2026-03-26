"""Webhook サーバーのインテグレーションテスト"""

import pytest
from httpx import ASGITransport, AsyncClient

from webhook_server import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("WEBHOOK_PASSPHRASE", "test-secret")
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

    with patch("handlers.oanda_handler.requests.post", return_value=mock_response):
        response = await client.post("/webhook", json=BASE_PAYLOAD)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["broker"] == "oanda"


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

    with patch("handlers.oanda_handler.requests.post", side_effect=req_lib.ConnectionError("internal host detail")):
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
    with patch("handlers.moomoo_handler.moomoo_order_handler", return_value={"order_id": "42", "ret_code": 0}) as mock_handler:
        # moomoo_order_handler を直接パッチ
        with patch("webhook_server.moomoo_order_handler", return_value={"order_id": "42", "ret_code": 0}):
            response = await client.post("/webhook", json=moomoo_payload)

    assert response.status_code == 200
    data = response.json()
    assert data["broker"] == "moomoo"
