"""共有テストフィクスチャ。

slowapi の rate limiter（`@limiter.limit("10/minute")`）は module-global かつ in-memory
ストレージのため、`/webhook` を POST する複数テストが同一分内で相乗りすると累積カウントが
10 を超えて 429 が出る（テスト追加で顕在化する潜在的な脆さ）。各テストの前にカウンタを
リセットして、テスト間の相互干渉を防ぐ。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from alpha_strike.webhook_server import limiter

    limiter._storage.reset()
    yield
    limiter._storage.reset()
