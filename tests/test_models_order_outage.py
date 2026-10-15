"""発注途絶イベントのモデルのテスト。

WHY: alpha-forge の LiveEvent は event_type を str（Literal ではない）で受け、
等値比較のフィルタで未知の種別を無視する。新種別の追加は安全だが、
下流が equity に計上しないことを docstring で明示しておく必要がある。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from alpha_strike.models import OrderOutageDetectedEvent


class TestOrderOutageDetectedEvent:
    def test_既定はdetected(self):
        e = OrderOutageDetectedEvent(
            event_id="evt_1",
            occurred_at=datetime(2026, 10, 13, 6, 0),
            broker="moomoo",
            signal_id="20261012-093000",
            abnormal_count=2,
            outcome_counts={"orphaned": 2},
        )
        assert e.event_type == "order_outage_detected"
        assert e.outage_state == "detected"

    def test_recoveredを受け付ける(self):
        e = OrderOutageDetectedEvent(
            event_id="evt_2",
            occurred_at=datetime(2026, 10, 14, 6, 0),
            broker="moomoo",
            outage_state="recovered",
            signal_id="20261013-093000",
            abnormal_count=0,
            outcome_counts={"ordered": 3},
        )
        assert e.outage_state == "recovered"
        assert e.abnormal_count == 0

    def test_JSON化できて読み戻せる(self):
        """watchdog は自分が書いたイベントの outage_state を読み戻して状態を復元する。

        この round-trip が壊れると、途絶中に鳴り止むか鳴り続けるかのどちらかになる。
        occurred_at が文字列化されることも必要（JSONL へ書けない型だと append が落ちる）。
        """
        e = OrderOutageDetectedEvent(
            event_id="evt_3",
            occurred_at=datetime(2026, 10, 13, 6, 0),
            broker="moomoo",
            outcome_counts={"orphaned": 1, "ordered": 1},
        )
        dumped = e.model_dump(mode="json")
        assert dumped["event_type"] == "order_outage_detected"
        assert dumped["outage_state"] == "detected"
        assert isinstance(dumped["occurred_at"], str)
        assert dumped["outcome_counts"] == {"orphaned": 1, "ordered": 1}
        assert OrderOutageDetectedEvent.model_validate(dumped) == e

    def test_不正なoutage_stateを拒否する(self):
        """状態復元は Literal の厳格さに依存している。typo が通ると復元が壊れる。"""
        with pytest.raises(ValidationError):
            OrderOutageDetectedEvent(
                event_id="evt_4",
                occurred_at=datetime(2026, 10, 13, 6, 0),
                broker="moomoo",
                outage_state="recoverd",  # typo
            )
