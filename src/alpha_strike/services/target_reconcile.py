"""target_qty による closed-loop 数量解決（#80）。

背景: Pine Script (TradingView) → webhook → broker のパイプラインは open-loop で、
Pine が内部ポジション ``prev_qty`` を「理想約定」前提で進める一方、実 broker の
約定は 0 約定・部分約定・端数切り捨て・注文拒否でズレる。#74 の sell_guard は
over-sell を broker 境界で封じ込めたが、想定保有と実保有の乖離自体は残る。

本サービスは payload の ``target_qty``（目標絶対保有量）と broker 実保有 ``qty``
の差分から発注 side / quantity を再解決する。これにより乖離が生じても、次の
シグナル受信時に実保有が target へ自動的に収束する（closed-loop 化）。
``status_provider`` は sell_guard と同じ ``MoomooStatusProvider`` を再利用する。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

from alpha_strike.models import WebhookPayload
from alpha_strike.services.status_service import StatusProvider

logger = logging.getLogger(__name__)

_RECONCILE_ENV_VAR = "MOOMOO_TARGET_QTY_RECONCILE"
_TRUTHY = {"1", "true", "yes", "on"}

# float 演算の dust（例: 1e-12）を「発注すべき差分」と誤認しないための許容誤差。
# 株は整数単位、crypto でも 1e-9 未満の発注単位は実在しない。
_QTY_EPSILON = 1e-9


def is_target_reconcile_enabled() -> bool:
    """target_qty 再解決の有効可否。

    既定 ON。closed-loop 解決は常に望ましい（open-loop desync の源流対策）ため、
    明示的に旧 delta 解釈へ戻したい場合のみ ``MOOMOO_TARGET_QTY_RECONCILE`` に
    偽値を設定する。
    """
    return os.getenv(_RECONCILE_ENV_VAR, "1").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class TargetReconcileDecision:
    """target_qty と実保有の差分から解決した発注内容。

    - ``order``: ``side`` / ``quantity`` で発注する（payload を上書き）
    - ``skip``: 実保有が既に target に到達しており broker へ送らない
    """

    action: Literal["order", "skip"]
    side: Literal["buy", "sell"] | None
    quantity: float
    reason: str = ""


def resolve_target_order(
    payload: WebhookPayload,
    status_provider: StatusProvider,
) -> TargetReconcileDecision:
    """``target_qty`` を broker の実保有 (``qty``) に対して解決する。

    判定（delta = target_qty - 実保有）:
      - ``delta > 0``           → buy ``delta``
      - ``delta < 0``           → sell ``|delta|``
      - ``|delta| <= epsilon``  → ``skip``（target 到達済み）

    ``payload.action`` と符号が逆転していても broker 実保有を正として補正する
    （open-loop desync では「減らす」つもりの sell が実際は不足、の逆転が起きる）。

    ``status_provider.get_status()`` の例外は **握り潰さず伝播** させる。
    判定不能（OpenD 障害等）時の fail-open（従来 delta のまま発注継続）の判断は
    呼び出し側の責務とする（sell_guard と同じ方針）。
    """
    if payload.target_qty is None:
        raise ValueError("target_qty が未指定の payload は解決できない")

    status = status_provider.get_status()
    held = 0.0
    for position in status.positions:
        if position.code == payload.ticker:
            held = position.qty
            break

    delta = payload.target_qty - held
    if abs(delta) <= _QTY_EPSILON:
        return TargetReconcileDecision(
            "skip",
            None,
            0.0,
            reason=(
                f"target 到達済み: {payload.ticker} "
                f"target={payload.target_qty} held={held}"
            ),
        )

    side: Literal["buy", "sell"] = "buy" if delta > 0 else "sell"
    quantity = abs(delta)
    flipped = f"（action {payload.action} から補正）" if side != payload.action else ""
    return TargetReconcileDecision(
        "order",
        side,
        quantity,
        reason=(
            f"target 解決: {payload.ticker} target={payload.target_qty} "
            f"held={held} → {side} {quantity}{flipped}"
        ),
    )
