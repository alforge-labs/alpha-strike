"""Idempotency store (issue #41, #126)

TradingView の Webhook は再送・alert 再評価で同一シグナルが
複数回到達することがある。alpha-strike 側で broker に流す前に
重複検知して拒否するための in-memory ストア。

設計上のポイント:

- **外部依存なし**: dict + threading.Lock + 時刻による TTL eviction だけで実装
  （E2.1.Micro 1GB RAM で動かすので Redis 等は持ち込まない）
- **TTL 既定 10 分**: TradingView 自動リトライ最長間隔をカバーする
- **スレッドセーフ**: FastAPI + uvicorn の async ハンドラ間で安全
- **永続化しない**: restart 時には空に戻る（restart は稀なので許容）
- **空 ``signal_id`` は不正**: 呼び出し側で必ず非空文字を渡す前提
- **判定単位は銘柄・売買方向まで含む** (#126): ``signal_id`` は bar 単位で払い出される
  ため、同一バーの銘柄別シグナルが同じ値を共有する。``signal_id`` 単独をキーにすると
  2 銘柄目以降を重複として捨ててしまう
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class IdempotencyKey:
    """重複判定の単位となる複合キー (#126)。

    TradingView は同一バーの銘柄別アラートに同じ ``signal_id`` を付けて送信するため、
    ``signal_id`` 単独では別銘柄・別方向のシグナルまで重複と誤判定する。銘柄と売買方向
    まで含めて 1 つのシグナルとして扱う。

    Attributes:
        signal_id: シグナル ID（bar 単位で払い出される。非空必須）。
        broker: ブローカー名。
        ticker: 銘柄コード。
        action: 売買方向（buy / sell）。
    """

    signal_id: str
    broker: str = ""
    ticker: str = ""
    action: str = ""


class IdempotencyStore:
    """`IdempotencyKey` → 受信時刻 (monotonic seconds) のマップ。

    Args:
        ttl_seconds: 重複拒否対象とする保持期間（秒）。既定 600 秒 (10 分)。
    """

    def __init__(self, ttl_seconds: float = 600) -> None:
        self.ttl_seconds = ttl_seconds
        self._data: dict[IdempotencyKey, float] = {}
        self._lock = Lock()

    def check_and_record(self, key: IdempotencyKey) -> bool:
        """``key`` を記録する。新規なら True、既存（重複）なら False。

        既存判定は TTL 内のエントリのみが対象。TTL を超えたエントリは
        本メソッド呼び出し時に併せて eviction される。

        Raises:
            ValueError: ``key.signal_id`` が空文字列のとき。
        """
        if not key.signal_id:
            raise ValueError("signal_id は非空文字列が必要です")

        now = time.monotonic()
        with self._lock:
            self._evict_expired(now)
            if key in self._data:
                return False
            self._data[key] = now
            return True

    def forget(self, key: IdempotencyKey) -> None:
        """記録済みの ``key`` を破棄する。

        carry-over 再発注 (#89) が ``route()`` 失敗時に呼ぶ。失敗した試行は注文を
        出していないため、次スイープで即リトライできるよう冪等キーを解放する
        （TTL 満了まで 30 分リトライが止まるのを防ぐ）。存在しないキーは無視。
        """
        with self._lock:
            self._data.pop(key, None)

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
