"""FillEventService の単体テスト"""
from datetime import datetime
from unittest.mock import MagicMock

from models import FillEvent, WebhookPayload
from services.fill_service import FillEventService


def _make_payload(**kwargs) -> WebhookPayload:
    defaults = {
        "passphrase": "test",
        "broker": "oanda",
        "asset_class": "FX",
        "action": "buy",
        "ticker": "USDJPY",
        "quantity": 1000.0,
    }
    return WebhookPayload(**(defaults | kwargs))


def _make_fill_event(**kwargs) -> FillEvent:
    defaults = {
        "event_id": "evt_001",
        "signal_id": "sig_001",
        "order_id": "ord_001",
        "fill_id": "fill_001",
        "occurred_at": datetime(2026, 1, 1, 9, 0),
        "broker": "oanda",
        "asset_class": "FX",
        "action": "buy",
        "ticker": "USDJPY",
        "quantity": 1000.0,
        "filled_qty": 1000.0,
        "filled_price": 150.0,
        "trade_id": "trd_001",
        "run_mode": "live",
    }
    return FillEvent(**(defaults | kwargs))


class TestFillEventServiceBuild:
    def test_build_returns_none_when_filled_qty_missing(self):
        mock_logger = MagicMock()
        svc = FillEventService(mock_logger)
        payload = _make_payload()
        result = svc.build(
            payload=payload,
            result={},
            signal_id="sig_001",
            internal_order_id="ord_001",
            broker_order_id=None,
        )
        assert result is None

    def test_build_returns_fill_event_when_filled_qty_and_price_present(self):
        mock_logger = MagicMock()
        svc = FillEventService(mock_logger)
        payload = _make_payload()
        result = svc.build(
            payload=payload,
            result={"filled_qty": 1000.0, "filled_price": 150.0},
            signal_id="sig_001",
            internal_order_id="ord_001",
            broker_order_id="brk_001",
        )
        assert result is not None
        assert result.filled_qty == 1000.0
        assert result.filled_price == 150.0
        assert result.broker_order_id == "brk_001"


class TestFillEventServiceAllocate:
    def test_allocate_returns_original_when_no_recent_fills(self):
        mock_logger = MagicMock()
        mock_logger.load_events.return_value = []
        svc = FillEventService(mock_logger)
        fill = _make_fill_event(action="sell")

        result = svc.allocate(fill)

        assert len(result) == 1
        assert result[0] is fill

    def test_allocate_returns_original_for_unknown_broker(self):
        mock_logger = MagicMock()
        svc = FillEventService(mock_logger)
        # バリデーション外のブローカーを model_construct で直接構築
        fill = FillEvent.model_construct(**{**_make_fill_event().__dict__, "broker": "ig"})

        result = svc.allocate(fill)

        assert len(result) == 1
        assert result[0] is fill
        mock_logger.load_events.assert_not_called()


class TestFillEventServiceBuildTradeClosed:
    def test_returns_none_when_trade_id_is_none(self):
        mock_logger = MagicMock()
        svc = FillEventService(mock_logger)
        fill = _make_fill_event(trade_id=None)

        result = svc.build_trade_closed(fill)

        assert result is None

    def test_returns_none_when_broker_not_supported(self):
        mock_logger = MagicMock()
        svc = FillEventService(mock_logger)
        fill = FillEvent.model_construct(**{**_make_fill_event().__dict__, "broker": "ig"})

        result = svc.build_trade_closed(fill)

        assert result is None
