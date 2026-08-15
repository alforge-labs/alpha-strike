"""TradingView シグナルの途絶を検知して通知する watchdog。

背景: TradingView は現行プランでアラートの有効期限が最大 1 ヶ月で、期限が切れると
**サイレントに配信を停止する**。alpha-strike 側は webhook サーバも OpenD も正常で
``/status`` は HTTP 200 を返し続けるため、「イベントログにシグナルが来ない」という
形でしか症状が出ない。実際に 2 回（2026-06-27 起点で 7 営業日、2026-08-08 起点で
4 営業日）取りこぼし、いずれも人手の点検でしか気づけなかった。

判定は「最後の ``signal_received`` からの **土日除外実効時間** がしきい値超か」。
シグナルは毎営業日 16:01 ET（= 05:01 JST）に届くため、実効時間で測ると正常な間隔は
最大 29h（週末跨ぎ）、米国祝日が 1 日挟まって最大 53h。既定しきい値 60h なら
誤報ゼロで 2 セッション欠落を捕捉できる。

VM / サービス停止は検知できない（watchdog もサーバごと死ぬため）。それは外部からの
死活監視が要る別課題。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from alpha_strike.services.market_hours import effective_hours_between

logger = logging.getLogger(__name__)

_ENABLED_ENV_VAR = "SIGNAL_WATCHDOG_ENABLED"
_INTERVAL_ENV_VAR = "SIGNAL_WATCHDOG_INTERVAL_SECONDS"
_THRESHOLD_ENV_VAR = "SIGNAL_WATCHDOG_THRESHOLD_HOURS"
_RENOTIFY_ENV_VAR = "SIGNAL_WATCHDOG_RENOTIFY_HOURS"
_BROKER_ENV_VAR = "SIGNAL_WATCHDOG_BROKER"
_TRUTHY = {"1", "true", "yes", "on"}
_VALID_BROKERS = {"oanda", "moomoo"}

DEFAULT_INTERVAL_SECONDS = 3600.0
DEFAULT_THRESHOLD_HOURS = 60.0
DEFAULT_RENOTIFY_HOURS = 24.0
DEFAULT_BROKER = "moomoo"


@dataclass(frozen=True)
class SignalOutageVerdict:
    """1 回分の途絶判定結果。"""

    is_outage: bool
    last_signal_at: datetime | None
    last_signal_id: str | None
    effective_hours: float
    threshold_hours: float


def evaluate_signal_outage(
    last_signal_at: datetime | None,
    now: datetime,
    *,
    threshold_hours: float,
    last_signal_id: str | None = None,
) -> SignalOutageVerdict:
    """実効経過が ``threshold_hours`` を **超えたら** 途絶と判定する。

    ``last_signal_at`` が ``None``（シグナル 0 件）は途絶と判定しない。初回デプロイ
    直後やログ空の状態で即発報すると誤報になるため（fail-safe）。

    境界は等号を含めない。しきい値ちょうど（祝日ケースの上限付近）で鳴らさないことで、
    祝日 1 日を挟んだ 53 実効時間との間に余裕を残す。
    """
    if last_signal_at is None:
        return SignalOutageVerdict(
            is_outage=False,
            last_signal_at=None,
            last_signal_id=None,
            effective_hours=0.0,
            threshold_hours=threshold_hours,
        )
    effective = effective_hours_between(last_signal_at, now)
    return SignalOutageVerdict(
        is_outage=effective > threshold_hours,
        last_signal_at=last_signal_at,
        last_signal_id=last_signal_id,
        effective_hours=effective,
        threshold_hours=threshold_hours,
    )


def is_signal_watchdog_enabled() -> bool:
    """途絶監視の有効可否。既定 ON。

    ``NTFY_TOPIC`` 未設定なら通知は no-op になるため、既定 ON でも配布ユーザーに
    実害は無い（``CARRYOVER_ENABLED`` / ``PENDING_RECONCILE_ENABLED`` と同じ方針）。
    """
    return os.getenv(_ENABLED_ENV_VAR, "1").strip().lower() in _TRUTHY


def _positive_float_env(name: str, default: float) -> float:
    """正の float 環境変数。不正値・0 以下は既定へフォールバックする。"""
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s が数値ではありません、既定の %s を使用", name, default)
        return default
    return value if value > 0 else default


def get_signal_watchdog_interval() -> float:
    """チェック間隔（秒）。既定 3600 秒（しきい値 60h に対し 1 時間毎で十分）。"""
    return _positive_float_env(_INTERVAL_ENV_VAR, DEFAULT_INTERVAL_SECONDS)


def get_signal_watchdog_threshold_hours() -> float:
    """途絶と判定する実効時間（土日除外）。既定 60 時間。"""
    return _positive_float_env(_THRESHOLD_ENV_VAR, DEFAULT_THRESHOLD_HOURS)


def get_signal_watchdog_renotify_hours() -> float:
    """途絶継続中の再通知の最小間隔（時間）。既定 24 時間。"""
    return _positive_float_env(_RENOTIFY_ENV_VAR, DEFAULT_RENOTIFY_HOURS)


def get_signal_watchdog_broker() -> str:
    """監視対象 broker。既定 moomoo。不正値は既定へフォールバック。

    イベントの ``broker`` にもこの値を使う。``event_logger.append`` は broker で
    書き込み先ファイルを決めるため、不正値を通すと ``.unknown.jsonl`` が生まれ
    alpha-forge 側の ``glob("*.jsonl")`` に混入する。
    """
    raw = os.getenv(_BROKER_ENV_VAR, DEFAULT_BROKER).strip().lower()
    if raw not in _VALID_BROKERS:
        logger.warning(
            "%s が不正な値のため既定 %s を使用", _BROKER_ENV_VAR, DEFAULT_BROKER
        )
        return DEFAULT_BROKER
    return raw


def find_last_signal(
    event_logger: Any, *, broker: str = DEFAULT_BROKER
) -> tuple[datetime | None, str | None]:
    """最新の ``signal_received`` の ``(occurred_at, signal_id)`` を返す。

    ``load_events`` は新しい順に返すため ``limit=1`` で最新 1 件が取れる。返り値は
    生 JSON 由来なので ``occurred_at`` は str。``fromisoformat`` でパースする
    （``carryover.py`` と同じ扱い）。

    見つからない・パースできない場合は ``(None, None)``。呼び出し側はこれを
    「途絶と判定しない」fail-safe として扱う。ここで例外を投げると常駐ループが
    毎周回エラーになる。
    """
    events = event_logger.load_events(
        broker=broker, event_type="signal_received", limit=1
    )
    if not events:
        return None, None
    event = events[0]
    try:
        occurred_at = datetime.fromisoformat(str(event.get("occurred_at")))
    except (TypeError, ValueError):
        logger.warning("signal_received の occurred_at をパースできませんでした")
        return None, None
    signal_id = event.get("signal_id")
    return occurred_at, str(signal_id) if signal_id is not None else None
