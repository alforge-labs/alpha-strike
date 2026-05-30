#!/usr/bin/env python3
"""flatten_simulate_positions.py

moomoo OpenD の SIMULATE 環境に残っている保有建玉を、指定した銘柄だけ
成行 SELL で決済（flatten）するユーティリティスクリプト。

webhook テストや実験で残った無関係なペーパー建玉（例: 過去の手動発注の残骸）の
片付けに使う。``cleanup_simulate_orders.py`` が pending 注文のキャンセル専用なのに対し、
本スクリプトは「保有建玉」を売り決済する点が異なる。

VM 側で実行する想定:

    cd ~/dev/alpha-strike
    # 1. まず dry-run で対象建玉を確認（注文は出さない）
    .venv/bin/python scripts/flatten_simulate_positions.py --tickers US.IBIT,US.AAPL,US.BITO --dry-run
    # 2. 米国市場の開場時間中に実行（成行のため約定は market open が前提）
    .venv/bin/python scripts/flatten_simulate_positions.py --tickers US.IBIT,US.AAPL,US.BITO

オプション:
    --tickers CODE[,CODE...]    決済対象の銘柄コード（必須・カンマ区切り、例: US.IBIT,US.AAPL）。
                                安全のため「全建玉一括」は提供せず、明示指定を必須とする。
    --market {US,HK}            対象市場（デフォルト: US）
    --trd-env {SIMULATE,REAL}   取引環境（デフォルト: SIMULATE）
    --host HOST                 OpenD ホスト（デフォルト: 127.0.0.1）
    --port PORT                 OpenD ポート（デフォルト: 11111）
    --dry-run                   売り注文を出さず、対象建玉を表示のみ

処理の仕組み:
    position_list_query で現在の保有建玉を取得し、--tickers に一致しかつ
    can_sell_qty > 0 の建玉に対して成行 SELL（OrderType.MARKET）を qty=can_sell_qty で発注する。
    成行注文は市場休場中は Unsubmitted のまま約定しないため、約定を伴う決済は
    市場開場時間中の実行を前提とする。

注意:
    - 米国市場が休場（土日・祝日）の時間帯に実行しても成行注文は約定しない。
    - SIMULATE で保有していない銘柄に SELL を出すと moomoo 側でキャンセルされる
      （本スクリプトは保有建玉のみ対象にするのでその心配はない）。

REAL 環境への誤適用防止:
    --trd-env=REAL を指定した場合、環境変数 CONFIRM_REAL=1 が設定されていない
    限り中止する。本番口座の建玉を成行決済する破壊的操作なので、明示的な意思確認を
    必須にしている。

終了コード:
    0  正常終了（または対象 0 件）
    1  REAL 環境の確認不足 / 引数不正
    2  position_list_query 失敗
    3  1 件以上の建玉で SELL 発注失敗
"""
from __future__ import annotations

import argparse
import os
import sys

from moomoo import (  # type: ignore[import-not-found]
    OpenSecTradeContext,
    OrderType,
    RET_OK,
    TrdEnv,
    TrdMarket,
    TrdSide,
)

_MARKET_MAP = {
    "US": TrdMarket.US,
    "HK": TrdMarket.HK,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="moomoo SIMULATE の指定建玉を成行 SELL で決済する"
    )
    parser.add_argument(
        "--tickers",
        required=True,
        help="決済対象の銘柄コード（カンマ区切り、必須。例: US.IBIT,US.AAPL,US.BITO）",
    )
    parser.add_argument(
        "--market", choices=["US", "HK"], default="US", help="対象市場（デフォルト: US）"
    )
    parser.add_argument(
        "--trd-env",
        choices=["SIMULATE", "REAL"],
        default="SIMULATE",
        help="取引環境（デフォルト: SIMULATE）",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="OpenD ホスト（デフォルト: 127.0.0.1）"
    )
    parser.add_argument(
        "--port", type=int, default=11111, help="OpenD ポート（デフォルト: 11111）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="売り注文を出さず、対象建玉を表示のみ",
    )
    return parser.parse_args()


def _sell_to_close(ctx, code: str, qty: float, trd_env) -> tuple[bool, str]:
    """指定建玉を成行 SELL で決済する。成功時 (True, order_id)、失敗時 (False, error)。"""
    ret, data = ctx.place_order(
        price=0,
        qty=qty,
        code=code,
        trd_side=TrdSide.SELL,
        order_type=OrderType.MARKET,
        trd_env=trd_env,
    )
    if ret != RET_OK:
        return False, str(data)
    order_id = "?"
    try:
        if hasattr(data, "empty") and not data.empty and "order_id" in data.columns:
            order_id = str(data["order_id"].iloc[0])
    except Exception:  # noqa: BLE001 - order_id 取得失敗は致命的でない
        pass
    return True, order_id


def main() -> int:
    args = parse_args()
    market = _MARKET_MAP[args.market]
    trd_env = TrdEnv.SIMULATE if args.trd_env == "SIMULATE" else TrdEnv.REAL

    target_codes = {t.strip() for t in args.tickers.split(",") if t.strip()}
    if not target_codes:
        print("[ABORT] --tickers に有効な銘柄コードがありません。", file=sys.stderr)
        return 1

    if trd_env == TrdEnv.REAL and os.environ.get("CONFIRM_REAL") != "1":
        print(
            "[ABORT] REAL 環境を選択しました。本番口座の建玉が成行決済されます。",
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
    print(f"[INFO] 決済対象指定: {sorted(target_codes)}")

    ctx = OpenSecTradeContext(
        filter_trdmarket=market, host=args.host, port=args.port
    )
    try:
        ret, positions = ctx.position_list_query(trd_env=trd_env)
        if ret != RET_OK:
            print(f"[ERROR] position_list_query failed: {positions}", file=sys.stderr)
            return 2

        pos_count = 0 if positions is None else len(positions)
        if pos_count == 0:
            print("\n=== 保有建玉 0 件 (決済不要) ===")
            return 0

        # --tickers に一致しかつ売却可能数量 > 0 の建玉だけを対象にする
        targets: list[tuple[str, float]] = []
        for _, row in positions.iterrows():
            code = str(row.get("code", ""))
            if code not in target_codes:
                continue
            can_sell = float(row.get("can_sell_qty", 0) or 0)
            if can_sell <= 0:
                print(f"  - {code}: can_sell_qty={can_sell} のためスキップ")
                continue
            targets.append((code, can_sell))

        if not targets:
            print("\n=== 指定銘柄に該当する売却可能建玉なし (決済不要) ===")
            return 0

        print(f"\n=== 決済対象建玉 ({len(targets)} 件) ===")
        for code, qty in targets:
            print(f"  {code}  SELL MARKET qty={qty}")

        if args.dry_run:
            print("\n[DRY-RUN] 売り注文は実行しません。")
            return 0

        print("\n=== 成行 SELL 発注開始 ===")
        success_count = 0
        failure_count = 0
        for code, qty in targets:
            ok, info = _sell_to_close(ctx, code, qty, trd_env)
            if ok:
                print(f"  ✓ {code} SELL qty={qty} → order_id={info}")
                success_count += 1
            else:
                print(f"  ✗ {code} SELL qty={qty} → 失敗: {info}", file=sys.stderr)
                failure_count += 1

        print(
            f"\n=== 結果: 成功 {success_count} 件 / 失敗 {failure_count} 件 ==="
        )
        print(
            "[NOTE] 成行注文は市場開場時間中のみ約定します。約定状況は "
            "scripts/show_simulate_status.py で確認してください。"
        )
        return 3 if failure_count > 0 else 0
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
