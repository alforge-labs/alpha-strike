#!/usr/bin/env python3
"""cleanup_simulate_orders.py

moomoo OpenD の SIMULATE 環境に残っている pending 注文を一括キャンセルする
ユーティリティスクリプト。webhook テストや実験で残った pending 注文の
片付けに使う。

VM 側で実行する想定:

    cd ~/dev/alpha-strike
    .venv/bin/python scripts/cleanup_simulate_orders.py --dry-run     # 確認のみ
    .venv/bin/python scripts/cleanup_simulate_orders.py               # 実際にキャンセル

オプション:
    --market {US,HK,ALL}        対象市場（デフォルト: US, ALL は全市場）
    --trd-env {SIMULATE,REAL}   取引環境（デフォルト: SIMULATE）
    --host HOST                 OpenD ホスト（デフォルト: 127.0.0.1）
    --port PORT                 OpenD ポート（デフォルト: 11111）
    --dry-run                   キャンセルせず現在の pending 注文を表示のみ

前提:
    - moomoo-api パッケージがインストールされていること
      （alpha-strike の .venv にあり、pyproject.toml の依存に含まれる）
    - moomoo OpenD が起動済みで API ポートが listening 状態

REAL 環境への誤適用防止:
    --trd-env=REAL を指定した場合、環境変数 CONFIRM_REAL=1 が
    設定されていない限り中止する。本番口座の未約定注文を一括キャンセル
    する破壊的操作なので、明示的な意思確認を必須にしている。

終了コード:
    0  正常終了（または対象 0 件）
    1  REAL 環境の確認不足
    2  order_list_query 失敗
    3  cancel_all_order 失敗
"""
from __future__ import annotations

import argparse
import os
import sys

from moomoo import (  # type: ignore[import-not-found]
    OpenSecTradeContext,
    RET_OK,
    TrdEnv,
    TrdMarket,
)

_MARKET_MAP = {
    "US": TrdMarket.US,
    "HK": TrdMarket.HK,
    "ALL": TrdMarket.NONE,  # cancel_all_order の全市場指定
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="moomoo SIMULATE 環境の pending 注文を一括キャンセル",
    )
    p.add_argument(
        "--market",
        choices=list(_MARKET_MAP.keys()),
        default="US",
        help="対象市場（デフォルト: US, ALL は全市場）",
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
        help="キャンセルせず現在の pending 注文を表示のみ",
    )
    return p.parse_args()


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

    # OpenSecTradeContext は filter_trdmarket=TrdMarket.NONE を受け付けないため、
    # ALL 指定時は US をフィルタに使い cancel_all_order 側で全市場対象にする。
    filter_market = TrdMarket.US if market == TrdMarket.NONE else market
    ctx = OpenSecTradeContext(
        filter_trdmarket=filter_market, host=args.host, port=args.port
    )
    try:
        ret, orders = ctx.order_list_query(trd_env=trd_env)
        if ret != RET_OK:
            print(f"[ERROR] order_list_query failed: {orders}", file=sys.stderr)
            return 2

        # moomoo SDK は pandas DataFrame を返す。len() で件数を判定。
        order_count = 0 if orders is None else len(orders)

        if order_count == 0:
            print("\n=== pending 注文 0 件 (キャンセル不要) ===")
            return 0

        print(f"\n=== 現在の pending 注文 ({order_count} 件) ===")
        print(orders.to_string())

        if args.dry_run:
            print("\n[DRY-RUN] キャンセル操作は実行しません。")
            return 0

        print(f"\n=== cancel_all_order 実行 (trdmarket={args.market}) ===")
        ret, data = ctx.cancel_all_order(trd_env=trd_env, trdmarket=market)
        if ret != RET_OK:
            print(f"[ERROR] cancel_all_order failed: {data}", file=sys.stderr)
            return 3
        print(f"[INFO] cancel_all_order 成功: {data}")

        # 結果確認
        ret, orders_after = ctx.order_list_query(trd_env=trd_env)
        if ret == RET_OK:
            remaining = 0 if orders_after is None else len(orders_after)
            print("\n=== キャンセル後の pending 注文 ===")
            if remaining == 0:
                print("(0 件 ✓)")
            else:
                print(f"({remaining} 件残存)")
                print(orders_after.to_string())
        return 0
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
