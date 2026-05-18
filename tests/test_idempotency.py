"""IdempotencyStore のユニットテスト (issue #41)"""

from __future__ import annotations

import threading
import time

import pytest

from alpha_strike.services.idempotency import IdempotencyStore


class TestIdempotencyStore:
    def test_first_record_returns_true(self):
        store = IdempotencyStore(ttl_seconds=60)
        assert store.check_and_record("sig_001") is True

    def test_duplicate_record_returns_false(self):
        store = IdempotencyStore(ttl_seconds=60)
        store.check_and_record("sig_001")
        assert store.check_and_record("sig_001") is False

    def test_distinct_signals_both_accepted(self):
        store = IdempotencyStore(ttl_seconds=60)
        assert store.check_and_record("sig_001") is True
        assert store.check_and_record("sig_002") is True
        assert store.check_and_record("sig_003") is True

    def test_ttl_expiration_allows_reuse(self):
        store = IdempotencyStore(ttl_seconds=0.05)
        assert store.check_and_record("sig_001") is True
        # TTL 切れまで待つ
        time.sleep(0.1)
        # 同じ signal_id を再受理できる
        assert store.check_and_record("sig_001") is True

    def test_within_ttl_still_rejected(self):
        store = IdempotencyStore(ttl_seconds=10)
        store.check_and_record("sig_001")
        # まだ TTL 内
        time.sleep(0.05)
        assert store.check_and_record("sig_001") is False

    def test_empty_signal_id_is_rejected_as_invalid(self):
        """空文字列の signal_id は idempotency 対象外（記録もしない）。"""
        store = IdempotencyStore(ttl_seconds=60)
        with pytest.raises(ValueError, match="signal_id"):
            store.check_and_record("")

    def test_eviction_keeps_only_unexpired(self):
        store = IdempotencyStore(ttl_seconds=0.05)
        store.check_and_record("old_sig")
        time.sleep(0.1)
        # eviction を内部的にトリガーする新規記録
        store.check_and_record("new_sig")
        # old_sig は再受理可能 (eviction 済み)
        assert store.check_and_record("old_sig") is True
        # new_sig はまだ TTL 内
        assert store.check_and_record("new_sig") is False

    def test_thread_safety_concurrent_check_and_record(self):
        """同一 signal_id を 100 スレッドで同時 check_and_record しても、True は 1 つだけ。"""
        store = IdempotencyStore(ttl_seconds=60)
        results: list[bool] = []
        lock = threading.Lock()

        def worker():
            r = store.check_and_record("contended_sig")
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
