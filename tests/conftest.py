"""共有テストフィクスチャ。

slowapi の rate limiter（`@limiter.limit("10/minute")`）は module-global かつ in-memory
ストレージのため、`/webhook` を POST する複数テストが同一分内で相乗りすると累積カウントが
10 を超えて 429 が出る（テスト追加で顕在化する潜在的な脆さ）。各テストの前にカウンタを
リセットして、テスト間の相互干渉を防ぐ。

同様に ``app.state.status_provider`` も module-global な ``app`` に載るためテスト間で
漏れる。over-sell ガード（moomoo SELL で ``status_provider`` を参照）導入後は、ある
テストが設定した Fake provider が別テストの SELL を巻き込んで skip/clamp してしまう。
各テスト前後で None にクリアし、必要なテストだけが明示的に設定する形に揃える。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from alpha_strike.webhook_server import limiter

    limiter._storage.reset()
    yield
    limiter._storage.reset()


@pytest.fixture(autouse=True)
def _reset_status_provider():
    from alpha_strike.webhook_server import app

    app.state.status_provider = None
    yield
    app.state.status_provider = None
