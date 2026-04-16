"""注文ルーティングサービス"""
from handlers.base import BrokerHandler
from handlers.oanda_handler import OandaHandler
from handlers.moomoo_handler import MoomooHandler
from models import WebhookPayload


class OrderRouter:
    """ブローカー名を元に適切なハンドラーへ注文をルーティングする。

    新しいブローカーを追加するには:
    1. `handlers/` に新しい `XxxHandler` クラスを作成する
    2. `build_default_router()` に登録する
    3. `webhook_server.py` は変更不要
    """

    def __init__(self, handlers: dict[str, BrokerHandler]) -> None:
        self._handlers = handlers

    def route(self, payload: WebhookPayload) -> dict:
        """payloadのブローカーに対応するハンドラーへ委譲する。

        Raises:
            ValueError: 未登録のブローカーの場合
        """
        handler = self._handlers.get(payload.broker)
        if handler is None:
            raise ValueError(f"未対応ブローカー: {payload.broker}")
        return handler.execute(payload)


def build_default_router() -> OrderRouter:
    """デフォルトのブローカーハンドラーを登録したルーターを返す。"""
    return OrderRouter({
        "oanda": OandaHandler(),
        "moomoo": MoomooHandler(),
    })
