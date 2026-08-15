"""シグナル途絶 watchdog のユニットテスト。

WHY: TradingView のアラートは現行プランで最大 1 ヶ月しか設定できず、期限が切れると
サイレントに配信を停止する。サーバも OpenD も正常なまま webhook だけ止まるため、
イベントログの「シグナルが来ない」以外に症状が出ず、過去 2 回とも人手の点検でしか
気づけなかった（7 営業日 / 4 営業日の取りこぼし）。

このテストが守るのは 2 つ:
1. 本物の途絶で必ず鳴ること（鳴らなければ機能が存在しないのと同じ）
2. 正常な週末跨ぎ・米国祝日で鳴らないこと（誤報が続くと通知を無視するようになり、
   監視そのものが死ぬ）

日付は実在の曜日を使う:
2026-08-07(金) / 08-08(土) / 08-09(日) / 08-10(月) / 08-11(火) / 08-12(水) / 08-13(木)
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from alpha_strike.services.signal_watchdog import (
    DEFAULT_BROKER,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_RENOTIFY_HOURS,
    DEFAULT_THRESHOLD_HOURS,
    evaluate_signal_outage,
    find_last_signal,
    get_signal_watchdog_broker,
    get_signal_watchdog_interval,
    get_signal_watchdog_renotify_hours,
    get_signal_watchdog_threshold_hours,
    is_signal_watchdog_enabled,
)

_THRESHOLD = 60.0


class TestEvaluateSignalOutage:
    def test_平日の通常間隔では途絶と判定しない(self):
        v = evaluate_signal_outage(
            datetime(2026, 8, 11, 5, 0),  # 火
            datetime(2026, 8, 12, 5, 0),  # 水
            threshold_hours=_THRESHOLD,
        )
        assert v.is_outage is False
        assert v.effective_hours == pytest.approx(24.0)

    def test_週末跨ぎは暦72時間でも途絶と判定しない(self):
        """正常運用で最大の間隔。ここで鳴ると毎週末アラートが飛ぶ。"""
        v = evaluate_signal_outage(
            datetime(2026, 8, 8, 5, 0),  # 土（金の米国クローズ分）
            datetime(2026, 8, 11, 5, 0),  # 火（月の米国クローズ分）
            threshold_hours=_THRESHOLD,
        )
        assert v.is_outage is False
        assert v.effective_hours == pytest.approx(29.0)

    def test_月曜が米国祝日でも途絶と判定しない(self):
        """祝日は市場カレンダーを持たないので実効 53h を閾値で吸収する。"""
        v = evaluate_signal_outage(
            datetime(2026, 8, 8, 5, 0),  # 土
            datetime(2026, 8, 12, 5, 0),  # 水（月が休場でシグナルが 1 回飛ぶ）
            threshold_hours=_THRESHOLD,
        )
        assert v.is_outage is False
        assert v.effective_hours == pytest.approx(53.0)

    def test_しきい値ちょうどは途絶と判定しない(self):
        """境界は「超えたら」検知。等号で鳴くと祝日ケースと 1 秒差で衝突する。"""
        v = evaluate_signal_outage(
            datetime(2026, 8, 10, 0, 0),  # 月 00:00
            datetime(2026, 8, 12, 12, 0),  # 水 12:00 = 実効ちょうど 60h
            threshold_hours=_THRESHOLD,
        )
        assert v.effective_hours == pytest.approx(60.0)
        assert v.is_outage is False

    def test_実際の障害ケースでは途絶と判定する(self):
        """2026-08-08 05:01 を最後に途絶した実障害の再現。"""
        v = evaluate_signal_outage(
            datetime(2026, 8, 8, 5, 0),  # 土
            datetime(2026, 8, 13, 5, 0),  # 木
            threshold_hours=_THRESHOLD,
            last_signal_id="20260807-093000",
        )
        assert v.is_outage is True
        assert v.effective_hours == pytest.approx(77.0)
        assert v.last_signal_id == "20260807-093000"

    def test_シグナルが1件も無ければ途絶と判定しない(self):
        """初回デプロイ直後やログ空で即発報すると誤報になる（fail-safe）。"""
        v = evaluate_signal_outage(
            None, datetime(2026, 8, 13, 5, 0), threshold_hours=_THRESHOLD
        )
        assert v.is_outage is False
        assert v.last_signal_at is None
        assert v.effective_hours == pytest.approx(0.0)


def _signal_event(
    occurred_at: str = "2026-08-08T05:01:12.363660",
    signal_id: str = "20260807-093000",
) -> dict:
    return {
        "event_type": "signal_received",
        "event_id": "evt_1",
        "signal_id": signal_id,
        "occurred_at": occurred_at,
        "broker": "moomoo",
        "asset_class": "US",
        "action": "sell",
        "ticker": "US.TQQQ",
        "quantity": 15.0,
        "strategy_id": "beat_qqq_hedged_v1",
    }


def _logger_with(events: list[dict]) -> MagicMock:
    logger = MagicMock()
    logger.load_events.return_value = events
    return logger


class TestEnvHelpers:
    def test_既定は有効(self, monkeypatch):
        monkeypatch.delenv("SIGNAL_WATCHDOG_ENABLED", raising=False)
        assert is_signal_watchdog_enabled() is True

    def test_0で無効化できる(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_WATCHDOG_ENABLED", "0")
        assert is_signal_watchdog_enabled() is False

    def test_数値でない間隔は既定へフォールバック(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_WATCHDOG_INTERVAL_SECONDS", "abc")
        assert get_signal_watchdog_interval() == DEFAULT_INTERVAL_SECONDS

    def test_0以下のしきい値は既定へフォールバック(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_WATCHDOG_THRESHOLD_HOURS", "-5")
        assert get_signal_watchdog_threshold_hours() == DEFAULT_THRESHOLD_HOURS

    def test_不正なbrokerは既定へフォールバック(self, monkeypatch):
        """イベントモデルが Literal で弾く前に落とす。unknown.jsonl を作らせない。"""
        monkeypatch.setenv("SIGNAL_WATCHDOG_BROKER", "kabu")
        assert get_signal_watchdog_broker() == DEFAULT_BROKER

    def test_有効なbrokerはそのまま使う(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_WATCHDOG_BROKER", "oanda")
        assert get_signal_watchdog_broker() == "oanda"

    def test_数値でない再通知間隔は既定へフォールバック(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_WATCHDOG_RENOTIFY_HOURS", "invalid")
        assert get_signal_watchdog_renotify_hours() == DEFAULT_RENOTIFY_HOURS

    def test_0以下の再通知間隔は既定へフォールバック(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_WATCHDOG_RENOTIFY_HOURS", "0")
        assert get_signal_watchdog_renotify_hours() == DEFAULT_RENOTIFY_HOURS

    def test_有効な再通知間隔はそのまま使う(self, monkeypatch):
        """環境変数名の取り違え検出。THRESHOLD と RENOTIFY を混同していないか確認。"""
        monkeypatch.setenv("SIGNAL_WATCHDOG_RENOTIFY_HOURS", "48")
        assert get_signal_watchdog_renotify_hours() == 48.0


class TestFindLastSignal:
    def test_最新のsignal_receivedを返す(self):
        logger = _logger_with([_signal_event()])
        at, sid = find_last_signal(logger, broker="moomoo")
        assert at == datetime(2026, 8, 8, 5, 1, 12, 363660)
        assert sid == "20260807-093000"
        logger.load_events.assert_called_once_with(
            broker="moomoo", event_type="signal_received", limit=1
        )

    def test_イベントが無ければNoneを返す(self):
        at, sid = find_last_signal(_logger_with([]), broker="moomoo")
        assert at is None
        assert sid is None

    def test_occurred_atが壊れていてもNoneを返す(self):
        """パース失敗で例外を投げると常駐ループが毎周回落ちる。"""
        logger = _logger_with([_signal_event(occurred_at="not-a-date")])
        at, sid = find_last_signal(logger, broker="moomoo")
        assert at is None
        assert sid is None
