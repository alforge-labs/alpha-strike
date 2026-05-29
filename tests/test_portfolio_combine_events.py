"""combine portfolio (alpha-forge issue #980) 向け event 拡張テスト。

WebhookPayload / SignalEvent / OrderEvent / FillEvent / TradeClosedEvent に
追加した ``portfolio_id`` / ``sub_strategy_id`` フィールドが pydantic で
正しく受理・伝播されることを確認する。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alpha_strike.models import (
    FillEvent,
    OrderEvent,
    SignalEvent,
    TradeClosedEvent,
    WebhookPayload,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_webhook_payload_accepts_portfolio_and_sub_strategy_id() -> None:
    """combine portfolio Pine が発火する payload を受理できる。"""
    payload = WebhookPayload(
        passphrase="secret",
        broker="moomoo",
        asset_class="US",
        action="buy",
        ticker="US.TQQQ",
        quantity=33,
        strategy_id="beat_qqq_hedged_v1",
        portfolio_id="beat_qqq_hedged_v1",
        sub_strategy_id="tqqq_phase2_v1",
        signal_id="20260529-135959",
        run_mode="paper",
    )
    assert payload.portfolio_id == "beat_qqq_hedged_v1"
    assert payload.sub_strategy_id == "tqqq_phase2_v1"


def test_webhook_payload_portfolio_id_is_optional() -> None:
    """単体戦略 webhook では portfolio_id / sub_strategy_id を省略可能 (後方互換)。"""
    payload = WebhookPayload(
        passphrase="secret",
        broker="moomoo",
        asset_class="US",
        action="buy",
        ticker="US.TQQQ",
        quantity=1,
        strategy_id="solo_v1",
    )
    assert payload.portfolio_id is None
    assert payload.sub_strategy_id is None


def test_webhook_payload_rejects_invalid_portfolio_id_pattern() -> None:
    """portfolio_id の pattern 制約 (英数字 + . _ - 1〜64 文字) を弾く。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WebhookPayload(
            passphrase="secret",
            broker="moomoo",
            asset_class="US",
            action="buy",
            ticker="US.TQQQ",
            quantity=1,
            portfolio_id="invalid id with spaces",
        )


def test_signal_event_propagates_portfolio_fields() -> None:
    event = SignalEvent(
        event_id="evt_001",
        signal_id="sig_001",
        occurred_at=_now(),
        broker="moomoo",
        asset_class="US",
        action="buy",
        ticker="US.TQQQ",
        quantity=33,
        portfolio_id="beat_qqq_hedged_v1",
        sub_strategy_id="tqqq_phase2_v1",
    )
    serialized = event.model_dump()
    assert serialized["portfolio_id"] == "beat_qqq_hedged_v1"
    assert serialized["sub_strategy_id"] == "tqqq_phase2_v1"


def test_order_event_propagates_portfolio_fields() -> None:
    event = OrderEvent(
        event_id="evt_001",
        signal_id="sig_001",
        order_id="ord_001",
        occurred_at=_now(),
        broker="moomoo",
        asset_class="US",
        action="buy",
        ticker="US.TQQQ",
        quantity=33,
        status="accepted",
        portfolio_id="beat_qqq_hedged_v1",
        sub_strategy_id="tqqq_phase2_v1",
    )
    serialized = event.model_dump()
    assert serialized["portfolio_id"] == "beat_qqq_hedged_v1"
    assert serialized["sub_strategy_id"] == "tqqq_phase2_v1"


def test_fill_event_propagates_portfolio_fields() -> None:
    event = FillEvent(
        event_id="evt_001",
        signal_id="sig_001",
        order_id="ord_001",
        fill_id="fill_001",
        occurred_at=_now(),
        broker="moomoo",
        asset_class="US",
        action="buy",
        ticker="US.TQQQ",
        quantity=33,
        filled_qty=33.0,
        filled_price=89.50,
        portfolio_id="beat_qqq_hedged_v1",
        sub_strategy_id="tqqq_phase2_v1",
    )
    serialized = event.model_dump()
    assert serialized["portfolio_id"] == "beat_qqq_hedged_v1"
    assert serialized["sub_strategy_id"] == "tqqq_phase2_v1"


def test_trade_closed_event_propagates_portfolio_fields() -> None:
    event = TradeClosedEvent(
        event_id="evt_close_001",
        signal_id="sig_001",
        trade_id="trd_001",
        occurred_at=_now(),
        closed_at=_now(),
        broker="moomoo",
        asset_class="US",
        action="sell",
        ticker="US.TQQQ",
        quantity=33,
        entry_price=89.50,
        exit_price=92.10,
        gross_pnl=86.0,
        net_pnl=85.0,
        portfolio_id="beat_qqq_hedged_v1",
        sub_strategy_id="tqqq_phase2_v1",
    )
    serialized = event.model_dump()
    assert serialized["portfolio_id"] == "beat_qqq_hedged_v1"
    assert serialized["sub_strategy_id"] == "tqqq_phase2_v1"
