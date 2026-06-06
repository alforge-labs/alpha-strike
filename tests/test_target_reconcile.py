"""target_qty closed-loop 数量解決（#80）のユニットテスト。

WHY: Pine→webhook→broker は open-loop で、送信側の想定保有と broker 実保有が
0 約定・部分約定・端数・拒否で乖離する。target_qty（目標絶対保有量）と
broker 実保有の差分から発注 side / quantity を再解決することで、乖離があっても
次のシグナルで実保有が target へ収束する——その収束計算の正しさを固定する。
"""

from __future__ import annotations

import pytest

from alpha_strike.models import WebhookPayload
from alpha_strike.services.status_service import (
    AccountStatus,
    AccountSummary,
    PositionRecord,
)
from alpha_strike.services.target_reconcile import (
    is_target_reconcile_enabled,
    resolve_target_order,
)


def _payload(
    action: str = "buy",
    quantity: float = 12.0,
    target_qty: float | None = 47.0,
    ticker: str = "US.TQQQ",
) -> WebhookPayload:
    return WebhookPayload(
        passphrase="secret",
        broker="moomoo",
        asset_class="US",
        action=action,
        ticker=ticker,
        quantity=quantity,
        target_qty=target_qty,
    )


def _provider_with(*positions: PositionRecord):
    status = AccountStatus(
        broker="moomoo",
        trd_env="SIMULATE",
        account=AccountSummary(),
        positions=list(positions),
        recent_orders=[],
    )

    class _FakeProvider:
        def get_status(self, *, trd_env: str | None = None) -> AccountStatus:
            return status

    return _FakeProvider()


class TestResolveTargetOrder:
    def test_target_above_holding_resolves_buy(self):
        """target 47 / 実保有 40 → buy 7（Pine の delta ではなく実保有差分）。"""
        provider = _provider_with(PositionRecord(code="US.TQQQ", qty=40.0))
        decision = resolve_target_order(_payload(), provider)
        assert decision.action == "order"
        assert decision.side == "buy"
        assert decision.quantity == 7.0

    def test_target_below_holding_resolves_sell(self):
        """target 40 / 実保有 47 → sell 7。"""
        provider = _provider_with(PositionRecord(code="US.TQQQ", qty=47.0))
        decision = resolve_target_order(_payload(target_qty=40.0), provider)
        assert decision.action == "order"
        assert decision.side == "sell"
        assert decision.quantity == 7.0

    def test_target_reached_skips(self):
        """target == 実保有 → skip（重複発注しない）。"""
        provider = _provider_with(PositionRecord(code="US.TQQQ", qty=47.0))
        decision = resolve_target_order(_payload(target_qty=47.0), provider)
        assert decision.action == "skip"

    def test_float_dust_difference_skips(self):
        """float 誤差レベルの差分（1e-12）は発注せず skip する。"""
        provider = _provider_with(
            PositionRecord(code="US.TQQQ", qty=47.0 + 1e-12)
        )
        decision = resolve_target_order(_payload(target_qty=47.0), provider)
        assert decision.action == "skip"

    def test_no_position_record_treats_holding_as_zero(self):
        """建玉レコードなし → 実保有 0 として target 全量を buy。"""
        provider = _provider_with()
        decision = resolve_target_order(_payload(target_qty=10.0), provider)
        assert decision.action == "order"
        assert decision.side == "buy"
        assert decision.quantity == 10.0

    def test_target_zero_sells_entire_holding(self):
        """target 0（全決済）→ 実保有 12 を全量 sell。"""
        provider = _provider_with(PositionRecord(code="US.TQQQ", qty=12.0))
        decision = resolve_target_order(
            _payload(action="sell", quantity=12.0, target_qty=0.0), provider
        )
        assert decision.action == "order"
        assert decision.side == "sell"
        assert decision.quantity == 12.0

    def test_action_flip_is_corrected(self):
        """Pine が sell と言っても実保有 < target なら buy に補正する。

        WHY: open-loop desync では Pine の想定保有が実保有を超え、
        「減らす」つもりの sell が実際には「足りない」ケースが起きる。
        broker 実保有を正とする以上、方向も target 側に合わせる。
        """
        provider = _provider_with(PositionRecord(code="US.TQQQ", qty=40.0))
        decision = resolve_target_order(
            _payload(action="sell", quantity=10.0, target_qty=50.0), provider
        )
        assert decision.action == "order"
        assert decision.side == "buy"
        assert decision.quantity == 10.0

    def test_other_ticker_positions_are_ignored(self):
        """別 ticker の建玉は照合に影響しない。"""
        provider = _provider_with(
            PositionRecord(code="US.GLD", qty=99.0),
            PositionRecord(code="US.TQQQ", qty=40.0),
        )
        decision = resolve_target_order(_payload(), provider)
        assert decision.quantity == 7.0

    def test_missing_target_qty_raises(self):
        """target_qty なしの payload は呼び出し側の契約違反 → ValueError。"""
        provider = _provider_with()
        with pytest.raises(ValueError):
            resolve_target_order(_payload(target_qty=None), provider)

    def test_provider_exception_propagates(self):
        """StatusProvider の例外は握り潰さず伝播（fail-open は呼び出し側の責務）。"""

        class _BrokenProvider:
            def get_status(self, *, trd_env: str | None = None):
                raise RuntimeError("OpenD down")

        with pytest.raises(RuntimeError):
            resolve_target_order(_payload(), _BrokenProvider())


class TestIsTargetReconcileEnabled:
    def test_default_is_enabled(self, monkeypatch):
        monkeypatch.delenv("MOOMOO_TARGET_QTY_RECONCILE", raising=False)
        assert is_target_reconcile_enabled() is True

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_truthy_values_enable(self, monkeypatch, value):
        monkeypatch.setenv("MOOMOO_TARGET_QTY_RECONCILE", value)
        assert is_target_reconcile_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "off", ""])
    def test_falsy_values_disable(self, monkeypatch, value):
        monkeypatch.setenv("MOOMOO_TARGET_QTY_RECONCILE", value)
        assert is_target_reconcile_enabled() is False
