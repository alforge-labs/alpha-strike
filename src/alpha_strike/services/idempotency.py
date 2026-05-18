"""Idempotency store (issue #41)

TradingView の Webhook は再送・alert 再評価で同一 ``signal_id`` が
複数回到達することがある。alpha-strike 側で broker に流す前に
重複検知して拒否するための in-memory ストア。

設計上のポイント:

- **外部依存なし**: dict + threading.Lock + 時刻による TTL eviction だけで実装
  （E2.1.Micro 1GB RAM で動かすので Redis 等は持ち込まない）
- **TTL 既定 10 分**: TradingView 自動リトライ最長間隔をカバーする
- **スレッドセーフ**: FastAPI + uvicorn の async ハンドラ間で安全
- **永続化しない**: restart 時には空に戻る（restart は稀なので許容）
- **空 ``signal_id`` は不正**: 呼び出し側で必ず非空文字を渡す前提
"""

from __future__ import annotations

import time
from threading import Lock


class IdempotencyStore:
    """signal_id → 受信時刻 (monotonic seconds) のマップ。

    Args:
        ttl_seconds: 重複拒否対象とする保持期間（秒）。既定 600 秒 (10 分)。
    """

    def __init__(self, ttl_seconds: float = 600) -> None:
        self.ttl_seconds = ttl_seconds
        self._data: dict[str, float] = {}
        self._lock = Lock()

    def check_and_record(self, signal_id: str) -> bool:
        """``signal_id`` を記録する。新規なら True、既存（重複）なら False。

        既存判定は TTL 内のエントリのみが対象。TTL を超えたエントリは
        本メソッド呼び出し時に併せて eviction される。

        Raises:
            ValueError: ``signal_id`` が空文字列のとき。
        """
        if not signal_id:
            raise ValueError("signal_id は非空文字列が必要です")

        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            if signal_id in self._data:
                return False
            self._data[signal_id] = now
            return True

    def _evict_expired(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        # 全件スキャン: TTL を超えたものを削除。
        # 通常運用では数十〜数百件程度なので問題なし。
        expired = [k for k, v in self._data.items() if v < cutoff]
        for k in expired:
            del self._data[k]

    def __len__(self) -> int:
        """テスト用: 現在保持しているエントリ数。"""
        with self._lock:
            return len(self._data)
