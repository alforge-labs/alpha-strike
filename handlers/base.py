"""ブローカーハンドラーの抽象インターフェース"""
from typing import Protocol

from models import WebhookPayload


class BrokerHandler(Protocol):
    """ブローカー注文ハンドラーの共通インターフェース。

    新しいブローカーを追加するには、このProtocolを満たすクラスを
    `handlers/` に追加し `build_default_router()` に登録するだけでよい。
    """

    def execute(self, payload: WebhookPayload) -> dict:
        """注文を実行し、結果dictを返す。

        Raises:
            ImportError: 必要なライブラリが未インストールの場合
            ValueError: 環境変数が不足または不正な場合
            RuntimeError: APIがエラーを返した場合
        """
        ...
