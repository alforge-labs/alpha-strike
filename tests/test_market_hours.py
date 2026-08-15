"""土日除外の実効時間計算のユニットテスト。

WHY: carry-over の lookback (#89) と signal watchdog の途絶判定が共有する土台。
暦時間で測ると金曜クローズ後のシグナルは土日だけで 48h を超え、月曜寄付前に
stale 判定されて取りこぼす。「週末を跨いでも実効時間はほとんど進まない」ことを
境界値で固定し、どちらの機能も週末で誤動作しないことを保証する。

日付は実データに合わせた実在の曜日を使う:
2026-08-07(金) / 08-08(土) / 08-09(日) / 08-10(月) / 08-11(火) / 08-12(水)
"""

from __future__ import annotations

from datetime import datetime

import pytest

from alpha_strike.services.market_hours import (
    effective_hours_between,
    weekend_hours_between,
)


class TestWeekendHoursBetween:
    def test_平日のみなら土日は0時間(self):
        start = datetime(2026, 8, 11, 5, 0)  # 火
        end = datetime(2026, 8, 12, 5, 0)  # 水
        assert weekend_hours_between(start, end) == pytest.approx(0.0)

    def test_土曜途中から火曜までは土日43時間(self):
        # 土 05:00〜24:00 = 19h + 日 24h = 43h
        start = datetime(2026, 8, 8, 5, 0)  # 土
        end = datetime(2026, 8, 11, 5, 0)  # 火
        assert weekend_hours_between(start, end) == pytest.approx(43.0)

    def test_endがstart以前なら0(self):
        start = datetime(2026, 8, 11, 5, 0)
        end = datetime(2026, 8, 10, 5, 0)
        assert weekend_hours_between(start, end) == pytest.approx(0.0)


class TestEffectiveHoursBetween:
    def test_平日24時間はそのまま24実効時間(self):
        start = datetime(2026, 8, 11, 5, 0)  # 火
        end = datetime(2026, 8, 12, 5, 0)  # 水
        assert effective_hours_between(start, end) == pytest.approx(24.0)

    def test_週末跨ぎは暦72時間でも実効29時間(self):
        """正常運用で最大の間隔。ここを誤検知すると毎週末アラートが鳴る。"""
        start = datetime(2026, 8, 8, 5, 0)  # 土（= 金の米国クローズ分）
        end = datetime(2026, 8, 11, 5, 0)  # 火（= 月の米国クローズ分）
        assert effective_hours_between(start, end) == pytest.approx(29.0)

    def test_週末を完全に内包すると48時間が差し引かれる(self):
        start = datetime(2026, 8, 7, 18, 0)  # 金 18:00
        end = datetime(2026, 8, 10, 9, 0)  # 月 09:00
        # 暦 63h - 土日 48h = 15h
        assert effective_hours_between(start, end) == pytest.approx(15.0)

    def test_endがstart以前なら0(self):
        start = datetime(2026, 8, 11, 5, 0)
        end = datetime(2026, 8, 10, 5, 0)
        assert effective_hours_between(start, end) == pytest.approx(0.0)
