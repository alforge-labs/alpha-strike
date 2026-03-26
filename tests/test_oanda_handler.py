"""OANDA ハンドラーのユニットテスト"""

import pytest
from unittest.mock import MagicMock, patch

from handlers.oanda_handler import _to_oanda_instrument, oanda_order_handler
from models import WebhookPayload


# --- _to_oanda_instrument のテスト ---

class TestToOandaInstrument:
    def test_fx_6char_converts_to_underscore(self):
        assert _to_oanda_instrument("USDJPY", "FX") == "USD_JPY"

    def test_fx_already_has_underscore_passthrough(self):
        assert _to_oanda_instrument("USD_JPY", "FX") == "USD_JPY"

    def test_commodity_6char_converts(self):
        assert _to_oanda_instrument("XAUUSD", "COMMODITY") == "XAU_USD"

    def test_us_stock_appends_usd(self):
        assert _to_oanda_instrument("AAPL", "US") == "AAPL_USD"

    def test_us_stock_already_has_underscore_passthrough(self):
        assert _to_oanda_instrument("AAPL_USD", "US") == "AAPL_USD"

    def test_index_appends_usd(self):
        assert _to_oanda_instrument("NAS100", "INDEX") == "NAS100_USD"

    def test_unknown_asset_class_passthrough(self):
        assert _to_oanda_instrument("USD_JPY", "RAW") == "USD_JPY"

    def test_fx_non6char_logs_warning_and_passthrough(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            result = _to_oanda_instrument("BTC", "FX")
        assert result == "BTC"
        assert "6文字ではありません" in caplog.text


# --- oanda_order_handler のテスト ---

def _make_payload(**kwargs) -> WebhookPayload:
    defaults = {
        "passphrase": "test-pass",
        "broker": "oanda",
        "asset_class": "FX",
        "action": "buy",
        "ticker": "USDJPY",
        "quantity": 1000.0,
    }
    return WebhookPayload(**(defaults | kwargs))


class TestOandaOrderHandler:
    def test_missing_api_key_raises_value_error(self, monkeypatch):
        monkeypatch.delenv("OANDA_API_KEY", raising=False)
        monkeypatch.setenv("OANDA_ACCOUNT_ID", "123")
        with pytest.raises(ValueError, match="OANDA_API_KEY"):
            oanda_order_handler(_make_payload())

    def test_missing_account_id_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("OANDA_API_KEY", "key")
        monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
        with pytest.raises(ValueError, match="OANDA_ACCOUNT_ID"):
            oanda_order_handler(_make_payload())

    def test_invalid_env_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("OANDA_API_KEY", "key")
        monkeypatch.setenv("OANDA_ACCOUNT_ID", "123")
        monkeypatch.setenv("OANDA_ENV", "INVALID")
        with pytest.raises(ValueError, match="PRACTICE または LIVE"):
            oanda_order_handler(_make_payload())

    def test_buy_order_success(self, monkeypatch):
        monkeypatch.setenv("OANDA_API_KEY", "key")
        monkeypatch.setenv("OANDA_ACCOUNT_ID", "123-456")
        monkeypatch.setenv("OANDA_ENV", "PRACTICE")

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "orderCreateTransaction": {"id": "42"}
        }
        mock_response.raise_for_status = MagicMock()

        with patch("handlers.oanda_handler.requests.post", return_value=mock_response) as mock_post:
            result = oanda_order_handler(_make_payload(action="buy", quantity=1000.0))

        assert result["order_id"] == "42"
        assert result["instrument"] == "USD_JPY"
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["json"]["order"]["units"] == "1000.0"

    def test_sell_order_uses_negative_units(self, monkeypatch):
        monkeypatch.setenv("OANDA_API_KEY", "key")
        monkeypatch.setenv("OANDA_ACCOUNT_ID", "123-456")
        monkeypatch.setenv("OANDA_ENV", "PRACTICE")

        mock_response = MagicMock()
        mock_response.json.return_value = {"orderCreateTransaction": {"id": "99"}}
        mock_response.raise_for_status = MagicMock()

        with patch("handlers.oanda_handler.requests.post", return_value=mock_response) as mock_post:
            oanda_order_handler(_make_payload(action="sell", quantity=500.0))

        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["json"]["order"]["units"] == "-500.0"

    def test_uses_practice_url(self, monkeypatch):
        monkeypatch.setenv("OANDA_API_KEY", "key")
        monkeypatch.setenv("OANDA_ACCOUNT_ID", "acc")
        monkeypatch.setenv("OANDA_ENV", "PRACTICE")

        mock_response = MagicMock()
        mock_response.json.return_value = {"orderCreateTransaction": {"id": "1"}}
        mock_response.raise_for_status = MagicMock()

        with patch("handlers.oanda_handler.requests.post", return_value=mock_response) as mock_post:
            oanda_order_handler(_make_payload())

        url = mock_post.call_args.args[0]
        assert "fxpractice" in url
