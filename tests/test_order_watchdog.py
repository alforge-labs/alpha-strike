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
    NORMAL_KINDS,
    SignalOutcomeKind,
    classify_batch,
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
