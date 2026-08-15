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

from dataclasses import dataclass
from datetime import datetime

from alpha_strike.services.market_hours import effective_hours_between


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
