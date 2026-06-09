"""moomoo ハンドラーのユニットテスト"""

import socket
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import futu
from alpha_strike.handlers.moomoo_handler import _get_trade_context, MoomooHandler
from alpha_strike.models import WebhookPayload


def _make_payload(**kwargs) -> WebhookPayload:
    defaults = {
        "passphrase": "test",
        "broker": "moomoo",
        "asset_class": "US",
        "action": "buy",
        "ticker": "US.AAPL",
        "quantity": 10.0,
    }
    return WebhookPayload(**(defaults | kwargs))


def _mock_ctx(
    ret_code=None, order_id="12345", dealt_qty: float | None = None, dealt_avg_price: float | None = None
) -> MagicMock:
    """place_order が成功を返す mock コンテキストを生成する。"""
    if ret_code is None:
        ret_code = futu.RET_OK
    data = {"order_id": [order_id]}
    if dealt_qty is not None:
        data["dealt_qty"] = [dealt_qty]
    if dealt_avg_price is not None:
        data["dealt_avg_price"] = [dealt_avg_price]
    mock_data = pd.DataFrame(data)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.place_order.return_value = (ret_code, mock_data)
    return ctx


# --- _get_trade_context のテスト ---
# 10.5.6508 以降は OpenUSTradeContext / OpenHKTradeContext が廃止され
# OpenSecTradeContext + filter_trdmarket=TrdMarket.<MARKET> に統一された。

class TestGetTradeContext:
    def test_hk_returns_sec_context_with_hk_market(self):
        with patch("futu.OpenSecTradeContext") as mock_sec:
            _get_trade_context("HK", "127.0.0.1", 11111)
            mock_sec.assert_called_once_with(
                filter_trdmarket=futu.TrdMarket.HK,
                host="127.0.0.1",
                port=11111,
                security_firm=futu.SecurityFirm.NONE,
            )

    def test_us_returns_sec_context_with_us_market(self):
        with patch("futu.OpenSecTradeContext") as mock_sec:
            _get_trade_context("US", "127.0.0.1", 11111)
            mock_sec.assert_called_once_with(
                filter_trdmarket=futu.TrdMarket.US,
                host="127.0.0.1",
                port=11111,
                security_firm=futu.SecurityFirm.NONE,
            )

    def test_index_falls_back_to_us_market(self):
        """INDEX (NAS100 等) は US 同等の Context を使う既存挙動を維持する。"""
        with patch("futu.OpenSecTradeContext") as mock_sec:
            _get_trade_context("INDEX", "127.0.0.1", 11111)
            mock_sec.assert_called_once_with(
                filter_trdmarket=futu.TrdMarket.US,
                host="127.0.0.1",
                port=11111,
                security_firm=futu.SecurityFirm.NONE,
            )

    def test_hk_case_insensitive(self):
        with patch("futu.OpenSecTradeContext") as mock_sec:
            _get_trade_context("hk", "127.0.0.1", 11111)
            mock_sec.assert_called_once()
            assert mock_sec.call_args.kwargs["filter_trdmarket"] == futu.TrdMarket.HK

    def test_crypto_returns_sec_context_with_crypto_market(self):
        """CRYPTO は filter_trdmarket=TrdMarket.CRYPTO で OpenSecTradeContext を生成。"""
        with patch("futu.OpenSecTradeContext") as mock_sec:
            _get_trade_context("CRYPTO", "127.0.0.1", 11111)
            mock_sec.assert_called_once_with(
                filter_trdmarket=futu.TrdMarket.CRYPTO,
                host="127.0.0.1",
                port=11111,
                security_firm=futu.SecurityFirm.NONE,
            )

    def test_crypto_case_insensitive(self):
        with patch("futu.OpenSecTradeContext") as mock_sec:
            _get_trade_context("crypto", "127.0.0.1", 11111)
            mock_sec.assert_called_once()
            assert mock_sec.call_args.kwargs["filter_trdmarket"] == futu.TrdMarket.CRYPTO


# --- MoomooHandler のテスト ---

class TestMoomooOrderHandler:
    def test_futu_not_available_raises_import_error(self):
        with patch("alpha_strike.handlers.moomoo_handler.FUTU_AVAILABLE", False):
            with pytest.raises(ImportError, match="futu-api"):
                MoomooHandler().execute(_make_payload())

    def test_invalid_trd_env_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_TRD_ENV", "INVALID")
        with pytest.raises(ValueError, match="SIMULATE または REAL"):
            MoomooHandler().execute(_make_payload())

    def test_crypto_with_simulate_raises_value_error_before_connect(self, monkeypatch):
        """CRYPTO + SIMULATE は moomoo API が live only のため、OpenD 接続前に ValueError で早期拒否する。

        実機検証 (2026-05-18 alpha-crypto): SIMULATE + CC.BTC で moomoo SDK が
        ret_code=-1 "the type of environment param is wrong" を返す → cryptic な 502 になる。
        この早期チェックで、ユーザーに ETF 代替 (US.IBIT 等) または REAL 利用を促す。
        """
        monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
        # socket.create_connection が呼ばれたら validation より後ろまで進んだことになる
        with patch("socket.create_connection") as mock_conn:
            with pytest.raises(ValueError, match="crypto"):
                MoomooHandler().execute(
                    _make_payload(asset_class="CRYPTO", ticker="CC.BTC", quantity=0.01)
                )
            mock_conn.assert_not_called()

    def test_crypto_with_simulate_error_suggests_etf_alternative(self, monkeypatch):
        """エラーメッセージに ETF 代替案 (US.IBIT) と REAL 利用案が含まれることを保証する。"""
        monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
        with patch("socket.create_connection"):
            with pytest.raises(ValueError) as exc_info:
                MoomooHandler().execute(_make_payload(asset_class="CRYPTO", ticker="CC.BTC"))
        msg = str(exc_info.value)
        assert "live only" in msg or "SIMULATE" in msg
        assert "IBIT" in msg or "ETF" in msg
        assert "REAL" in msg

    def test_crypto_case_insensitive_simulate_guard(self, monkeypatch):
        """asset_class が小文字 'crypto' でも guard が効く。"""
        monkeypatch.setenv("MOOMOO_TRD_ENV", "simulate")
        with patch("socket.create_connection"):
            with pytest.raises(ValueError, match="crypto"):
                MoomooHandler().execute(_make_payload(asset_class="crypto", ticker="CC.BTC"))

    def test_opend_not_running_raises_runtime_error(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_HOST", "127.0.0.1")
        monkeypatch.setenv("MOOMOO_PORT", "11111")
        monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
        with patch("socket.create_connection", side_effect=OSError("Connection refused")):
            with pytest.raises(RuntimeError, match="OpenD"):
                MoomooHandler().execute(_make_payload())

    def test_socket_timeout_raises_runtime_error(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
        with patch("socket.create_connection", side_effect=socket.timeout("timed out")):
            with pytest.raises(RuntimeError, match="OpenD"):
                MoomooHandler().execute(_make_payload())

    def test_successful_buy_order(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
        ctx = _mock_ctx(order_id="99", dealt_qty=10, dealt_avg_price=188.5)
        with patch("socket.create_connection"):
            with patch("alpha_strike.handlers.moomoo_handler._get_trade_context", return_value=ctx):
                result = MoomooHandler().execute(_make_payload(action="buy"))
        assert result["order_id"] == "99"
        assert result["ret_code"] == futu.RET_OK
        assert result["filled_qty"] == 10.0
        assert result["filled_price"] == 188.5

    def test_successful_sell_order_uses_sell_side(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
        ctx = _mock_ctx()
        with patch("socket.create_connection"):
            with patch("alpha_strike.handlers.moomoo_handler._get_trade_context", return_value=ctx):
                MoomooHandler().execute(_make_payload(action="sell"))
        call_kwargs = ctx.place_order.call_args.kwargs
        assert call_kwargs["trd_side"] == futu.TrdSide.SELL

    def test_failed_order_raises_runtime_error(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.place_order.return_value = (-1, "APIエラー")
        with patch("socket.create_connection"):
            with patch("alpha_strike.handlers.moomoo_handler._get_trade_context", return_value=ctx):
                with pytest.raises(RuntimeError, match="moomoo注文エラー"):
                    MoomooHandler().execute(_make_payload())

    def test_hk_asset_class_uses_hk_market(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
        ctx = _mock_ctx()
        with patch("socket.create_connection"):
            with patch("futu.OpenSecTradeContext", return_value=ctx) as mock_sec:
                MoomooHandler().execute(_make_payload(asset_class="HK", ticker="HK.00700"))
            assert mock_sec.call_args.kwargs["filter_trdmarket"] == futu.TrdMarket.HK

    def test_crypto_asset_class_uses_crypto_market(self, monkeypatch):
        """CRYPTO + CC.BTC を REAL 環境で payload に渡すと filter_trdmarket=CRYPTO で発注される。

        moomoo crypto は live only のため SIMULATE は早期検出で拒否される。
        正常系は REAL 環境でのみ成立する。
        """
        monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
        ctx = _mock_ctx(order_id="crypto-1", dealt_qty=0.01, dealt_avg_price=68000.0)
        with patch("socket.create_connection"):
            with patch("futu.OpenSecTradeContext", return_value=ctx) as mock_sec:
                result = MoomooHandler().execute(
                    _make_payload(asset_class="CRYPTO", ticker="CC.BTC", quantity=0.01)
                )
            assert mock_sec.call_args.kwargs["filter_trdmarket"] == futu.TrdMarket.CRYPTO
            assert mock_sec.call_args.kwargs["security_firm"] == futu.SecurityFirm.NONE
        call_kwargs = ctx.place_order.call_args.kwargs
        assert call_kwargs["code"] == "CC.BTC"
        assert call_kwargs["qty"] == 0.01
        assert result["order_id"] == "crypto-1"

    def test_real_env_uses_real_trd_env(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
        ctx = _mock_ctx()
        with patch("socket.create_connection"):
            with patch("alpha_strike.handlers.moomoo_handler._get_trade_context", return_value=ctx):
                MoomooHandler().execute(_make_payload())
        call_kwargs = ctx.place_order.call_args.kwargs
        assert call_kwargs["trd_env"] == futu.TrdEnv.REAL

    def test_order_id_fallback_when_column_missing(self, monkeypatch):
        monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
        # order_id カラムが存在しない DataFrame を返す
        mock_data = pd.DataFrame({"other_col": ["value"]})
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.place_order.return_value = (futu.RET_OK, mock_data)
        with patch("socket.create_connection"):
            with patch("alpha_strike.handlers.moomoo_handler._get_trade_context", return_value=ctx):
                result = MoomooHandler().execute(_make_payload())
        # フォールバックとして文字列全体が返される
        assert "order_id" in result


# --- time_in_force のテスト (#76 / moomoo 10.7 paper 制約) ---

class TestTimeInForce:
    """time_in_force の解決ロジック (#76 / moomoo 10.7)。

    TradingView の日足アラート（Once Per Bar Close）はバー確定直後
    = 市場クローズ後に webhook を送るため、DAY 成行注文は約定機会がなく
    翌営業日にも持ち越されずに CANCELLED_ALL で失効する（実運用で確認済み）。
    REAL の米国株は GTC で発注し、翌営業日寄付での約定を保証する。

    ただし moomoo 10.7 はペーパートレード（SIMULATE）での GTC を発注時点で
    拒否する（place_order が "Paper trading does not support GTC orders" を返す）。
    そのため SIMULATE では市場・env に関わらず DAY を強制し 502 を防ぐ。
    GTC carry-over は REAL でのみ有効になる。
    """

    def _execute_and_get_tif(self, **payload_kwargs):
        ctx = _mock_ctx()
        with patch("socket.create_connection"):
            with patch("alpha_strike.handlers.moomoo_handler._get_trade_context", return_value=ctx):
                MoomooHandler().execute(_make_payload(**payload_kwargs))
        return ctx.place_order.call_args.kwargs["time_in_force"]

    def test_real_us_market_order_uses_gtc_by_default(self, monkeypatch):
        """REAL の米国株はデフォルト GTC。クローズ後注文を翌営業日寄付に持ち越すため。"""
        monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
        monkeypatch.delenv("MOOMOO_TIME_IN_FORCE", raising=False)
        assert self._execute_and_get_tif() == futu.TimeInForce.GTC

    def test_real_index_market_order_uses_gtc(self, monkeypatch):
        """INDEX は US 市場扱い（_MARKET_MAP フォールバック）なので REAL では GTC。"""
        monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
        monkeypatch.delenv("MOOMOO_TIME_IN_FORCE", raising=False)
        tif = self._execute_and_get_tif(asset_class="INDEX", ticker="US.QQQ")
        assert tif == futu.TimeInForce.GTC

    def test_simulate_us_market_order_uses_day(self, monkeypatch):
        """SIMULATE は moomoo 10.7 が GTC を拒否するため US でも DAY を強制する。

        旧挙動（SIMULATE US → GTC）のまま 10.7 へ昇格すると、クローズ後注文が
        "Paper trading does not support GTC orders" で 502 になる。これを防ぐ。
        """
        monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
        monkeypatch.delenv("MOOMOO_TIME_IN_FORCE", raising=False)
        assert self._execute_and_get_tif() == futu.TimeInForce.DAY

    def test_simulate_ignores_gtc_env_and_uses_day(self, monkeypatch):
        """SIMULATE では MOOMOO_TIME_IN_FORCE=GTC を明示しても DAY に落とす（10.7 制約）。"""
        monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
        monkeypatch.setenv("MOOMOO_TIME_IN_FORCE", "GTC")
        assert self._execute_and_get_tif() == futu.TimeInForce.DAY

    def test_hk_market_order_uses_day(self, monkeypatch):
        """香港市場の成行注文は moomoo 仕様で当日有効のみ → REAL でも DAY を維持。"""
        monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
        monkeypatch.delenv("MOOMOO_TIME_IN_FORCE", raising=False)
        tif = self._execute_and_get_tif(asset_class="HK", ticker="HK.00700")
        assert tif == futu.TimeInForce.DAY

    def test_crypto_market_order_uses_day(self, monkeypatch):
        """CRYPTO は 24/365 取引でクローズ後問題が存在しないため DAY を維持。"""
        monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")  # crypto は live only
        monkeypatch.delenv("MOOMOO_TIME_IN_FORCE", raising=False)
        tif = self._execute_and_get_tif(
            asset_class="CRYPTO", ticker="CC.BTC", quantity=0.01
        )
        assert tif == futu.TimeInForce.DAY

    def test_env_override_day_restores_legacy_behavior(self, monkeypatch):
        """REAL US + MOOMOO_TIME_IN_FORCE=DAY で旧挙動にロールバックできる（運用安全弁）。"""
        monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
        monkeypatch.setenv("MOOMOO_TIME_IN_FORCE", "DAY")
        assert self._execute_and_get_tif() == futu.TimeInForce.DAY

    def test_env_override_is_case_insensitive(self, monkeypatch):
        """小文字 gtc も .upper() 正規化で GTC として解釈される（REAL US で検証）。"""
        monkeypatch.setenv("MOOMOO_TRD_ENV", "REAL")
        monkeypatch.setenv("MOOMOO_TIME_IN_FORCE", "gtc")
        assert self._execute_and_get_tif() == futu.TimeInForce.GTC

    def test_env_invalid_value_raises_value_error(self, monkeypatch):
        """不正値は黙って DAY に落とさず fail-loud（SIMULATE でも parse 先行で検証）。"""
        monkeypatch.setenv("MOOMOO_TRD_ENV", "SIMULATE")
        monkeypatch.setenv("MOOMOO_TIME_IN_FORCE", "INVALID")
        ctx = _mock_ctx()
        with patch("socket.create_connection"):
            with patch("alpha_strike.handlers.moomoo_handler._get_trade_context", return_value=ctx):
                with pytest.raises(ValueError, match="MOOMOO_TIME_IN_FORCE"):
                    MoomooHandler().execute(_make_payload())
