"""ntfy.sh プッシュ通知ヘルパー (issue #57 Phase 2)。

`NTFY_TOPIC` 未設定 or 空のときは no-op で `False` を返す（デフォルト無効）。
通信は標準ライブラリ `urllib` で完結し、外部依存を増やさない。通知失敗は握りつぶして
`False` を返す（通知は副次機能であり、発注フローを壊さない）。
"""

from __future__ import annotations

import logging
import os
import urllib.request
from collections.abc import Sequence
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_NTFY_SERVER = "https://ntfy.sh"
DEFAULT_TIMEOUT_SEC = 10.0

# urlopen 差し替え用フック（テスト用）。
UrlOpener = Callable[..., object]


class NtfyNotifier:
    """ntfy.sh への push 通知。`NTFY_TOPIC` / `NTFY_SERVER` 環境変数で設定する。"""

    def __init__(self, topic: str | None = None, server: str | None = None) -> None:
        self.topic = topic if topic is not None else os.getenv("NTFY_TOPIC", "")
        self.server = (
            server if server is not None else os.getenv("NTFY_SERVER", DEFAULT_NTFY_SERVER)
        ).rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def notify(
        self,
        title: str,
        message: str,
        *,
        tags: Sequence[str] = (),
        priority: str | None = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        opener: UrlOpener | None = None,
    ) -> bool:
        """ntfy に通知を送る。無効・失敗時は False を返す（例外を投げない）。"""
        if not self.enabled:
            return False
        url = f"{self.server}/{self.topic}"
        headers = {"Title": title}
        if tags:
            headers["Tags"] = ",".join(tags)
        if priority:
            headers["Priority"] = priority
        req = urllib.request.Request(
            url, data=message.encode("utf-8"), headers=headers, method="POST"
        )
        _open = opener or urllib.request.urlopen
        try:
            with _open(req, timeout=timeout_sec):  # type: ignore[call-arg]
                return True
        except Exception as exc:  # noqa: BLE001 - 通知失敗は発注フローを壊さない
            logger.warning("ntfy 通知に失敗しました: %s", exc)
            return False
