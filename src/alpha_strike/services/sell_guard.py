"""SELL 発注前の over-sell ガード。

背景: Pine Script (TradingView) → webhook → broker のパイプラインは open-loop で、
Pine が内部ポジション ``prev_qty`` を「理想約定」前提で進める一方、実 broker の約定は
0 約定・部分約定・端数切り捨て等でズレる。結果、実保有を超える SELL が webhook に届き、
moomoo が ``ret_code=-1 "Not enough positions"`` で拒否 → RuntimeError を誘発する
（建玉ゼロでの空売りも同根）。

本ガードは broker の実保有 ``can_sell_qty`` を正として、SELL 数量を
proceed / clamp / skip に解決し、over-sell を broker 境界で根絶する。
``status_provider`` は ``MoomooStatusProvider``（既存・status API と共用）を再利用する。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

from alpha_strike.models import WebhookPayload
from alpha_strike.services.status_service import StatusProvider

logger = logging.getLogger(__name__)

_GUARD_ENV_VAR = "MOOMOO_SELL_POSITION_GUARD"
_TRUTHY = {"1", "true", "yes", "on"}


def is_sell_guard_enabled() -> bool:
    """over-sell ガードの有効可否。

    既定 ON。over-sell は常に望ましくない（broker 拒否＝RuntimeError を誘発する）ため、
    明示的に無効化したい場合のみ ``MOOMOO_SELL_POSITION_GUARD`` に偽値を設定する。
    """
    return os.getenv(_GUARD_ENV_VAR, "1").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class SellGuardDecision:
    """SELL 数量の解決結果。

    - ``proceed``: 要求数量のまま発注する
    - ``clamp``: ``quantity`` まで減らして発注する（部分クローズ）
    - ``skip``: broker へ送らない（建玉なし／売却可能数量ゼロ）
    """

    action: Literal["proceed", "clamp", "skip"]
    quantity: float
    reason: str = ""


def resolve_sell_quantity(
    payload: WebhookPayload,
    status_provider: StatusProvider,
) -> SellGuardDecision:
    """SELL 数量を broker の実保有 (``can_sell_qty``) に対して解決する。

    判定:
      - 実保有 <= 0（建玉なし／凍結）      → ``skip``
      - 0 < 実保有 < 要求数量              → ``clamp``（実保有まで）
      - 実保有 >= 要求数量                → ``proceed``

    ``status_provider.get_status()`` の例外は **握り潰さず伝播** させる。
    判定不能（OpenD 障害等）時に「正当な決済を skip する」副作用を避けるため、
    fail-open（従来通り broker へ委ねる）の判断は呼び出し側の責務とする。
    """
    status = status_provider.get_status()
    available = 0.0
    for position in status.positions:
        if position.code == payload.ticker:
            available = position.can_sell_qty
            break

    requested = payload.quantity
    if available <= 0:
        return SellGuardDecision(
            "skip",
            0.0,
            reason=(
                f"建玉なし/売却不可: {payload.ticker} can_sell_qty={available} "
                f"(要求 {requested})"
            ),
        )
    if available < requested:
        return SellGuardDecision(
            "clamp",
            available,
            reason=(
                f"clamp: {payload.ticker} 要求 {requested} > 実保有 {available}"
            ),
        )
    return SellGuardDecision("proceed", requested)
