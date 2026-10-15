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

``load_events`` は読み込み失敗時に例外を投げず、**読めた分だけ（``[]`` とは限らない）**
を返す。したがって**「イベントが無い」と「読めなかった」は区別できない**。空集合や
欠けた集合を「全件正常」と解釈して ``recovered`` に遷移させてはならない（v1.2.0 でこの穴を踏み、途絶中に
偽の「✅ 受信を再開」を飛ばした）。fail-safe は入口と出口の両方に掛ける。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from alpha_strike.models import OrderOutageDetectedEvent
from alpha_strike.services.fill_service import _generate_id
from alpha_strike.services.market_hours import effective_hours_between
from alpha_strike.services.signal_identity import carryover_key, intent_key
from alpha_strike.services.signal_watchdog import DEFAULT_BROKER, positive_float_env

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


_ENABLED_ENV_VAR = "ORDER_WATCHDOG_ENABLED"
_GRACE_ENV_VAR = "ORDER_WATCHDOG_GRACE_MINUTES"
_RENOTIFY_ENV_VAR = "ORDER_WATCHDOG_RENOTIFY_HOURS"
_TRUTHY = {"1", "true", "yes", "on"}
_TIME_FMT = "%Y-%m-%d %H:%M"

DEFAULT_RENOTIFY_HOURS = 24.0

# この watchdog が判定するのは最新バッチ 1 つだけで、その相関イベント
# （order_recorded / signal_carryover_queued）は webhook と同一リクエスト内（秒単位）か
# 次の寄付で書かれる。したがって必要なのは「直近の数日分」であり、carryover の
# ``_ORDER_SCAN_LIMIT=2000`` のように 48 実効時間ぶんの queued 全件の解消マーカーを
# 探す広い窓は要らない。
# 1 バッチは数銘柄。50 件あれば十数日分の最新バッチを必ず含む。
_SIGNAL_SCAN_LIMIT = 50
# 日次 15 件程度のイベント量に対し 500 件は約 1 ヶ月分をカバーする。
_CORRELATE_SCAN_LIMIT = 500


def is_order_watchdog_enabled() -> bool:
    """発注途絶の監視の有効可否。既定 ON。

    ``NTFY_TOPIC`` 未設定なら通知は no-op になるため、既定 ON でも配布ユーザーに
    実害は無い（``SIGNAL_WATCHDOG_ENABLED`` と同じ方針）。
    """
    return os.getenv(_ENABLED_ENV_VAR, "1").strip().lower() in _TRUTHY


def get_order_watchdog_grace_minutes() -> float:
    """``orphaned`` と判定するまでの猶予（暦分）。既定 30。"""
    return positive_float_env(_GRACE_ENV_VAR, DEFAULT_GRACE_MINUTES)


def get_order_watchdog_renotify_hours() -> float:
    """途絶継続中の再通知の最小間隔（時間）。既定 24。"""
    return positive_float_env(_RENOTIFY_ENV_VAR, DEFAULT_RENOTIFY_HOURS)


@dataclass(frozen=True)
class OrderWatchdogState:
    """周回をまたぐ通知抑制の状態。"""

    last_notified_at: datetime | None = None
    in_outage: bool = False


def load_order_watchdog_state(
    event_logger: Any, *, broker: str = DEFAULT_BROKER
) -> OrderWatchdogState:
    """最新の ``order_outage_detected`` から再通知抑制の状態を復元する。

    単発実行の watchdog はプロセスをまたいで状態を持てないため、自分が書いたイベントを
    唯一の状態ソースとして使う。新しい状態ファイルは作らない。
    """
    events = event_logger.load_events(
        broker=broker, event_type="order_outage_detected", limit=1
    )
    if not events:
        return OrderWatchdogState()
    event = events[0]
    if str(event.get("outage_state")) != "detected":
        return OrderWatchdogState()
    occurred_at = _parse_occurred_at(event)
    if occurred_at is None:
        return OrderWatchdogState()
    return OrderWatchdogState(last_notified_at=occurred_at, in_outage=True)


def _describe(verdict: OrderOutageVerdict) -> str:
    """通知本文に入れる異常の内訳。"""
    return "\n".join(
        f"  {o.key[1]}: {o.kind}（signal_id={o.key[0]}）" for o in verdict.abnormal
    )


def _emit(
    event_logger: Any,
    notifier: Any,
    verdict: OrderOutageVerdict,
    current: datetime,
    broker: str,
    outage_state: Literal["detected", "recovered"],
) -> None:
    """通知を試み、結果に関わらずイベントを 1 件追記する。

    「試みた」であって「成功した」ではない。``NTFY_TOPIC`` 未設定 (no-op) でも
    POST 失敗でもイベントは残す。通知経路が壊れていても検知履歴だけは追えるようにする。
    """
    signal_at = verdict.outcomes[0].signal_at if verdict.outcomes else None
    at_str = signal_at.strftime(_TIME_FMT) if signal_at is not None else "なし"

    if outage_state == "detected":
        title = "🚨 発注途絶を検知"
        message = (
            f"シグナルは届いているのに発注に至っていません（{len(verdict.abnormal)} 件）。\n"
            f"{_describe(verdict)}\n"
            f"シグナル: {at_str} JST (signal_id={verdict.signal_id})\n"
            "OpenD の認証失効を疑ってください。"
            "/status が 502（Connect timeout）で /status/events が 200 ならほぼ確定です。"
        )
        tags = ["rotating_light"]
        priority: str | None = "urgent"
        logger.error(
            "発注途絶を検知: 異常 %d 件 signal_id=%s 内訳=%s",
            len(verdict.abnormal),
            verdict.signal_id,
            verdict.counts(),
        )
    else:
        title = "✅ 発注を再開"
        message = (
            f"最新バッチの発注が全件正常に戻りました。\n"
            f"シグナル: {at_str} JST (signal_id={verdict.signal_id})"
        )
        tags = ["white_check_mark"]
        priority = None
        logger.info("発注を再開: signal_id=%s", verdict.signal_id)

    if notifier is not None:
        sent = notifier.notify(title, message, tags=tags, priority=priority)
        if not sent and getattr(notifier, "enabled", False):
            logger.warning("order watchdog の通知送信に失敗しました")

    try:
        event_logger.append(
            OrderOutageDetectedEvent(
                event_id=_generate_id("evt"),
                occurred_at=current,
                broker=broker,
                outage_state=outage_state,
                signal_id=verdict.signal_id,
                signal_at=signal_at,
                abnormal_count=len(verdict.abnormal),
                outcome_counts=verdict.counts(),
            )
        )
    except Exception as exc:  # noqa: BLE001 — 記録失敗で通知状態まで巻き戻さない
        logger.warning("order_outage_detected の追記に失敗: %s", exc)


def run_order_watchdog_once(
    *,
    event_logger: Any,
    notifier: Any = None,
    state: OrderWatchdogState,
    lookback_hours: float,
    grace_minutes: float = DEFAULT_GRACE_MINUTES,
    renotify_hours: float = DEFAULT_RENOTIFY_HOURS,
    margin_hours: float = DEFAULT_MARGIN_HOURS,
    broker: str = DEFAULT_BROKER,
    now: datetime | None = None,
) -> OrderWatchdogState:
    """1 周回分の判定・通知・イベント追記を行い、新しい state を返す。

    - 判定できない周回（シグナル 0 件 / ``pending`` あり）は**状態を変えない**。
      ここを緩めると、発注が失敗しているのに「復旧しました」を飛ばす。
    - 途絶中でも ``renotify_hours`` 未満なら沈黙する。鳴りっぱなしにすると通知を
      無視するようになり、監視そのものが死ぬ。
    """
    current = now if now is not None else datetime.now()
    verdict = classify_batch(
        signals=event_logger.load_events(
            broker=broker, event_type="signal_received", limit=_SIGNAL_SCAN_LIMIT
        ),
        orders=event_logger.load_events(
            broker=broker, event_type="order_recorded", limit=_CORRELATE_SCAN_LIMIT
        ),
        carryovers=event_logger.load_events(
            broker=broker,
            event_type="signal_carryover_queued",
            limit=_CORRELATE_SCAN_LIMIT,
        ),
        now=current,
        grace_minutes=grace_minutes,
        lookback_hours=lookback_hours,
        margin_hours=margin_hours,
    )
    counts = verdict.counts()
    normal = sum(v for k, v in counts.items() if k in NORMAL_KINDS)
    abnormal = sum(v for k, v in counts.items() if k in ABNORMAL_KINDS)
    pending = counts.get("pending", 0)
    logger.info(
        "order watchdog: signal_id=%s 判定 %d 件（正常 %d / 異常 %d / 保留 %d）",
        verdict.signal_id,
        len(verdict.outcomes),
        normal,
        abnormal,
        pending,
    )

    if not verdict.judgable:
        return state

    if verdict.is_outage:
        if state.in_outage and state.last_notified_at is not None:
            since_hours = (current - state.last_notified_at).total_seconds() / 3600.0
            if since_hours < renotify_hours:
                return state
        _emit(event_logger, notifier, verdict, current, broker, "detected")
        return OrderWatchdogState(last_notified_at=current, in_outage=True)

    if state.in_outage:
        _emit(event_logger, notifier, verdict, current, broker, "recovered")
        return OrderWatchdogState(last_notified_at=None, in_outage=False)
    return state
