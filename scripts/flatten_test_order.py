"""go_live_smoke のテスト発注が約定した場合に、その約定分だけを反対売買して相殺する。

市場クローズ中はテスト発注が pending のまま `cleanup_simulate_orders.py` で cancel
されるため不要だが、市場時間中は即約定して建玉が残る。本スクリプトは指定 order_id の
約定分のうち「現在も保有している数量」だけを反対売買で相殺する。

over-sell ガード: 相殺数量は min(テスト発注の約定数量, 現在の保有数量) とし、
戦略由来の既存建玉や、既に手仕舞い済みのケースで誤って建玉を作らないようにする。
指定 order_id が未約定 / 不在 / 既に flat なら何もしない（冪等）。
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    from moomoo import (  # type: ignore[import-not-found]
        OpenSecTradeContext,
        OrderType,
        RET_OK,
        TimeInForce,
        TrdEnv,
        TrdMarket,
        TrdSide,
    )
except ImportError:  # moomoo-api 未導入環境（VM 等）では機能等価の futu-api を使う
    from futu import (  # type: ignore[import-not-found]
        OpenSecTradeContext,
        OrderType,
        RET_OK,
        TimeInForce,
        TrdEnv,
        TrdMarket,
        TrdSide,
    )

_MARKET_MAP = {
    "US": TrdMarket.US,
    "HK": TrdMarket.HK,
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="go_live_smoke のテスト発注の約定分を反対売買で相殺する",
    )
    p.add_argument("--order-id", required=True, help="相殺対象のテスト発注 order_id")
    p.add_argument(
        "--market", choices=list(_MARKET_MAP.keys()), default="US", help="対象市場"
    )
    p.add_argument(
        "--trd-env",
        dest="trd_env",
        choices=["SIMULATE", "REAL"],
        default="SIMULATE",
        help="取引環境（デフォルト: SIMULATE）",
    )
    p.add_argument("--host", default="127.0.0.1", help="OpenD ホスト")
    p.add_argument("--port", type=int, default=11111, help="OpenD ポート")
    return p.parse_args()


def _find_order(orders, order_id: str):
    """today の注文一覧から order_id 一致行を返す（無ければ None）。"""
    if orders is None or not hasattr(orders, "columns") or orders.empty:
        return None
    row = orders[orders["order_id"].astype(str) == str(order_id)]
    return None if row.empty else row.iloc[0]


def _sellable_qty(ctx, trd_env, code: str) -> float:
    """指定銘柄の現在の売却可能数量（無ければ 0）。"""
    ret, pos = ctx.position_list_query(trd_env=trd_env)
    if ret != RET_OK or not hasattr(pos, "columns") or pos.empty:
        return 0.0
    row = pos[pos["code"] == code]
    if row.empty:
        return 0.0
    col = "can_sell_qty" if "can_sell_qty" in row.columns else "qty"
    try:
        return float(row[col].iloc[0])
    except (ValueError, TypeError):
        return 0.0


def main() -> int:
    args = parse_args()
    market = _MARKET_MAP[args.market]
    trd_env = TrdEnv.SIMULATE if args.trd_env == "SIMULATE" else TrdEnv.REAL

    if trd_env == TrdEnv.REAL and os.environ.get("CONFIRM_REAL") != "1":
        print(
            "[ABORT] REAL 環境では実行しません（環境変数 CONFIRM_REAL=1 が必要）。",
            file=sys.stderr,
        )
        return 1

    ctx = OpenSecTradeContext(
        filter_trdmarket=market, host=args.host, port=args.port
    )
    try:
        ret, orders = ctx.order_list_query(trd_env=trd_env)
        if ret != RET_OK:
            print(f"[ERROR] order_list_query failed: {orders}", file=sys.stderr)
            return 2

        order = _find_order(orders, args.order_id)
        if order is None:
            print(f"[INFO] order_id={args.order_id} が見つかりません。相殺不要。")
            return 0

        code = str(order["code"])
        side = str(order["trd_side"]).upper()
        dealt_qty = float(order.get("dealt_qty", 0) or 0)

        if dealt_qty <= 0:
            print(
                f"[INFO] order_id={args.order_id} ({code} {side}) は未約定 "
                "(dealt_qty=0)。pending は cleanup 側で cancel 済み。相殺不要。"
            )
            return 0

        if side == "BUY":
            # BUY テスト → 保有が増えた分を SELL で戻す（over-sell ガードで保有上限）
            offset_qty = min(dealt_qty, _sellable_qty(ctx, trd_env, code))
            offset_side = TrdSide.SELL
        else:
            # SELL テスト → 約定数量を BUY で買い戻す
            offset_qty = dealt_qty
            offset_side = TrdSide.BUY

        if offset_qty <= 0:
            print(
                f"[INFO] {code} は既に flat（テスト約定分は手仕舞い済み）。相殺不要。"
            )
            return 0

        offset_name = "SELL" if offset_side == TrdSide.SELL else "BUY"
        print(
            f"[INFO] テスト発注 {args.order_id} ({code} {side} 約定 {dealt_qty}) を相殺: "
            f"{offset_name} {offset_qty} {code}（SIMULATE 成行）"
        )
        ret, data = ctx.place_order(
            price=0,
            qty=offset_qty,
            code=code,
            trd_side=offset_side,
            order_type=OrderType.MARKET,
            trd_env=trd_env,
            time_in_force=TimeInForce.DAY,
        )
        if ret != RET_OK:
            print(f"[ERROR] 相殺注文に失敗: {data}", file=sys.stderr)
            return 2

        print("[OK] 相殺注文を発注しました。")
        if hasattr(data, "to_string"):
            cols = [
                c
                for c in ("order_id", "code", "trd_side", "qty", "order_status")
                if c in data.columns
            ]
            print(data[cols].to_string(index=False))
        return 0
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
