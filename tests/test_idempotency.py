"""IdempotencyStore のユニットテスト (issue #41, #126)"""

from __future__ import annotations

import threading
import time

import pytest

from alpha_strike.services.idempotency import IdempotencyKey, IdempotencyStore


def _key(signal_id: str, ticker: str = "US.TQQQ", action: str = "buy") -> IdempotencyKey:
    return IdempotencyKey(
        signal_id=signal_id, broker="moomoo", ticker=ticker, action=action
    )


class TestIdempotencyStore:
    def test_first_record_returns_true(self):
        store = IdempotencyStore(ttl_seconds=60)
        assert store.check_and_record(_key("sig_001")) is True

    def test_duplicate_record_returns_false(self):
        store = IdempotencyStore(ttl_seconds=60)
        store.check_and_record(_key("sig_001"))
        assert store.check_and_record(_key("sig_001")) is False

    def test_distinct_signals_both_accepted(self):
        store = IdempotencyStore(ttl_seconds=60)
        assert store.check_and_record(_key("sig_001")) is True
        assert store.check_and_record(_key("sig_002")) is True
        assert store.check_and_record(_key("sig_003")) is True

    def test_ttl_expiration_allows_reuse(self):
        store = IdempotencyStore(ttl_seconds=0.05)
        assert store.check_and_record(_key("sig_001")) is True
        # TTL 切れまで待つ
        time.sleep(0.1)
        # 同じキーを再受理できる
        assert store.check_and_record(_key("sig_001")) is True

    def test_within_ttl_still_rejected(self):
        store = IdempotencyStore(ttl_seconds=10)
        store.check_and_record(_key("sig_001"))
        # まだ TTL 内
        time.sleep(0.05)
        assert store.check_and_record(_key("sig_001")) is False

    def test_empty_signal_id_is_rejected_as_invalid(self):
        """空文字列の signal_id は idempotency 対象外（記録もしない）。"""
        store = IdempotencyStore(ttl_seconds=60)
        with pytest.raises(ValueError, match="signal_id"):
            store.check_and_record(_key(""))

    def test_eviction_keeps_only_unexpired(self):
        store = IdempotencyStore(ttl_seconds=0.05)
        store.check_and_record(_key("old_sig"))
        time.sleep(0.1)
        # eviction を内部的にトリガーする新規記録
        store.check_and_record(_key("new_sig"))
        # old_sig は再受理可能 (eviction 済み)
        assert store.check_and_record(_key("old_sig")) is True
        # new_sig はまだ TTL 内
        assert store.check_and_record(_key("new_sig")) is False

    def test_thread_safety_concurrent_check_and_record(self):
        """同一キーを 100 スレッドで同時 check_and_record しても、True は 1 つだけ。"""
        store = IdempotencyStore(ttl_seconds=60)
        results: list[bool] = []
        lock = threading.Lock()

        def worker():
            r = store.check_and_record(_key("contended_sig"))
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 100 スレッドのうち True を返したのは厳密に 1 つ
        assert sum(1 for r in results if r) == 1
        assert sum(1 for r in results if not r) == 99

    def test_default_ttl_is_600_seconds(self):
        store = IdempotencyStore()
        assert store.ttl_seconds == 600


class TestCompositeKey:
    """#126: signal_id は bar 単位で払い出されるため単独ではキーにできない。

    TradingView は同一バーの銘柄別アラートに同じ signal_id を付けて送信する。
    signal_id だけで重複判定すると 2 銘柄目以降が捨てられ、リバランスが欠落する。
    """

    def test_same_signal_id_different_ticker_both_accepted(self):
        store = IdempotencyStore(ttl_seconds=60)
        bar = "20260723-093000"
        assert store.check_and_record(_key(bar, ticker="US.TQQQ")) is True
        assert store.check_and_record(_key(bar, ticker="US.TLT")) is True
        assert store.check_and_record(_key(bar, ticker="US.GLD")) is True

    def test_same_signal_id_different_action_both_accepted(self):
        store = IdempotencyStore(ttl_seconds=60)
        bar = "20260723-093000"
        assert store.check_and_record(_key(bar, action="buy")) is True
        assert store.check_and_record(_key(bar, action="sell")) is True

    def test_same_signal_id_different_broker_both_accepted(self):
        store = IdempotencyStore(ttl_seconds=60)
        bar = "20260723-093000"
        moomoo = IdempotencyKey(signal_id=bar, broker="moomoo", ticker="X", action="buy")
        oanda = IdempotencyKey(signal_id=bar, broker="oanda", ticker="X", action="buy")
        assert store.check_and_record(moomoo) is True
        assert store.check_and_record(oanda) is True

    def test_identical_key_still_rejected(self):
        """全要素が一致する再送は従来どおり拒否する（#41 の二重発注防止は不変）。"""
        store = IdempotencyStore(ttl_seconds=60)
        bar = "20260723-093000"
        assert store.check_and_record(_key(bar, ticker="US.TQQQ")) is True
        assert store.check_and_record(_key(bar, ticker="US.TQQQ")) is False

    def test_forget_releases_only_the_given_key(self):
        """forget は指定キーだけを解放し、同一 signal_id の別銘柄には影響しない。"""
        store = IdempotencyStore(ttl_seconds=60)
        bar = "20260723-093000"
        store.check_and_record(_key(bar, ticker="US.TQQQ"))
        store.check_and_record(_key(bar, ticker="US.TLT"))

        store.forget(_key(bar, ticker="US.TQQQ"))

        # 解放した銘柄は再受理できる
        assert store.check_and_record(_key(bar, ticker="US.TQQQ")) is True
        # 解放していない銘柄は保持されたまま
        assert store.check_and_record(_key(bar, ticker="US.TLT")) is False

    def test_key_is_immutable(self):
        """キーは frozen dataclass（辞書キーとして安全に使える）。"""
        key = _key("sig_001")
        with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
            key.ticker = "US.OTHER"  # type: ignore[misc]
