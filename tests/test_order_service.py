"""OrderRouter の単体テスト"""
from unittest.mock import MagicMock

import pytest

from alpha_strike.models import WebhookPayload
from alpha_strike.services.order_service import OrderRouter, build_default_router


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


class TestOrderRouter:
    def test_route_dispatches_to_correct_handler(self):
        mock_handler = MagicMock()
        mock_handler.execute.return_value = {"order_id": "123"}
        router = OrderRouter({"oanda": mock_handler})
        payload = _make_payload(broker="oanda")

        result = router.route(payload)

        mock_handler.execute.assert_called_once_with(payload)
        assert result == {"order_id": "123"}

    def test_route_raises_for_unknown_broker(self):
        router = OrderRouter({})
        mock_payload = MagicMock()
        mock_payload.broker = "unknown_broker"
        with pytest.raises(ValueError, match="未対応ブローカー"):
            router.route(mock_payload)

    def test_route_moomoo_dispatches_to_moomoo_handler(self):
        mock_oanda = MagicMock()
        mock_moomoo = MagicMock()
        mock_moomoo.execute.return_value = {"order_id": "456"}
        router = OrderRouter({"oanda": mock_oanda, "moomoo": mock_moomoo})
        payload = _make_payload(broker="moomoo")

        result = router.route(payload)

        mock_moomoo.execute.assert_called_once_with(payload)
        mock_oanda.execute.assert_not_called()
        assert result == {"order_id": "456"}

    def test_build_default_router_returns_router_with_oanda_and_moomoo(self):
        router = build_default_router()
        assert "oanda" in router._handlers
        assert "moomoo" in router._handlers
