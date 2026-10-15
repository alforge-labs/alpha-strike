"""発注途絶 watchdog のユニットテスト。

WHY: 2026-10-12 の障害では signal_received が 11 件記録されているのに
order_recorded 以降が 1 件も無い状態が 5 営業日続いた。既存の signal watchdog は
シグナル到着だけを見るため「実効 22.0h / しきい値 60.0h」と毎時「正常」を
記録し続け、7 営業日誰も気づかなかった。

このテストが守るのは 3 つ:
1. 発注に至らなかったシグナルで必ず鳴ること
2. 正常な carry-over 待ち（クローズ後シグナル）で鳴らないこと
3. 判定できないとき（シグナル 0 件 / grace 以内 / 壊れた行）に沈黙し、
   偽の「復旧」を出さないこと

日付は実在の曜日を使う:
2026-10-09(金) / 10-10(土) / 10-11(日) / 10-12(月) / 10-13(火)
"""

from __future__ import annotations

from datetime import datetime
from itertools import chain, combinations
from typing import get_args

import pytest

from alpha_strike.services.order_watchdog import (
    ABNORMAL_KINDS,
    DEFAULT_GRACE_MINUTES,
    DEFAULT_MARGIN_HOURS,
    DEFAULT_RENOTIFY_HOURS,
    NORMAL_KINDS,
    OrderWatchdogState,
    SignalOutcomeKind,
    classify_batch,
    get_order_watchdog_grace_minutes,
    get_order_watchdog_renotify_hours,
    is_order_watchdog_enabled,
    load_order_watchdog_state,
    run_order_watchdog_once,
)

_LOOKBACK = 48.0


def _signal(signal_id: str, ticker: str, at: datetime, **extra) -> dict:
    return {
        "event_type": "signal_received",
        "event_id": f"evt_{signal_id}_{ticker}",
        "signal_id": signal_id,
        "ticker": ticker,
        "occurred_at": at.isoformat(),
        **extra,
    }


def _order(signal_id: str, ticker: str, status: str, **extra) -> dict:
    return {
        "event_type": "order_recorded",
        "signal_id": signal_id,
        "ticker": ticker,
        "status": status,
        **extra,
    }


def _carryover(signal_id: str, ticker: str, state: str) -> dict:
    return {
        "event_type": "signal_carryover_queued",
        "signal_id": signal_id,
        "ticker": ticker,
        "carryover_state": state,
    }


def _kinds(verdict) -> dict[tuple[str, str], str]:
    """終着点を ``(signal_id, ticker)`` で引く。

    ticker だけをキーにすると、同じ銘柄で signal_id 違いの 2 件が後勝ちで 1 件に
    化け、取りこぼしや余分な判定をテストが見逃す。キー全体で引き、件数の食い違いも
    辞書の比較で検出できるようにする。
    """
    return {o.key: o.kind for o in verdict.outcomes}


class TestOrdered:
    def test_発注が成功していれば正常(self):
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[_order("S1", "US.TQQQ", "accepted")],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.judgable is True
        assert v.is_outage is False
        assert _kinds(v) == {("S1", "US.TQQQ"): "ordered"}

    def test_skippedも正常な終着点(self):
        """sell_guard / target reconcile が「発注すべきものが無い」と判断した結果。"""
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[_order("S1", "US.TQQQ", "skipped")],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is False
        assert _kinds(v) == {("S1", "US.TQQQ"): "ordered"}

    def test_carryover再発注での成功も正常(self):
        """発注側の signal_id は <signal_id>__carryover になる。"""
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[_order("S1__carryover", "US.TQQQ", "accepted")],
            carryovers=[_carryover("S1", "US.TQQQ", "queued")],
            now=datetime(2026, 10, 13, 23, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is False
        assert _kinds(v) == {("S1", "US.TQQQ"): "ordered"}


class TestOrphaned:
    def test_発注の痕跡が無ければ異常(self):
        """2026-10-06〜10-10 のパターン。signal_received だけが残り続けた。"""
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.judgable is True
        assert v.is_outage is True
        assert _kinds(v) == {("S1", "US.TQQQ"): "orphaned"}
        assert v.counts() == {"orphaned": 1}

    def test_バッチの一部だけ欠けても異常(self):
        """同一 signal_id の 2 銘柄で 1 銘柄のみ成功した場合（#126 複合キーの回帰）。

        signal_id 単独キーだと 1 銘柄の発注で両方解消済みに見えてしまう。
        """
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[
                _signal("S1", "US.GLD", sig_at),
                _signal("S1", "US.TQQQ", sig_at),
            ],
            orders=[_order("S1", "US.TQQQ", "accepted")],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is True
        assert _kinds(v) == {("S1", "US.TQQQ"): "ordered", ("S1", "US.GLD"): "orphaned"}


class TestPending:
    def test_grace以内は判定しない(self):
        """シグナル到着直後。相関イベントは秒単位で出るが、まだ待つ。"""
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[],
            carryovers=[],
            now=datetime(2026, 10, 13, 5, 20),  # 19 分後
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert _kinds(v) == {("S1", "US.TQQQ"): "pending"}
        assert v.judgable is False, "判定保留のまま recovered へ遷移させない"
        assert v.is_outage is False

    def test_grace境界ちょうどは判定保留(self):
        """等号は正常側（既存 evaluate_signal_outage が境界で鳴らさない方針と揃える）。"""
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[],
            carryovers=[],
            now=datetime(2026, 10, 13, 5, 31),  # ちょうど 30 分後
            grace_minutes=30.0,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert _kinds(v) == {("S1", "US.TQQQ"): "pending"}

    def test_grace超過でorphanedになる(self):
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[],
            carryovers=[],
            now=datetime(2026, 10, 13, 5, 32),  # 31 分後
            grace_minutes=30.0,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert _kinds(v) == {("S1", "US.TQQQ"): "orphaned"}
        assert v.is_outage is True


class TestCarryoverInFlight:
    def test_クローズ後シグナルのqueuedは正常(self):
        """05:01 JST のシグナルは市場クローズ後。次の 22:30 の寄付まで待つのが正常。"""
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[],
            carryovers=[_carryover("S1", "US.TQQQ", "queued")],
            now=datetime(2026, 10, 13, 12, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is False
        assert _kinds(v) == {("S1", "US.TQQQ"): "in_flight"}

    def test_週末跨ぎのqueuedでも鳴らない(self):
        """金クローズ分は土 05:01 に届き、月 22:30 に再発注される。

        暦時間なら 65h 超だが、実効時間（土日除外）では 22.0h にすぎない（実測）。
        ここで鳴ると毎週末アラートが飛ぶ。
        """
        sig_at = datetime(2026, 10, 10, 5, 1)  # 土
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[],
            carryovers=[_carryover("S1", "US.TQQQ", "queued")],
            now=datetime(2026, 10, 12, 22, 0),  # 月
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is False
        assert _kinds(v) == {("S1", "US.TQQQ"): "in_flight"}


class TestStalled:
    def test_lookback_margin超過のqueuedは異常(self):
        """carryover が abandoned を書くべきなのに書いていない = ループ自体の死亡。"""
        sig_at = datetime(2026, 10, 5, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[],
            carryovers=[_carryover("S1", "US.TQQQ", "queued")],
            now=datetime(2026, 10, 9, 12, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is True
        assert _kinds(v) == {("S1", "US.TQQQ"): "stalled"}

    def test_margin内はまだin_flight(self):
        """carryover ループ（300 秒間隔）が abandoned を書く余地を残す。

        margin が無いと、lookback 超過の瞬間に『ループが死んだ』と誤報する。
        """
        sig_at = datetime(2026, 10, 12, 5, 1)  # 月
        # 実効 48.5h 後 = lookback 48 超だが margin 1h 以内
        now = sig_at.replace(day=14, hour=5, minute=31)  # 水 05:31（実効 48.5h）
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[],
            carryovers=[_carryover("S1", "US.TQQQ", "queued")],
            now=now,
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert _kinds(v) == {("S1", "US.TQQQ"): "in_flight"}


class TestAbandoned:
    def test_abandonedは異常(self):
        """2026-08-22 の取りこぼしパターン。carryover が打ち切った。"""
        sig_at = datetime(2026, 10, 9, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[],
            carryovers=[
                _carryover("S1", "US.TQQQ", "queued"),
                _carryover("S1", "US.TQQQ", "abandoned"),
            ],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is True
        assert _kinds(v) == {("S1", "US.TQQQ"): "abandoned"}

    def test_abandonedでも発注が成功していれば正常(self):
        """打ち切り前に再発注が通っていた場合は取りこぼしていない。"""
        sig_at = datetime(2026, 10, 9, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[_order("S1__carryover", "US.TQQQ", "accepted")],
            carryovers=[
                _carryover("S1", "US.TQQQ", "queued"),
                _carryover("S1", "US.TQQQ", "abandoned"),
            ],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is False
        assert _kinds(v) == {("S1", "US.TQQQ"): "ordered"}


class TestDirectionFlipIsStillMatched:
    """target reconcile が発注方向を反転させても照合が成立すること。

    action を照合キーに入れると orphaned 誤判定になり、正常運用で毎日鳴る。
    誤報を 1 件でも出すと通知を無視するようになり、監視そのものが死ぬ。
    """

    def test_buyシグナルがsell発注になっても正常(self):
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at, action="buy")],
            orders=[_order("S1", "US.TQQQ", "accepted", action="sell")],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is False
        assert _kinds(v) == {("S1", "US.TQQQ"): "ordered"}


class TestFailed:
    def test_発注失敗は異常(self):
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[_order("S1", "US.TQQQ", "failed")],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is True
        assert _kinds(v) == {("S1", "US.TQQQ"): "failed"}


class TestRetrySucceeded:
    """failed と accepted が両方ある = リトライで成功した履歴。

    failed と報告すると、直っているのに鳴り続ける。
    """

    def test_失敗と成功が両方あれば正常(self):
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[
                _order("S1", "US.TQQQ", "accepted"),
                _order("S1", "US.TQQQ", "failed"),
            ],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is False
        assert _kinds(v) == {("S1", "US.TQQQ"): "ordered"}


class TestNotJudgable:
    def test_シグナル0件では判定しない(self):
        """load_events は読み込み失敗でも [] を返す。空集合は『全件正常』ではない。"""
        v = classify_batch(
            signals=[],
            orders=[],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.judgable is False
        assert v.is_outage is False
        assert v.outcomes == ()


class TestCorruptEventRow:
    """occurred_at がパースできない行で監視全体を落とさないこと。

    例外を投げると systemd timer が failed になり、監視が止まる。
    """

    def test_壊れた行は判定から除く(self):
        sig_at = datetime(2026, 10, 13, 5, 1)
        broken = _signal("S1", "US.GLD", sig_at)
        broken["occurred_at"] = "not-a-datetime"
        v = classify_batch(
            signals=[broken, _signal("S1", "US.TQQQ", sig_at)],
            orders=[_order("S1", "US.TQQQ", "accepted")],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert _kinds(v) == {("S1", "US.TQQQ"): "ordered"}

    def test_全件壊れていれば判定しない(self):
        sig_at = datetime(2026, 10, 13, 5, 1)
        broken = _signal("S1", "US.GLD", sig_at)
        broken["occurred_at"] = None
        v = classify_batch(
            signals=[broken],
            orders=[],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.judgable is False


class TestLookbackMarginBoundary:
    """``lookback + margin`` ちょうどは正常側（in_flight）、超えたら stalled。

    carryover 側は ``> lookback`` で abandoned を書く。watchdog が同じ瞬間に
    stalled と言うと、ループが abandoned を書く前に「ループが死んだ」と誤報する。
    """

    @pytest.mark.parametrize(
        ("now", "expected"),
        [
            # 月 05:01 → 水 06:01 = 実効 49.0h（lookback 48 + margin 1 ちょうど）
            (datetime(2026, 10, 14, 6, 1), "in_flight"),
            # 月 05:01 → 水 06:31 = 実効 49.5h（境界を超えた）
            (datetime(2026, 10, 14, 6, 31), "stalled"),
        ],
    )
    def test_境界ちょうどは正常側で超えたらstalled(self, now, expected):
        sig_at = datetime(2026, 10, 12, 5, 1)
        v = classify_batch(
            signals=[_signal("S1", "US.TQQQ", sig_at)],
            orders=[],
            carryovers=[_carryover("S1", "US.TQQQ", "queued")],
            now=now,
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert _kinds(v) == {("S1", "US.TQQQ"): expected}
        assert v.is_outage is (expected == "stalled")


class TestLatestBatchOnly:
    """判定対象は最新 signal_id のバッチだけ（``signals`` は新しい順）。

    古いバッチまで見ると、過去の取りこぼしで永久に鳴り続けて復旧できない。
    逆に最新バッチが欠落しているのに古いバッチが正常だからと黙ると、今回の障害
    （直近の発注が全滅）を見逃す。
    """

    def test_最新バッチが正常なら古いバッチの欠落は見ない(self):
        new_at = datetime(2026, 10, 13, 5, 1)
        old_at = datetime(2026, 10, 12, 5, 1)
        v = classify_batch(
            signals=[
                _signal("S2", "US.TQQQ", new_at),
                _signal("S1", "US.TQQQ", old_at),
            ],
            orders=[_order("S2", "US.TQQQ", "accepted")],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.signal_id == "S2"
        assert v.is_outage is False
        assert len(v.outcomes) == 1, "S1 のバッチは判定対象に含めない"
        assert _kinds(v) == {("S2", "US.TQQQ"): "ordered"}

    def test_最新バッチが欠落なら古いバッチが正常でも異常(self):
        new_at = datetime(2026, 10, 13, 5, 1)
        old_at = datetime(2026, 10, 12, 5, 1)
        v = classify_batch(
            signals=[
                _signal("S2", "US.TQQQ", new_at),
                _signal("S1", "US.TQQQ", old_at),
            ],
            orders=[_order("S1", "US.TQQQ", "accepted")],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.signal_id == "S2"
        assert v.is_outage is True
        assert _kinds(v) == {("S2", "US.TQQQ"): "orphaned"}


class TestBlankSignalId:
    """``signal_id`` が空・欠損の行を最新バッチの候補にしないこと。

    ``signals[0]`` が空 ID だと、本来の最新バッチが判定されず（偽陰性）、
    空 ID の行そのものが orphaned になって鳴る（偽陽性）という両方向の故障になる。
    ``carryover.py`` も ``if not sid`` で同じ行を除いている。
    """

    @pytest.mark.parametrize(
        "blank",
        [{"signal_id": ""}, {"signal_id": None}, {}],
        ids=["空文字", "null", "キー欠損"],
    )
    def test_空signal_idの行が混ざっても本来の最新バッチを判定する(self, blank):
        sig_at = datetime(2026, 10, 13, 5, 1)
        junk = {
            "event_type": "signal_received",
            "event_id": "evt_junk",
            "ticker": "US.TQQQ",
            "occurred_at": sig_at.isoformat(),
            **blank,
        }
        v = classify_batch(
            signals=[junk, _signal("S1", "US.TQQQ", sig_at)],  # 新しい順
            orders=[_order("S1", "US.TQQQ", "accepted")],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.signal_id == "S1"
        assert v.is_outage is False, "空 ID の行を orphaned として鳴らさない"
        assert _kinds(v) == {("S1", "US.TQQQ"): "ordered"}

    def test_全行のsignal_idが空なら判定しない(self):
        """判定できる行が 1 件も無い。『全件正常』ではなく沈黙する。"""
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[_signal("", "US.GLD", sig_at), _signal("", "US.TQQQ", sig_at)],
            orders=[],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.judgable is False
        assert v.is_outage is False
        assert v.signal_id is None
        assert v.outcomes == ()


class TestKeyIsolation:
    """他の signal_id / 他の銘柄の痕跡で、このシグナルを解消済みにしないこと。

    2026-10-12 の障害中も、過去の営業日の同一銘柄の発注はイベントログに残って
    いた。銘柄だけで照合すると、それが今日の欠落を覆い隠して障害を見逃す。
    """

    def test_前日の同一銘柄の発注では解消しない(self):
        v = classify_batch(
            signals=[_signal("S2", "US.TQQQ", datetime(2026, 10, 13, 5, 1))],
            orders=[_order("S1", "US.TQQQ", "accepted")],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is True
        assert _kinds(v) == {("S2", "US.TQQQ"): "orphaned"}

    def test_前日のcarryover再発注でも解消しない(self):
        v = classify_batch(
            signals=[_signal("S2", "US.TQQQ", datetime(2026, 10, 13, 5, 1))],
            orders=[_order("S1__carryover", "US.TQQQ", "accepted")],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is True
        assert _kinds(v) == {("S2", "US.TQQQ"): "orphaned"}

    def test_別銘柄のcarryover待ちでin_flightにならない(self):
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[
                _signal("S1", "US.GLD", sig_at),
                _signal("S1", "US.TQQQ", sig_at),
            ],
            orders=[],
            carryovers=[_carryover("S1", "US.GLD", "queued")],
            now=datetime(2026, 10, 13, 12, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.is_outage is True
        assert _kinds(v) == {
            ("S1", "US.GLD"): "in_flight",
            ("S1", "US.TQQQ"): "orphaned",
        }


class TestMixedBatchPrecedence:
    """バッチ内に複数の終着点が混在するときの優先順位。"""

    def test_異常とpendingが混在すれば異常を優先して鳴らす(self):
        """pending が 1 件あるからと沈黙すると、確定した失敗を黙殺する。"""
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[
                _signal("S1", "US.GLD", sig_at),
                _signal("S1", "US.TQQQ", sig_at),
            ],
            orders=[_order("S1", "US.GLD", "failed")],
            carryovers=[],
            now=datetime(2026, 10, 13, 5, 10),  # TQQQ は grace 以内
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert _kinds(v) == {("S1", "US.GLD"): "failed", ("S1", "US.TQQQ"): "pending"}
        assert v.judgable is True
        assert v.is_outage is True

    def test_正常とpendingが混在すれば判定保留(self):
        """片方が ordered でも、もう片方が未確定なら『全件正常』とは言えない。"""
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[
                _signal("S1", "US.GLD", sig_at),
                _signal("S1", "US.TQQQ", sig_at),
            ],
            orders=[_order("S1", "US.GLD", "accepted")],
            carryovers=[],
            now=datetime(2026, 10, 13, 5, 10),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert _kinds(v) == {("S1", "US.GLD"): "ordered", ("S1", "US.TQQQ"): "pending"}
        assert v.judgable is False
        assert v.is_outage is False

    def test_ordered_in_flight混在は正常(self):
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[
                _signal("S1", "US.GLD", sig_at),
                _signal("S1", "US.TQQQ", sig_at),
            ],
            orders=[_order("S1", "US.GLD", "accepted")],
            carryovers=[_carryover("S1", "US.TQQQ", "queued")],
            now=datetime(2026, 10, 13, 12, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert _kinds(v) == {
            ("S1", "US.GLD"): "ordered",
            ("S1", "US.TQQQ"): "in_flight",
        }
        assert v.judgable is True
        assert v.is_outage is False


class TestVerdictHelpers:
    """次タスク（発報）が使う ``counts`` / ``abnormal`` と終着点の分類表。"""

    def test_countsとabnormalは終着点ごとに集計する(self):
        sig_at = datetime(2026, 10, 13, 5, 1)
        v = classify_batch(
            signals=[
                _signal("S1", "US.GLD", sig_at),
                _signal("S1", "US.IEF", sig_at),
                _signal("S1", "US.TQQQ", sig_at),
                _signal("S1", "US.SPY", sig_at),
            ],
            orders=[
                _order("S1", "US.TQQQ", "accepted"),
                _order("S1", "US.IEF", "failed"),
            ],
            carryovers=[],
            now=datetime(2026, 10, 13, 6, 0),
            grace_minutes=DEFAULT_GRACE_MINUTES,
            lookback_hours=_LOOKBACK,
            margin_hours=DEFAULT_MARGIN_HOURS,
        )
        assert v.counts() == {"ordered": 1, "failed": 1, "orphaned": 2}
        assert {o.key[1] for o in v.abnormal} == {"US.GLD", "US.IEF", "US.SPY"}

    def test_全ての終着点は異常_正常_保留のどれか1つに属する(self):
        """終着点を足したのに分類表へ入れ忘れると、未分類の帯が生まれる。

        pending だけは異常でも正常でもない（判定保留）ので表の外に置く。
        """
        all_kinds = set(get_args(SignalOutcomeKind))
        assert ABNORMAL_KINDS & NORMAL_KINDS == set()
        assert "pending" not in ABNORMAL_KINDS | NORMAL_KINDS
        assert ABNORMAL_KINDS | NORMAL_KINDS | {"pending"} == all_kinds


def _subsets(items: list[str]) -> list[frozenset[str]]:
    return [
        frozenset(c)
        for c in chain.from_iterable(
            combinations(items, n) for n in range(len(items) + 1)
        )
    ]


class TestEveryInputIsClassified:
    """発注状態 × carry-over 状態 × 経過時間のどの組合せでも必ず終着点が決まる。

    分類に落ちない帯（未分類）があると、その入力で例外か無言の誤判定になる。
    例外は systemd timer を failed にして監視を止める。
    """

    def test_どの組合せでも有効な終着点に分類される(self):
        sig_at = datetime(2026, 10, 13, 5, 1)
        nows = [
            datetime(2026, 10, 13, 5, 10),  # grace 以内
            datetime(2026, 10, 13, 6, 0),  # grace 超・lookback 以内
            datetime(2026, 10, 20, 6, 0),  # lookback + margin 超
        ]
        valid_kinds = set(get_args(SignalOutcomeKind))
        checked = 0
        for order_states in _subsets(["accepted", "failed", "skipped"]):
            for co_states in _subsets(["queued", "abandoned"]):
                for now in nows:
                    case = f"orders={sorted(order_states)} co={sorted(co_states)} now={now}"
                    v = classify_batch(
                        signals=[_signal("S1", "US.TQQQ", sig_at)],
                        orders=[
                            _order("S1", "US.TQQQ", s) for s in sorted(order_states)
                        ],
                        carryovers=[
                            _carryover("S1", "US.TQQQ", s) for s in sorted(co_states)
                        ],
                        now=now,
                        grace_minutes=DEFAULT_GRACE_MINUTES,
                        lookback_hours=_LOOKBACK,
                        margin_hours=DEFAULT_MARGIN_HOURS,
                    )
                    (outcome,) = v.outcomes
                    assert outcome.kind in valid_kinds, case
                    # accepted / skipped が 1 件でもあれば、他の履歴によらず ordered
                    resolved = bool(order_states & {"accepted", "skipped"})
                    assert (outcome.kind == "ordered") is resolved, case
                    checked += 1
        assert checked == 8 * 4 * 3, "組合せの取りこぼし"


class TestConfig:
    def test_既定で有効(self, monkeypatch):
        monkeypatch.delenv("ORDER_WATCHDOG_ENABLED", raising=False)
        assert is_order_watchdog_enabled() is True

    def test_0で無効化できる(self, monkeypatch):
        monkeypatch.setenv("ORDER_WATCHDOG_ENABLED", "0")
        assert is_order_watchdog_enabled() is False

    def test_grace既定は30分(self, monkeypatch):
        monkeypatch.delenv("ORDER_WATCHDOG_GRACE_MINUTES", raising=False)
        assert get_order_watchdog_grace_minutes() == DEFAULT_GRACE_MINUTES

    def test_renotify既定は24時間(self, monkeypatch):
        monkeypatch.delenv("ORDER_WATCHDOG_RENOTIFY_HOURS", raising=False)
        assert get_order_watchdog_renotify_hours() == DEFAULT_RENOTIFY_HOURS

    def test_grace_とrenotifyは環境変数で上書きできる(self, monkeypatch):
        monkeypatch.setenv("ORDER_WATCHDOG_GRACE_MINUTES", "45")
        monkeypatch.setenv("ORDER_WATCHDOG_RENOTIFY_HOURS", "12")
        assert get_order_watchdog_grace_minutes() == 45.0
        assert get_order_watchdog_renotify_hours() == 12.0

    @pytest.mark.parametrize("raw", ["abc", "0", "-5", ""])
    def test_不正値は既定へ戻る(self, monkeypatch, raw):
        """0 以下や非数値で grace=0 / renotify=0 になると、鳴りっぱなしか即 orphaned になる。"""
        monkeypatch.setenv("ORDER_WATCHDOG_GRACE_MINUTES", raw)
        monkeypatch.setenv("ORDER_WATCHDOG_RENOTIFY_HOURS", raw)
        assert get_order_watchdog_grace_minutes() == DEFAULT_GRACE_MINUTES
        assert get_order_watchdog_renotify_hours() == DEFAULT_RENOTIFY_HOURS

    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_有効を表す値(self, monkeypatch, raw):
        monkeypatch.setenv("ORDER_WATCHDOG_ENABLED", raw)
        assert is_order_watchdog_enabled() is True


def _mock_logger(signals, orders, carryovers, outages=None):
    """``load_events`` をイベント種別ごとに引き分ける偽のイベントロガー。"""
    from unittest.mock import MagicMock

    m = MagicMock()

    def _load(*, broker=None, event_type=None, limit=None, **kw):
        return {
            "signal_received": signals,
            "order_recorded": orders,
            "signal_carryover_queued": carryovers,
            "order_outage_detected": outages or [],
        }.get(event_type, [])

    m.load_events.side_effect = _load
    return m


def _appended(ev):
    """``append`` に渡されたイベント（1 件だけであること）を返す。"""
    ev.append.assert_called_once()
    return ev.append.call_args.args[0]


class TestRunOnce:
    def test_異常で通知とイベントを出す(self):
        from unittest.mock import MagicMock

        sig_at = datetime(2026, 10, 13, 5, 1)
        now = datetime(2026, 10, 13, 6, 0)
        ev = _mock_logger([_signal("S1", "US.TQQQ", sig_at)], [], [])
        notifier = MagicMock()
        notifier.notify.return_value = True

        new_state = run_order_watchdog_once(
            event_logger=ev,
            notifier=notifier,
            state=OrderWatchdogState(),
            lookback_hours=_LOOKBACK,
            now=now,
        )

        assert new_state == OrderWatchdogState(last_notified_at=now, in_outage=True)
        notifier.notify.assert_called_once()
        event = _appended(ev)
        assert event.outage_state == "detected"
        assert event.signal_id == "S1"
        assert event.signal_at == sig_at
        assert event.abnormal_count == 1
        assert event.outcome_counts == {"orphaned": 1}
        assert event.occurred_at == now

    def test_通知文に銘柄と終着点が入る(self):
        """操作者が通知だけで『どの銘柄がどう欠落したか』を読み取れること。"""
        from unittest.mock import MagicMock

        sig_at = datetime(2026, 10, 13, 5, 1)
        ev = _mock_logger(
            [_signal("S1", "US.TQQQ", sig_at), _signal("S1", "US.GLD", sig_at)],
            [_order("S1", "US.GLD", "failed")],
            [],
        )
        notifier = MagicMock()

        run_order_watchdog_once(
            event_logger=ev,
            notifier=notifier,
            state=OrderWatchdogState(),
            lookback_hours=_LOOKBACK,
            now=datetime(2026, 10, 13, 6, 0),
        )

        title, message = notifier.notify.call_args.args
        assert "発注途絶" in title
        assert "US.TQQQ: orphaned" in message
        assert "US.GLD: failed" in message
        assert notifier.notify.call_args.kwargs["priority"] == "urgent"

    def test_再通知間隔内は沈黙する(self):
        from unittest.mock import MagicMock

        sig_at = datetime(2026, 10, 13, 5, 1)
        ev = _mock_logger([_signal("S1", "US.TQQQ", sig_at)], [], [])
        notifier = MagicMock()

        state = OrderWatchdogState(
            last_notified_at=datetime(2026, 10, 13, 6, 0), in_outage=True
        )
        new_state = run_order_watchdog_once(
            event_logger=ev,
            notifier=notifier,
            state=state,
            lookback_hours=_LOOKBACK,
            now=datetime(2026, 10, 13, 12, 0),  # 6 時間後
        )

        assert new_state == state
        notifier.notify.assert_not_called()
        ev.append.assert_not_called()

    @pytest.mark.parametrize(
        ("now", "renotifies"),
        [
            pytest.param(datetime(2026, 10, 14, 5, 59), False, id="23h59m_沈黙"),
            pytest.param(datetime(2026, 10, 14, 6, 0), True, id="24h_ちょうどで再通知"),
            pytest.param(datetime(2026, 10, 14, 6, 1), True, id="24h超_再通知"),
        ],
    )
    def test_再通知は24時間ちょうどから(self, now, renotifies):
        """鳴らなすぎれば見落とし、鳴りすぎれば無視される。境界を固定する。"""
        from unittest.mock import MagicMock

        sig_at = datetime(2026, 10, 13, 5, 1)
        ev = _mock_logger([_signal("S1", "US.TQQQ", sig_at)], [], [])
        notifier = MagicMock()
        last = datetime(2026, 10, 13, 6, 0)

        new_state = run_order_watchdog_once(
            event_logger=ev,
            notifier=notifier,
            state=OrderWatchdogState(last_notified_at=last, in_outage=True),
            lookback_hours=_LOOKBACK,
            renotify_hours=DEFAULT_RENOTIFY_HOURS,
            now=now,
        )

        assert new_state.in_outage is True
        if renotifies:
            notifier.notify.assert_called_once()
            assert _appended(ev).outage_state == "detected"
            assert new_state.last_notified_at == now, "再通知の起点を更新すること"
        else:
            notifier.notify.assert_not_called()
            ev.append.assert_not_called()
            assert new_state.last_notified_at == last

    def test_途絶中でない状態は再通知抑制の対象にならない(self):
        """抑制は in_outage のときだけ。last_notified_at が残っていても平常からの
        新規検知を黙らせてはいけない（不変条件への依存を残さない）。"""
        from unittest.mock import MagicMock

        sig_at = datetime(2026, 10, 13, 5, 1)
        now = datetime(2026, 10, 13, 6, 0)
        ev = _mock_logger([_signal("S1", "US.TQQQ", sig_at)], [], [])
        notifier = MagicMock()

        new_state = run_order_watchdog_once(
            event_logger=ev,
            notifier=notifier,
            state=OrderWatchdogState(
                last_notified_at=datetime(2026, 10, 13, 5, 0), in_outage=False
            ),
            lookback_hours=_LOOKBACK,
            now=now,
        )

        notifier.notify.assert_called_once()
        assert _appended(ev).outage_state == "detected"
        assert new_state == OrderWatchdogState(last_notified_at=now, in_outage=True)

    def test_復旧で1回だけ通知する(self):
        from unittest.mock import MagicMock

        sig_at = datetime(2026, 10, 14, 5, 1)
        ev = _mock_logger(
            [_signal("S2", "US.TQQQ", sig_at)],
            [_order("S2", "US.TQQQ", "accepted")],
            [],
        )
        notifier = MagicMock()

        new_state = run_order_watchdog_once(
            event_logger=ev,
            notifier=notifier,
            state=OrderWatchdogState(
                last_notified_at=datetime(2026, 10, 13, 6, 0), in_outage=True
            ),
            lookback_hours=_LOOKBACK,
            now=datetime(2026, 10, 14, 6, 0),
        )

        assert new_state == OrderWatchdogState(last_notified_at=None, in_outage=False)
        notifier.notify.assert_called_once()
        assert "再開" in notifier.notify.call_args.args[0]
        event = _appended(ev)
        assert event.outage_state == "recovered"
        assert event.abnormal_count == 0
        assert event.outcome_counts == {"ordered": 1}

    def test_復旧後の正常周回は何も出さない(self):
        """途絶中でないときの正常は日常。毎周回「再開」を飛ばしてはいけない。"""
        from unittest.mock import MagicMock

        sig_at = datetime(2026, 10, 14, 5, 1)
        ev = _mock_logger(
            [_signal("S2", "US.TQQQ", sig_at)],
            [_order("S2", "US.TQQQ", "accepted")],
            [],
        )
        notifier = MagicMock()

        state = OrderWatchdogState()
        new_state = run_order_watchdog_once(
            event_logger=ev,
            notifier=notifier,
            state=state,
            lookback_hours=_LOOKBACK,
            now=datetime(2026, 10, 14, 6, 0),
        )

        assert new_state == state
        notifier.notify.assert_not_called()
        ev.append.assert_not_called()

    def test_carryover待ちは異常にも復旧にも数えない(self):
        """クローズ後シグナルの正常な待機(in_flight)で鳴らないこと。"""
        from unittest.mock import MagicMock

        sig_at = datetime(2026, 10, 13, 5, 1)
        ev = _mock_logger(
            [_signal("S1", "US.TQQQ", sig_at)],
            [],
            [_carryover("S1", "US.TQQQ", "queued")],
        )
        notifier = MagicMock()

        new_state = run_order_watchdog_once(
            event_logger=ev,
            notifier=notifier,
            state=OrderWatchdogState(),
            lookback_hours=_LOOKBACK,
            now=datetime(2026, 10, 13, 12, 0),
        )

        assert new_state == OrderWatchdogState()
        notifier.notify.assert_not_called()
        ev.append.assert_not_called()

    def test_graceは引数で上書きできる(self):
        """grace を渡し忘れて既定 30 分で判定すると、設定が効いていないのに気づけない。"""
        from unittest.mock import MagicMock

        sig_at = datetime(2026, 10, 13, 5, 1)
        now = datetime(2026, 10, 13, 6, 0)  # シグナルから 59 分
        notifier = MagicMock()

        run_order_watchdog_once(
            event_logger=_mock_logger([_signal("S1", "US.TQQQ", sig_at)], [], []),
            notifier=notifier,
            state=OrderWatchdogState(),
            lookback_hours=_LOOKBACK,
            grace_minutes=120.0,
            now=now,
        )
        notifier.notify.assert_not_called()  # 120 分猶予なので pending（判定保留）

        run_order_watchdog_once(
            event_logger=_mock_logger([_signal("S1", "US.TQQQ", sig_at)], [], []),
            notifier=notifier,
            state=OrderWatchdogState(),
            lookback_hours=_LOOKBACK,
            grace_minutes=DEFAULT_GRACE_MINUTES,
            now=now,
        )
        notifier.notify.assert_called_once()  # 30 分猶予なので orphaned

    @pytest.mark.parametrize(
        ("lookback", "margin", "alerts"),
        [
            pytest.param(48.0, 1.0, False, id="既定_まだcarryover待ち"),
            pytest.param(24.0, 1.0, True, id="lookbackを縮めればstalled"),
            pytest.param(48.0, 0.0, True, id="marginを外せばstalled"),
        ],
    )
    def test_lookbackとmarginが判定に渡る(self, lookback, margin, alerts):
        """シグナルから実効 48.98h 後。carryover の打ち切り目安 lookback+margin との比較。"""
        from unittest.mock import MagicMock

        sig_at = datetime(2026, 10, 13, 5, 1)
        ev = _mock_logger(
            [_signal("S1", "US.TQQQ", sig_at)],
            [],
            [_carryover("S1", "US.TQQQ", "queued")],
        )
        notifier = MagicMock()

        run_order_watchdog_once(
            event_logger=ev,
            notifier=notifier,
            state=OrderWatchdogState(),
            lookback_hours=lookback,
            margin_hours=margin,
            now=datetime(2026, 10, 15, 6, 0),
        )

        assert notifier.notify.called is alerts
        if alerts:
            assert _appended(ev).outcome_counts == {"stalled": 1}

    def test_renotify_hoursは引数で上書きできる(self):
        from unittest.mock import MagicMock

        sig_at = datetime(2026, 10, 13, 5, 1)
        notifier = MagicMock()
        state = OrderWatchdogState(
            last_notified_at=datetime(2026, 10, 13, 6, 0), in_outage=True
        )

        run_order_watchdog_once(
            event_logger=_mock_logger([_signal("S1", "US.TQQQ", sig_at)], [], []),
            notifier=notifier,
            state=state,
            lookback_hours=_LOOKBACK,
            renotify_hours=4.0,
            now=datetime(2026, 10, 13, 12, 0),  # 6 時間後 = 4 時間間隔なら再通知
        )

        notifier.notify.assert_called_once()

    def test_全てのロードがbrokerを引き継ぐ(self):
        """broker を取り違えると別ブローカーのイベントを判定する。"""
        sig_at = datetime(2026, 10, 13, 5, 1)
        ev = _mock_logger([_signal("S1", "US.TQQQ", sig_at)], [], [])

        run_order_watchdog_once(
            event_logger=ev,
            state=OrderWatchdogState(),
            lookback_hours=_LOOKBACK,
            broker="oanda",
            now=datetime(2026, 10, 13, 6, 0),
        )

        loaded = {
            c.kwargs["event_type"]: c.kwargs["broker"]
            for c in ev.load_events.call_args_list
        }
        assert loaded == {
            "signal_received": "oanda",
            "order_recorded": "oanda",
            "signal_carryover_queued": "oanda",
        }
        assert _appended(ev).broker == "oanda"


class TestEmitIsAttemptNotSuccess:
    """通知の成否や記録の成否に、状態遷移を左右させない。

    「試みた」であって「成功した」ではない。通知経路が壊れていても検知履歴は残す。
    """

    def _orphaned(self):
        sig_at = datetime(2026, 10, 13, 5, 1)
        return _mock_logger([_signal("S1", "US.TQQQ", sig_at)], [], [])

    def test_通知の送信に失敗してもイベントは記録する(self):
        from unittest.mock import MagicMock

        ev = self._orphaned()
        notifier = MagicMock()
        notifier.notify.return_value = False
        notifier.enabled = True

        new_state = run_order_watchdog_once(
            event_logger=ev,
            notifier=notifier,
            state=OrderWatchdogState(),
            lookback_hours=_LOOKBACK,
            now=datetime(2026, 10, 13, 6, 0),
        )

        assert _appended(ev).outage_state == "detected"
        assert new_state.in_outage is True

    def test_通知先が未設定でもイベントは記録する(self):
        ev = self._orphaned()

        new_state = run_order_watchdog_once(
            event_logger=ev,
            notifier=None,
            state=OrderWatchdogState(),
            lookback_hours=_LOOKBACK,
            now=datetime(2026, 10, 13, 6, 0),
        )

        assert _appended(ev).outage_state == "detected"
        assert new_state.in_outage is True

    def test_イベントの追記に失敗しても例外にせず状態は進める(self):
        """例外は systemd timer を failed にして監視を止める。"""
        from unittest.mock import MagicMock

        ev = self._orphaned()
        ev.append.side_effect = OSError("disk full")
        notifier = MagicMock()
        now = datetime(2026, 10, 13, 6, 0)

        new_state = run_order_watchdog_once(
            event_logger=ev,
            notifier=notifier,
            state=OrderWatchdogState(),
            lookback_hours=_LOOKBACK,
            now=now,
        )

        notifier.notify.assert_called_once()
        assert new_state == OrderWatchdogState(last_notified_at=now, in_outage=True)


class TestNeverFalseRecovery:
    """途絶中に判定できなくなっても「復旧」を出さないこと。

    v1.2.0 で実際に踏んだ穴。読み込み失敗が続くと、操作者が最後に見たメッセージが
    「復旧しました」のまま全部止まる。
    """

    def test_シグナル0件では復旧通知を出さない(self):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.load_events.return_value = []
        notifier = MagicMock()

        state = OrderWatchdogState(
            last_notified_at=datetime(2026, 10, 13, 6, 0), in_outage=True
        )
        new_state = run_order_watchdog_once(
            event_logger=m,
            notifier=notifier,
            state=state,
            lookback_hours=_LOOKBACK,
            now=datetime(2026, 10, 15, 6, 0),
        )

        assert new_state == state, "途絶状態を維持すること"
        notifier.notify.assert_not_called()
        m.append.assert_not_called()

    def test_シグナルが読めず発注だけ読めても復旧通知を出さない(self):
        """シグナルの読み込みだけ失敗した場合。発注が見えているのは復旧の証拠ではない。"""
        from unittest.mock import MagicMock

        ev = _mock_logger(
            [],
            [_order("S2", "US.TQQQ", "accepted")],
            [_carryover("S2", "US.TQQQ", "queued")],
        )
        notifier = MagicMock()

        state = OrderWatchdogState(
            last_notified_at=datetime(2026, 10, 13, 6, 0), in_outage=True
        )
        new_state = run_order_watchdog_once(
            event_logger=ev,
            notifier=notifier,
            state=state,
            lookback_hours=_LOOKBACK,
            now=datetime(2026, 10, 15, 6, 0),
        )

        assert new_state == state
        notifier.notify.assert_not_called()
        ev.append.assert_not_called()

    def test_pendingだけの周回でも復旧通知を出さない(self):
        from unittest.mock import MagicMock

        sig_at = datetime(2026, 10, 14, 5, 1)
        m = _mock_logger([_signal("S2", "US.TQQQ", sig_at)], [], [])
        notifier = MagicMock()

        state = OrderWatchdogState(
            last_notified_at=datetime(2026, 10, 13, 6, 0), in_outage=True
        )
        new_state = run_order_watchdog_once(
            event_logger=m,
            notifier=notifier,
            state=state,
            lookback_hours=_LOOKBACK,
            now=datetime(2026, 10, 14, 5, 20),  # grace 以内
        )

        assert new_state == state
        notifier.notify.assert_not_called()
        m.append.assert_not_called()

    def test_判定不能の周回は途絶していない状態も変えない(self):
        """途絶中でないときに判定不能が来ても、新たに途絶を宣言しない。"""
        from unittest.mock import MagicMock

        m = MagicMock()
        m.load_events.return_value = []
        notifier = MagicMock()

        new_state = run_order_watchdog_once(
            event_logger=m,
            notifier=notifier,
            state=OrderWatchdogState(),
            lookback_hours=_LOOKBACK,
            now=datetime(2026, 10, 15, 6, 0),
        )

        assert new_state == OrderWatchdogState()
        notifier.notify.assert_not_called()
        m.append.assert_not_called()


class TestLoadState:
    def test_detectedから途絶中を復元する(self):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.load_events.return_value = [
            {
                "event_type": "order_outage_detected",
                "outage_state": "detected",
                "occurred_at": "2026-10-13T06:00:00",
            }
        ]
        s = load_order_watchdog_state(m)
        assert s.in_outage is True
        assert s.last_notified_at == datetime(2026, 10, 13, 6, 0)

    def test_recoveredは初期状態(self):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.load_events.return_value = [
            {
                "event_type": "order_outage_detected",
                "outage_state": "recovered",
                "occurred_at": "2026-10-14T06:00:00",
            }
        ]
        assert load_order_watchdog_state(m) == OrderWatchdogState()

    def test_0件は初期状態(self):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.load_events.return_value = []
        assert load_order_watchdog_state(m) == OrderWatchdogState()

    def test_パース失敗は初期状態(self):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.load_events.return_value = [
            {"outage_state": "detected", "occurred_at": "broken"}
        ]
        assert load_order_watchdog_state(m) == OrderWatchdogState()

    def test_最新1件のorder_outage_detectedだけを見る(self):
        """別種のイベントや別ブローカーの履歴で状態が決まってはいけない。"""
        from unittest.mock import MagicMock

        m = MagicMock()
        m.load_events.return_value = []
        load_order_watchdog_state(m, broker="oanda")
        m.load_events.assert_called_once_with(
            broker="oanda", event_type="order_outage_detected", limit=1
        )


class TestStateSurvivesProcessBoundary:
    """状態は自分が書いたイベントだけから復元する（単発実行・状態ファイル無し）。

    実際の JSONL に書いて、別のロガーインスタンス（= 別プロセス相当）で読み戻す。
    モックでは ``occurred_at`` のシリアライズ形式の食い違いを検出できない。
    """

    @staticmethod
    def _seed(path, events):
        import json

        with path.open("a", encoding="utf-8") as fp:
            for e in events:
                fp.write(json.dumps(e, ensure_ascii=False) + "\n")

    def _run(self, tmp_path, now):
        from unittest.mock import MagicMock

        from alpha_strike.event_logger import JsonlEventLogger

        logger = JsonlEventLogger(tmp_path)  # 毎回作り直す = プロセスを跨ぐ
        notifier = MagicMock()
        state = load_order_watchdog_state(logger)
        new_state = run_order_watchdog_once(
            event_logger=logger,
            notifier=notifier,
            state=state,
            lookback_hours=_LOOKBACK,
            now=now,
        )
        return state, new_state, notifier

    def test_検知_復旧_再検知を別プロセスで辿る(self, tmp_path):
        # 1 回目: 10-13 のシグナルが発注されないまま 1 時間経過 → 検知
        self._seed(
            tmp_path / "2026-10-13.moomoo.jsonl",
            [_signal("S1", "US.TQQQ", datetime(2026, 10, 13, 5, 1))],
        )
        before, after, notifier = self._run(tmp_path, datetime(2026, 10, 13, 6, 0))
        assert before == OrderWatchdogState()
        assert after.in_outage is True
        notifier.notify.assert_called_once()

        # 2 回目: 同じ状況の 6 時間後。書き残したイベントから途絶中を復元し沈黙する
        before, after, notifier = self._run(tmp_path, datetime(2026, 10, 13, 12, 0))
        assert before.in_outage is True
        assert before.last_notified_at == datetime(2026, 10, 13, 6, 0)
        assert after == before
        notifier.notify.assert_not_called()

        # 3 回目: 翌日のバッチが発注された → 復旧を 1 回だけ通知
        self._seed(
            tmp_path / "2026-10-14.moomoo.jsonl",
            [
                _signal("S2", "US.TQQQ", datetime(2026, 10, 14, 5, 1)),
                _order("S2", "US.TQQQ", "accepted"),
            ],
        )
        before, after, notifier = self._run(tmp_path, datetime(2026, 10, 14, 6, 0))
        assert before.in_outage is True
        assert after == OrderWatchdogState()
        notifier.notify.assert_called_once()

        # 4 回目: 復旧後の正常は沈黙。復旧イベントから初期状態が復元される
        before, after, notifier = self._run(tmp_path, datetime(2026, 10, 14, 7, 0))
        assert before == OrderWatchdogState()
        assert after == before
        notifier.notify.assert_not_called()

        # 5 回目: また欠落 → 復旧済みなので再び検知する（再通知抑制に巻き込まれない）
        self._seed(
            tmp_path / "2026-10-15.moomoo.jsonl",
            [_signal("S3", "US.TQQQ", datetime(2026, 10, 15, 5, 1))],
        )
        before, after, notifier = self._run(tmp_path, datetime(2026, 10, 15, 6, 0))
        assert before == OrderWatchdogState()
        assert after.in_outage is True
        notifier.notify.assert_called_once()


class TestOutcomeCountsVocabulary:
    """``outcome_counts`` は ``dict[str, int]`` で型の制約が無い。

    分類側の語彙とずれても静かに通り、事後の切り分けに使うイベントが壊れる。
    イベントに書く前にここで固定する。
    """

    def _every_kind_batch(self):
        """7 種の終着点を 1 バッチに揃える（同一 signal_id・時刻は種別ごと）。"""
        old = datetime(2026, 10, 9, 5, 1)  # 金。実効 48h + margin を十分に超える
        yday = datetime(2026, 10, 14, 5, 1)
        recent = datetime(2026, 10, 15, 5, 50)  # now の 10 分前 = grace 以内
        signals = [
            _signal("S1", "US.ORD", yday),
            _signal("S1", "US.FLY", yday),
            _signal("S1", "US.STL", old),
            _signal("S1", "US.FAIL", yday),
            _signal("S1", "US.ABN", yday),
            _signal("S1", "US.ORP", yday),
            _signal("S1", "US.PND", recent),
        ]
        orders = [_order("S1", "US.ORD", "accepted"), _order("S1", "US.FAIL", "failed")]
        carryovers = [
            _carryover("S1", "US.FLY", "queued"),
            _carryover("S1", "US.STL", "queued"),
            _carryover("S1", "US.ABN", "abandoned"),
        ]
        return signals, orders, carryovers

    def test_counts_のキーは語彙内に収まる(self):
        signals, orders, carryovers = self._every_kind_batch()
        v = classify_batch(
            signals=signals,
            orders=orders,
            carryovers=carryovers,
            now=datetime(2026, 10, 15, 6, 0),
            lookback_hours=_LOOKBACK,
        )
        vocabulary = ABNORMAL_KINDS | NORMAL_KINDS | {"pending"}
        assert set(v.counts()) <= vocabulary
        # 全種が実際に現れる = この検査が全語彙を網羅している
        assert set(v.counts()) == vocabulary, "7 種を揃えられていない"

    def test_outcome_countsのキーは語彙内に収まる(self):
        """classify_batch の結果ではなく、イベントに実際に書かれた値で検査する。"""
        signals, orders, carryovers = self._every_kind_batch()
        ev = _mock_logger(signals, orders, carryovers)

        run_order_watchdog_once(
            event_logger=ev,
            state=OrderWatchdogState(),
            lookback_hours=_LOOKBACK,
            now=datetime(2026, 10, 15, 6, 0),
        )

        counts = _appended(ev).outcome_counts
        assert set(counts) <= ABNORMAL_KINDS | NORMAL_KINDS | {"pending"}
        assert all(isinstance(n, int) and n > 0 for n in counts.values())
        assert sum(counts.values()) == 7
