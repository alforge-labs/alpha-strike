#!/usr/bin/env python3
"""cleanup_simulate_orders.py

moomoo OpenD の SIMULATE 環境に残っている pending 注文を一括キャンセル
(または削除) するユーティリティスクリプト。webhook テストや実験で残った
pending 注文の片付けに使う。

VM 側で実行する想定:

    cd ~/dev/alpha-strike
    .venv/bin/python scripts/cleanup_simulate_orders.py --dry-run     # 確認のみ
    .venv/bin/python scripts/cleanup_simulate_orders.py               # 実際に処理

オプション:
    --market {US,HK}            対象市場（デフォルト: US）
    --trd-env {SIMULATE,REAL}   取引環境（デフォルト: SIMULATE）
    --host HOST                 OpenD ホスト（デフォルト: 127.0.0.1）
    --port PORT                 OpenD ポート（デフォルト: 11111）
    --dry-run                   キャンセル/削除せず現在の pending 注文を表示のみ

処理の仕組み:
    各 pending 注文に対して順番に modify_order を呼び、まず CANCEL を試行する。
    market 時間外に SIMULATE で発注した注文は内部的に Unsubmitted 状態のため
    CANCEL では reject されるので、その場合は DELETE にフォールバックする。
    (moomoo SDK の ModifyOrderOp.DELETE は "无成交的订单才能删除" = 未約定の
    注文のみ削除可能、というドキュメント記載)

前提:
    - moomoo-api パッケージがインストールされていること
      (alpha-strike の .venv にあり、pyproject.toml の依存に含まれる)
    - moomoo OpenD が起動済みで API ポートが listening 状態

REAL 環境への誤適用防止:
    --trd-env=REAL を指定した場合、環境変数 CONFIRM_REAL=1 が
    設定されていない限り中止する。本番口座の未約定注文を一括処理する
    破壊的操作なので、明示的な意思確認を必須にしている。

終了コード:
    0  正常終了（または対象 0 件）
    1  REAL 環境の確認不足
    2  order_list_query 失敗
    3  1 件以上の注文で CANCEL/DELETE 両方失敗
"""
from __future__ import annotations

import argparse
import os
import sys

from moomoo import (  # type: ignore[import-not-found]
    ModifyOrderOp,
    OpenSecTradeContext,
    RET_OK,
    TrdEnv,
    TrdMarket,
)

_MARKET_MAP = {
    "US": TrdMarket.US,
    "HK": TrdMarket.HK,
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="moomoo SIMULATE 環境の pending 注文を一括キャンセル/削除",
    )
    p.add_argument(
        "--market",
        choices=list(_MARKET_MAP.keys()),
        default="US",
        help="対象市場（デフォルト: US）",
    )
    p.add_argument(
        "--trd-env",
        dest="trd_env",
        choices=["SIMULATE", "REAL"],
        default="SIMULATE",
        help="取引環境（デフォルト: SIMULATE）",
    )
    p.add_argument(
        "--host", default="127.0.0.1", help="OpenD ホスト（デフォルト: 127.0.0.1）"
    )
    p.add_argument(
        "--port", type=int, default=11111, help="OpenD ポート（デフォルト: 11111）"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="キャンセル/削除せず現在の pending 注文を表示のみ",
    )
    return p.parse_args()


def _cancel_or_delete(ctx, order_id: str, trd_env) -> tuple[bool, str]:
    """指定 order に対して CANCEL → DELETE の順に試行。

    market 時間内の SUBMITTED 状態なら CANCEL で成功、time 外の
    Unsubmitted 状態なら CANCEL は reject されるので DELETE にフォールバックする。
    成功時は (True, op_name) を返し、両方失敗なら (False, error_message) を返す。
    """
    # CANCEL を先に試す（SUBMITTED 状態の order 用）
    ret, data = ctx.modify_order(
        modify_order_op=ModifyOrderOp.CANCEL,
        order_id=order_id,
        qty=0,
        price=0,
        trd_env=trd_env,
    )
    if ret == RET_OK:
        return True, "CANCEL"

    cancel_err = str(data)

    # CANCEL が失敗した場合（Unsubmitted 状態の order 等）は DELETE を試す
    ret, data = ctx.modify_order(
        modify_order_op=ModifyOrderOp.DELETE,
        order_id=order_id,
        qty=0,
        price=0,
        trd_env=trd_env,
    )
    if ret == RET_OK:
        return True, "DELETE"

    delete_err = str(data)
    return False, f"CANCEL=[{cancel_err}] DELETE=[{delete_err}]"


def main() -> int:
    args = parse_args()
    market = _MARKET_MAP[args.market]
    trd_env = TrdEnv.SIMULATE if args.trd_env == "SIMULATE" else TrdEnv.REAL

    if trd_env == TrdEnv.REAL and os.environ.get("CONFIRM_REAL") != "1":
        print(
            "[ABORT] REAL 環境を選択しました。本番口座の未約定注文がすべて消えます。",
            file=sys.stderr,
        )
        print(
            "[ABORT] 続行する場合は環境変数 CONFIRM_REAL=1 を設定してください。",
            file=sys.stderr,
        )
        return 1

    print(
        f"[INFO] OpenD 接続: {args.host}:{args.port} "
        f"market={args.market} trd_env={args.trd_env}"
    )

    ctx = OpenSecTradeContext(
        filter_trdmarket=market, host=args.host, port=args.port
    )
    try:
        ret, orders = ctx.order_list_query(trd_env=trd_env)
        if ret != RET_OK:
            print(f"[ERROR] order_list_query failed: {orders}", file=sys.stderr)
            return 2

        order_count = 0 if orders is None else len(orders)

        if order_count == 0:
            print("\n=== pending 注文 0 件 (キャンセル不要) ===")
            return 0

        print(f"\n=== 現在の pending 注文 ({order_count} 件) ===")
        print(orders.to_string())

        if args.dry_run:
            print("\n[DRY-RUN] キャンセル/削除操作は実行しません。")
            return 0

        print("\n=== 個別処理開始 (CANCEL → DELETE の順に試行) ===")
        success_count = 0
        failure_count = 0
        for _, row in orders.iterrows():
            order_id = str(row["order_id"])
            code = row.get("code", "?")
            side = row.get("trd_side", "?")
            qty = row.get("qty", "?")
            ok, info = _cancel_or_delete(ctx, order_id, trd_env)
            if ok:
                print(f"  ✓ order_id={order_id} {code} {side} {qty} → {info} 成功")
                success_count += 1
            else:
                print(
                    f"  ✗ order_id={order_id} {code} {side} {qty} → 失敗: {info}",
                    file=sys.stderr,
                )
                failure_count += 1

        print(
            f"\n=== 結果: 成功 {success_count} 件 / 失敗 {failure_count} 件 ==="
        )

        # 確認のための再クエリ
        ret, orders_after = ctx.order_list_query(trd_env=trd_env)
        if ret == RET_OK:
            remaining = 0 if orders_after is None else len(orders_after)
            if remaining == 0:
                print("=== 残存 pending 注文: 0 件 ✓ ===")
            else:
                print(f"=== 残存 pending 注文: {remaining} 件 ===")
                print(orders_after.to_string())

        return 3 if failure_count > 0 else 0
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
