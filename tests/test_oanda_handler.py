"""OANDA ハンドラーのユニットテスト"""

import pytest
import requests
from unittest.mock import MagicMock, patch

from handlers.oanda_handler import _call_oanda_api, _to_oanda_instrument, oanda_order_handler
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


# --- リトライロジックのテスト ---

class TestCallOandaApiRetry:
    """_call_oanda_api のリトライ動作を検証する。"""

    def _make_http_error(self, status_code: int) -> requests.HTTPError:
        """指定ステータスコードの HTTPError を生成する。"""
        mock_response = MagicMock()
        mock_response.status_code = status_code
        err = requests.HTTPError(response=mock_response)
        return err

    def test_5xx_error_retries_up_to_3_times(self):
        """5xx エラーは最大3回リトライし、最終的に例外を送出する。"""
        with patch("handlers.oanda_handler.requests.post") as mock_post, \
             patch("time.sleep"):  # リトライ待機をスキップ
            mock_post.side_effect = self._make_http_error(503)
            with pytest.raises(requests.HTTPError):
                _call_oanda_api("http://example.com", {}, {})
            assert mock_post.call_count == 3

    def test_4xx_error_does_not_retry(self):
        """4xx エラー（クライアントエラー）はリトライせず即座に例外を送出する。"""
        with patch("handlers.oanda_handler.requests.post") as mock_post:
            mock_post.side_effect = self._make_http_error(401)
            with pytest.raises(requests.HTTPError):
                _call_oanda_api("http://example.com", {}, {})
            assert mock_post.call_count == 1

    def test_connection_error_retries_up_to_3_times(self):
        """接続エラーは最大3回リトライし、最終的に例外を送出する。"""
        with patch("handlers.oanda_handler.requests.post") as mock_post, \
             patch("time.sleep"):
            mock_post.side_effect = requests.ConnectionError("接続失敗")
            with pytest.raises(requests.ConnectionError):
                _call_oanda_api("http://example.com", {}, {})
            assert mock_post.call_count == 3

    def test_retries_succeed_on_second_attempt(self):
        """最初の呼び出しが5xxで失敗し、2回目で成功するケース。"""
        mock_fail = MagicMock()
        mock_fail.raise_for_status.side_effect = self._make_http_error(503)

        mock_success = MagicMock()
        mock_success.raise_for_status = MagicMock()
        mock_success.json.return_value = {"orderCreateTransaction": {"id": "99"}}

        with patch("handlers.oanda_handler.requests.post") as mock_post, \
             patch("time.sleep"):
            mock_post.side_effect = [mock_fail, mock_success]
            result = _call_oanda_api("http://example.com", {}, {})

        assert result == {"orderCreateTransaction": {"id": "99"}}
        assert mock_post.call_count == 2
