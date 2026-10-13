"""シグナルと発注の照合キーのユニットテスト。

WHY: ここには再導出したくない知見が 2 つ埋まっている。

1. signal_id は bar 単位で払い出され、同一バーの銘柄別シグナルが同じ値を共有する。
   単独でキーにすると 1 銘柄の発注で他銘柄まで解消済みと誤判定する（#126）。
2. action をキーに含めてはいけない。target_qty closed-loop (#80) と over-sell
   ガード (#74) が発注方向を反転させるため、queued が buy でも order_recorded は
   sell になり得る。含めると照合が成立せず再発注が繰り返される。

carryover と order_watchdog の 2 箇所で使うため共有モジュールに置く。
"""

from __future__ import annotations

from alpha_strike.services.signal_identity import (
    CO_SUFFIX,
    carryover_key,
    carryover_signal_id,
    intent_key,
)


class TestIntentKey:
    def test_signal_idとtickerの複合キーになる(self):
        assert intent_key({"signal_id": "20261012-093000", "ticker": "US.TQQQ"}) == (
            "20261012-093000",
            "US.TQQQ",
        )

    def test_同一signal_idの別銘柄は別キーになる(self):
        a = intent_key({"signal_id": "S1", "ticker": "US.TQQQ"})
        b = intent_key({"signal_id": "S1", "ticker": "US.GLD"})
        assert a != b

    def test_actionはキーに含めない(self):
        """target reconcile が方向を反転させても照合が成立すること。"""
        queued = intent_key({"signal_id": "S1", "ticker": "US.TQQQ", "action": "buy"})
        ordered = intent_key({"signal_id": "S1", "ticker": "US.TQQQ", "action": "sell"})
        assert queued == ordered

    def test_欠損フィールドは空文字になる(self):
        assert intent_key({}) == ("", "")


class TestCarryoverKey:
    def test_signal_idにサフィックスが付く(self):
        assert carryover_signal_id("S1") == f"S1{CO_SUFFIX}"

    def test_キー変換はsignal_id側だけを変える(self):
        assert carryover_key(("S1", "US.TQQQ")) == (f"S1{CO_SUFFIX}", "US.TQQQ")

    def test_サフィックスのリテラル値を固定する(self):
        """この値は order_recorded の signal_id としてイベントログへ永続化される。

        デプロイを跨いで変えると、未解消の carry-over の照合が切れて取りこぼす。
        定数から期待値を組む他のテストでは変更を検知できないため、ここだけリテラルで固定する。
        """
        assert CO_SUFFIX == "__carryover"
        assert carryover_signal_id("S1") == "S1__carryover"
