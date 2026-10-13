"""発注途絶の検知。

既存の [signal_watchdog] が「シグナルが届かない」を見るのに対し、こちらは
**「シグナルは届いたのに発注まで到達していない」** を見る。

2026-10-12 の障害（OpenD トークン失効 4 回目）では ``signal_received`` が 11 件
記録されているのに ``order_recorded`` 以降が 1 件も無い状態が 5 営業日続いた。
signal watchdog はシグナル到着だけを見るため「実効 22.0h / しきい値 60.0h」と
毎時「正常」を記録し続け、米国 7 営業日分の発注が欠落したことに誰も気づかなかった。

判定対象は **最新の ``signal_id`` に属するシグナル全件**とする。シグナルは bar 単位の
``signal_id`` で毎営業日まとめて届くため、最新バッチの顛末がそのまま「いま発注できて
いるか」を表す。既存 signal watchdog が最新 1 件だけを見るのと同じ方針で揃える。

``load_events`` は読み込み失敗時に例外ではなく ``[]`` を返す。したがって
**「イベントが無い」と「読めなかった」は区別できない**。空集合を「全件正常」と
解釈して ``recovered`` に遷移させてはならない（v1.2.0 でこの穴を踏み、途絶中に
偽の「✅ 受信を再開」を飛ばした）。fail-safe は入口と出口の両方に掛ける。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from alpha_strike.services.market_hours import effective_hours_between
from alpha_strike.services.signal_identity import carryover_key, intent_key

logger = logging.getLogger(__name__)

DEFAULT_GRACE_MINUTES = 30.0
# stalled 判定の余裕。carryover の再発注ループは 300 秒間隔なので、lookback 超過の
# 直後は「carryover がまさに abandoned を書こうとしている」可能性がある。margin なしだと
# 「ループが死んだ」と誤報する。
DEFAULT_MARGIN_HOURS = 1.0

SignalOutcomeKind = Literal[
    "ordered", "in_flight", "pending", "orphaned", "failed", "abandoned", "stalled"
]

# 発注に至らなかった = 発報すべき終着点
ABNORMAL_KINDS = frozenset({"orphaned", "failed", "abandoned", "stalled"})
# 正常に終着した = recovered へ遷移できる終着点
NORMAL_KINDS = frozenset({"ordered", "in_flight"})
_RESOLVED_ORDER_STATUSES = frozenset({"accepted", "skipped"})


@dataclass(frozen=True)
class SignalOutcome:
    """1 シグナルの終着点。"""

    key: tuple[str, str]
    kind: SignalOutcomeKind
    signal_at: datetime


@dataclass(frozen=True)
class OrderOutageVerdict:
    """1 周回分の判定結果。

    ``judgable=False`` は「異常が無い」ではなく **「判定できなかった」** である。
    この場合は通知もイベントも出さず、状態も変えない。
    """

    judgable: bool
    is_outage: bool
    signal_id: str | None
    outcomes: tuple[SignalOutcome, ...]

    def counts(self) -> dict[str, int]:
        """終着点の内訳。イベントの ``outcome_counts`` に入れる。"""
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.kind] = out.get(o.kind, 0) + 1
        return out

    @property
    def abnormal(self) -> tuple[SignalOutcome, ...]:
        return tuple(o for o in self.outcomes if o.kind in ABNORMAL_KINDS)


def _signal_id_of(event: dict) -> str:
    """``signal_id`` を文字列で返す。空・``None``・キー欠損はすべて ``""``。

    ``str(None)`` は ``"None"`` という実在しうる ID に化けるため、``None`` を
    先に弾く。
    """
    raw = event.get("signal_id")
    return "" if raw is None else str(raw)


def _parse_occurred_at(event: dict) -> datetime | None:
    """``occurred_at`` をパースする。失敗は ``None``（その行を判定から除く）。

    ここで例外を投げると systemd timer が failed になり監視が止まる。壊れた 1 行で
    監視全体を落とすより、その行だけ除いて残りを判定するほうがよい。
    """
    try:
        return datetime.fromisoformat(str(event.get("occurred_at")))
    except (TypeError, ValueError):
        logger.warning(
            "occurred_at をパースできませんでした: event_id=%s", event.get("event_id")
        )
        return None


def _classify_one(
    key: tuple[str, str],
    signal_at: datetime,
    now: datetime,
    order_statuses: dict[tuple[str, str], set[str]],
    carryover_states: dict[tuple[str, str], set[str]],
    *,
    grace_minutes: float,
    lookback_hours: float,
    margin_hours: float,
) -> SignalOutcomeKind:
    """1 シグナルの終着点を決める。

    発注は直接発注（同一キー）と carry-over 再発注（``<signal_id>__carryover``）の
    両方を見る。``accepted`` / ``skipped`` が 1 件でもあれば ``ordered`` を優先する
    （リトライで成功した履歴を ``failed`` と報告しないため）。
    """
    statuses = order_statuses.get(key, frozenset()) | order_statuses.get(
        carryover_key(key), frozenset()
    )
    if statuses & _RESOLVED_ORDER_STATUSES:
        return "ordered"
    if "failed" in statuses:
        return "failed"

    states = carryover_states.get(key, frozenset())
    if "abandoned" in states:
        return "abandoned"
    if "queued" in states:
        # lookback は carryover と同じ「市場が動いている実効時間」で測る。暦時間で
        # 測ると金曜クローズ後シグナルが週末だけで打ち切られる。
        if effective_hours_between(signal_at, now) <= lookback_hours + margin_hours:
            return "in_flight"
        return "stalled"

    # 相関イベントは webhook と同一リクエスト内で秒単位に書かれるため、grace は
    # 暦時間で足りる（土日除外は不要）。
    if (now - signal_at).total_seconds() / 60.0 <= grace_minutes:
        return "pending"
    return "orphaned"


def _index_by_key(events: list[dict], field: str) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    for e in events:
        out.setdefault(intent_key(e), set()).add(str(e.get(field, "")))
    return out


def classify_batch(
    *,
    signals: list[dict],
    orders: list[dict],
    carryovers: list[dict],
    now: datetime,
    grace_minutes: float = DEFAULT_GRACE_MINUTES,
    lookback_hours: float,
    margin_hours: float = DEFAULT_MARGIN_HOURS,
) -> OrderOutageVerdict:
    """最新 ``signal_id`` のバッチを分類して判定を返す。

    ``signals`` は ``load_events`` の戻り（**新しい順**）を前提とする。

    ``signal_id`` が空・欠損の行は最新バッチの候補から除く。先頭がそうした行だと
    本来の最新バッチが判定されず（偽陰性）、空 ID の行自体が orphaned になって鳴る
    （偽陽性）という両方向の故障になるため。``carryover.find_carryover_intents`` も
    ``if not sid`` で同じ行を除いている。
    """
    candidates = [s for s in signals if _signal_id_of(s)]
    if len(candidates) < len(signals):
        logger.warning(
            "signal_id が空のシグナルを判定から除きました: %d 件",
            len(signals) - len(candidates),
        )
    if not candidates:
        # 判定できる行が 0 件は「全件正常」ではない。``load_events`` は読み込み失敗でも
        # ``[]`` を返すので、イベントが無いのか読めなかったのか区別できない。
        return OrderOutageVerdict(
            judgable=False, is_outage=False, signal_id=None, outcomes=()
        )

    newest_signal_id = _signal_id_of(candidates[0])
    batch = [s for s in candidates if _signal_id_of(s) == newest_signal_id]

    order_statuses = _index_by_key(orders, "status")
    carryover_states = _index_by_key(carryovers, "carryover_state")

    outcomes: list[SignalOutcome] = []
    for s in batch:
        signal_at = _parse_occurred_at(s)
        if signal_at is None:
            continue
        key = intent_key(s)
        outcomes.append(
            SignalOutcome(
                key=key,
                kind=_classify_one(
                    key,
                    signal_at,
                    now,
                    order_statuses,
                    carryover_states,
                    grace_minutes=grace_minutes,
                    lookback_hours=lookback_hours,
                    margin_hours=margin_hours,
                ),
                signal_at=signal_at,
            )
        )

    if not outcomes:
        return OrderOutageVerdict(
            judgable=False,
            is_outage=False,
            signal_id=newest_signal_id,
            outcomes=(),
        )

    kinds = {o.kind for o in outcomes}
    frozen = tuple(outcomes)
    if kinds & ABNORMAL_KINDS:
        return OrderOutageVerdict(
            judgable=True, is_outage=True, signal_id=newest_signal_id, outcomes=frozen
        )
    if "pending" in kinds:
        # まだ判定できない。正常側に数えると発注失敗時に偽の「復旧」を飛ばす。
        return OrderOutageVerdict(
            judgable=False, is_outage=False, signal_id=newest_signal_id, outcomes=frozen
        )
    return OrderOutageVerdict(
        judgable=True, is_outage=False, signal_id=newest_signal_id, outcomes=frozen
    )
