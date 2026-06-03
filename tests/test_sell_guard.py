"""SELL over-sell ガード (sell_guard) の単体テスト。

Pine→webhook→broker の open-loop desync により、実保有を超える SELL
（moomoo "Not enough positions"）や建玉ゼロの空売りが webhook に届く。
本ガードが broker の ``can_sell_qty`` を正として proceed / clamp / skip を
正しく判定することを network-free に検証する。

WHY: ここで判定を誤ると「正当な決済を握り潰す（skip 過剰）」または
「over-sell を素通しして RuntimeError を再発させる」ため、境界値
（実保有 == 要求 / 実保有 < 要求 / 実保有 == 0 / 建玉なし）を固定する。
"""

from __future__ import annotations

import pytest

from alpha_strike.models import WebhookPayload
from alpha_strike.services.sell_guard import (
    SellGuardDecision,
    is_sell_guard_enabled,
    resolve_sell_quantity,
)
from alpha_strike.services.status_service import (
    AccountStatus,
    AccountSummary,
    PositionRecord,
)

_GUARD_ENV_VAR = "MOOMOO_SELL_POSITION_GUARD"


def _status(*positions: PositionRecord) -> AccountStatus:
    return AccountStatus(
        broker="moomoo",
        trd_env="SIMULATE",
        account=AccountSummary(),
        positions=list(positions),
        recent_orders=[],
    )


class _FakeProvider:
    def __init__(self, status: AccountStatus) -> None:
        self._status = status
        self.calls = 0

    def get_status(self, *, trd_env: str | None = None) -> AccountStatus:
        self.calls += 1
        return self._status


def _sell(ticker: str, qty: float) -> WebhookPayload:
    return WebhookPayload(
        passphrase="x",
        broker="moomoo",
        asset_class="US",
        action="sell",
        ticker=ticker,
        quantity=qty,
    )


def test_proceed_when_position_covers_request():
    """実保有 >= 要求数量なら proceed（数量そのまま）。"""
    provider = _FakeProvider(
        _status(PositionRecord(code="US.TQQQ", qty=5.0, can_sell_qty=5.0))
    )
    decision = resolve_sell_quantity(_sell("US.TQQQ", 3.0), provider)
    assert decision == SellGuardDecision("proceed", 3.0)


def test_proceed_when_position_exactly_equals_request():
    """実保有 == 要求数量は境界。clamp せず proceed。"""
    provider = _FakeProvider(
        _status(PositionRecord(code="US.TQQQ", qty=3.0, can_sell_qty=3.0))
    )
    decision = resolve_sell_quantity(_sell("US.TQQQ", 3.0), provider)
    assert decision.action == "proceed"
    assert decision.quantity == 3.0


def test_clamp_when_position_below_request():
    """実保有 1 < 要求 3 → clamp して実保有まで（本番 TQQQ の再現）。"""
    provider = _FakeProvider(
        _status(PositionRecord(code="US.TQQQ", qty=1.0, can_sell_qty=1.0))
    )
    decision = resolve_sell_quantity(_sell("US.TQQQ", 3.0), provider)
    assert decision.action == "clamp"
    assert decision.quantity == 1.0


def test_skip_when_can_sell_qty_zero():
    """建玉はあるが can_sell_qty=0（凍結等）→ skip（broker へ送らない）。"""
    provider = _FakeProvider(
        _status(PositionRecord(code="US.GLD", qty=1.0, can_sell_qty=0.0))
    )
    decision = resolve_sell_quantity(_sell("US.GLD", 1.0), provider)
    assert decision.action == "skip"
    assert decision.quantity == 0.0


def test_skip_when_ticker_not_held():
    """建玉一覧に対象 ticker が無い → skip（本番 GLD 空売りの再現）。"""
    provider = _FakeProvider(
        _status(PositionRecord(code="US.TQQQ", qty=1.0, can_sell_qty=1.0))
    )
    decision = resolve_sell_quantity(_sell("US.GLD", 1.0), provider)
    assert decision.action == "skip"


def test_provider_exception_propagates_for_fail_open_at_caller():
    """provider 例外は握り潰さず伝播させる（呼び出し側で fail-open 判断するため）。"""

    class _Boom:
        def get_status(self, *, trd_env: str | None = None) -> AccountStatus:
            raise RuntimeError("OpenD down")

    with pytest.raises(RuntimeError):
        resolve_sell_quantity(_sell("US.TQQQ", 3.0), _Boom())


def test_is_sell_guard_enabled_default_true(monkeypatch):
    """既定は ON（over-sell は常に望ましくないため）。"""
    monkeypatch.delenv(_GUARD_ENV_VAR, raising=False)
    assert is_sell_guard_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "  OFF  "])
def test_is_sell_guard_enabled_disabled(monkeypatch, value):
    monkeypatch.setenv(_GUARD_ENV_VAR, value)
    assert is_sell_guard_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "On"])
def test_is_sell_guard_enabled_truthy(monkeypatch, value):
    monkeypatch.setenv(_GUARD_ENV_VAR, value)
    assert is_sell_guard_enabled() is True
