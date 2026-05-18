"""ブローカーハンドラーの抽象インターフェース"""
from typing import Protocol

from alpha_strike.models import WebhookPayload


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
        # Protocol method body — concrete implementations must override this.
        # `pass` を使うことで CodeQL の py/ineffectual-statement (Ellipsis 検出)
        # を回避しつつ、Protocol としての契約は変わらない。
        raise NotImplementedError  # pragma: no cover
