#!/usr/bin/env python3
"""show_simulate_status.py

moomoo OpenD の SIMULATE 環境（または REAL）の口座状態を取得して
人間が読みやすい形で表示するユーティリティスクリプト。

moomoo の OpenAPI SIMULATE 口座は moomoo モバイル/デスクトップアプリの
通常 UI には表示されないため、状況確認は OpenD API 経由で行う必要がある。
本スクリプトはその確認を簡略化する。

VM 側で実行する想定:

    cd ~/dev/alpha-strike
    .venv/bin/python scripts/show_simulate_status.py
    .venv/bin/python scripts/show_simulate_status.py --days 30
    .venv/bin/python scripts/show_simulate_status.py --json | jq .

オプション:
    --market {US,HK,CRYPTO}       対象市場 (デフォルト US)
    --trd-env {SIMULATE,REAL}     取引環境 (デフォルト SIMULATE)
    --host HOST                   OpenD ホスト (デフォルト 127.0.0.1)
    --port PORT                   OpenD ポート (デフォルト 11111)
    --days N                      履歴の表示期間 (デフォルト 7 日)
    --json                        全データを JSON で出力 (機械読み取り用)

出力セクション:
    1. 口座サマリ: 総資産 / 現金 / 買付余力 / 未実現損益 等
    2. ポジション: 保有銘柄一覧
    3. アクティブ注文: pending な未約定注文（status_filter で実 pending のみ）
    4. 直近 N 日の注文履歴: CANCELLED/FILLED 含む全状態
    5. 直近の約定履歴

終了コード:
    0  正常終了 (個別 query 失敗でも終了)
    2  OpenD 接続不能等の致命的エラー (現状未到達、将来用)
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from datetime import datetime, timedelta

try:
    from moomoo import (  # type: ignore[import-not-found]
        OpenSecTradeContext,
        OrderStatus,
        RET_OK,
        TrdEnv,
        TrdMarket,
    )
except ImportError:  # moomoo-api 未導入環境（VM 等）では機能等価の futu-api を使う
    from futu import (  # type: ignore[import-not-found]
        OpenSecTradeContext,
        OrderStatus,
        RET_OK,
        TrdEnv,
        TrdMarket,
    )


@contextlib.contextmanager
def _suppress_stdout():
    """moomoo SDK が起動時に独自ロガーで stdout に書き込むので、
    JSON モード時はそれを /dev/null に redirect する。
    sys.stdout の差し替えに加え、C 拡張からの直接書き込みにも備えて
    OS レベルの fd 1 も devnull に向け直す。
    """
    devnull_path = os.devnull
    saved_stdout = sys.stdout
    saved_fd = os.dup(1)
    devnull_fd = os.open(devnull_path, os.O_WRONLY)
    try:
        # sys.stdout を捨てる
        sys.stdout = open(devnull_path, "w")
        # fd 1 も devnull に
        os.dup2(devnull_fd, 1)
        yield
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
        os.close(devnull_fd)
        sys.stdout.close()
        sys.stdout = saved_stdout

_MARKET_MAP = {
    "US": TrdMarket.US,
    "HK": TrdMarket.HK,
    "CRYPTO": TrdMarket.CRYPTO,
}

PENDING_STATUSES = [
    OrderStatus.WAITING_SUBMIT,
    OrderStatus.SUBMITTING,
    OrderStatus.SUBMITTED,
    OrderStatus.FILLED_PART,
]

# accinfo_query が返す DataFrame から人間向けに抜粋する主要列
ACCINFO_KEY_FIELDS = [
    "total_assets",
    "cash",
    "market_val",
    "power",
    "available_funds",
    "frozen_cash",
    "unrealized_pl",
    "realized_pl",
    "currency",
]

POSITION_KEY_FIELDS = [
    "code",
    "stock_name",
    "qty",
    "can_sell_qty",
    "cost_price",
    "nominal_price",
    "market_val",
    "pl_ratio",
    "pl_val",
    "today_pl_val",
]

ORDER_KEY_FIELDS = [
    "code",
    "trd_side",
    "order_type",
    "qty",
    "price",
    "order_status",
    "order_id",
    "create_time",
    "dealt_qty",
    "dealt_avg_price",
]

DEAL_KEY_FIELDS = [
    "code",
    "trd_side",
    "qty",
    "price",
    "deal_id",
    "order_id",
    "create_time",
    "status",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="moomoo SIMULATE 口座の状態を一覧表示"
    )
    p.add_argument(
        "--market",
        choices=list(_MARKET_MAP.keys()),
        default="US",
        help="対象市場 (デフォルト US)",
    )
    p.add_argument(
        "--trd-env",
        dest="trd_env",
        choices=["SIMULATE", "REAL"],
        default="SIMULATE",
        help="取引環境 (デフォルト SIMULATE)",
    )
    p.add_argument(
        "--host", default="127.0.0.1", help="OpenD ホスト (デフォルト 127.0.0.1)"
    )
    p.add_argument(
        "--port", type=int, default=11111, help="OpenD ポート (デフォルト 11111)"
    )
    p.add_argument(
        "--days",
        type=int,
        default=7,
        help="注文/約定履歴の表示期間 (デフォルト 7 日)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="全データを JSON で出力 (機械読み取り用)",
    )
    return p.parse_args()


def _df_to_records(df, key_fields: list[str] | None = None) -> list[dict]:
    """DataFrame を辞書のリストに変換。

    key_fields が指定されていれば、df に存在する列だけ抜粋。
    None や空 DataFrame の場合は空リストを返す。
    """
    if df is None or len(df) == 0:
        return []
    if key_fields:
        cols = [c for c in key_fields if c in df.columns]
        df = df[cols]
    return df.to_dict(orient="records")


def _safe_query(
    label: str,
    fn,
    *args,
    warnings_sink: list[str] | None = None,
    quiet: bool = False,
    **kwargs,
) -> tuple[bool, object]:
    """OpenD クエリを呼び出し、ret/data を返す。

    失敗時のメッセージは:
      - warnings_sink が渡されていればそこに append (JSON 出力に含める用)
      - quiet=False のときは sys.stderr にも `[WARN] ...` 形式で表示
    JSON 出力時に stderr WARN が ssh/pipe 経由で stdout と混ざらないよう、
    呼び出し側で `quiet=args.json` を渡す。
    """
    ret, data = fn(*args, **kwargs)
    if ret != RET_OK:
        msg = f"{label} 失敗: {data}"
        if warnings_sink is not None:
            warnings_sink.append(msg)
        if not quiet:
            print(f"[WARN] {msg}", file=sys.stderr)
        return False, data
    return True, data


def _print_section_header(title: str) -> None:
    print()
    print("=" * 60)
    print(f" {title}")
    print("=" * 60)


def _print_records(records: list[dict], empty_msg: str, limit: int = 20) -> None:
    if not records:
        print(f"  {empty_msg}")
        return
    for r in records[:limit]:
        # 短く整形
        parts = [f"{k}={v}" for k, v in r.items()]
        print("  " + ", ".join(parts))
    if len(records) > limit:
        print(f"  ... ({len(records) - limit} 件省略)")


def main() -> int:
    args = parse_args()
    market = _MARKET_MAP[args.market]
    trd_env = TrdEnv.SIMULATE if args.trd_env == "SIMULATE" else TrdEnv.REAL

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=args.days)
    start_str = start_dt.strftime("%Y-%m-%d 00:00:00")
    end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    # warnings は JSON 出力にも含めるため result に集約。
    # JSON モード時は stderr への WARN を抑制 (ssh/pipe で stdout と混ざる罠を避ける)。
    warnings: list[str] = []
    result: dict = {
        "meta": {
            "market": args.market,
            "trd_env": args.trd_env,
            "days": args.days,
            "start": start_str,
            "end": end_str,
        },
        "warnings": warnings,
    }
    common_kwargs = {"warnings_sink": warnings, "quiet": args.json}

    # moomoo SDK は OpenSecTradeContext 生成と各クエリの過程で独自ロガーを
    # 介して stdout にタイムスタンプログを書き出すため、JSON モード時はそれを
    # /dev/null に redirect しないと出力が valid JSON にならない。
    stdout_suppressor = _suppress_stdout() if args.json else contextlib.nullcontext()
    with stdout_suppressor:
        ctx = OpenSecTradeContext(
            filter_trdmarket=market, host=args.host, port=args.port
        )
        try:
            ok, info = _safe_query(
                "accinfo_query",
                ctx.accinfo_query,
                trd_env=trd_env,
                **common_kwargs,
            )
            result["accinfo"] = (
                _df_to_records(info, ACCINFO_KEY_FIELDS) if ok else []
            )

            ok, pos = _safe_query(
                "position_list_query",
                ctx.position_list_query,
                trd_env=trd_env,
                **common_kwargs,
            )
            result["positions"] = (
                _df_to_records(pos, POSITION_KEY_FIELDS) if ok else []
            )

            ok, pending = _safe_query(
                "order_list_query (pending)",
                ctx.order_list_query,
                trd_env=trd_env,
                status_filter_list=PENDING_STATUSES,
                **common_kwargs,
            )
            result["pending_orders"] = (
                _df_to_records(pending, ORDER_KEY_FIELDS) if ok else []
            )

            ok, orders = _safe_query(
                "order_list_query (recent)",
                ctx.order_list_query,
                trd_env=trd_env,
                start=start_str,
                end=end_str,
                **common_kwargs,
            )
            result["recent_orders"] = (
                _df_to_records(orders, ORDER_KEY_FIELDS) if ok else []
            )

            ok, deals = _safe_query(
                "deal_list_query",
                ctx.deal_list_query,
                trd_env=trd_env,
                **common_kwargs,
            )
            result["recent_deals"] = (
                _df_to_records(deals, DEAL_KEY_FIELDS) if ok else []
            )
        finally:
            ctx.close()
            # moomoo SDK の close は非同期で on_disconnect コールバックが
            # 走り、内部ロガーが stdout に追加で書き込む。with を抜けて
            # stdout を復元する前にコールバック完了を待つ必要がある。
            # 0.5 秒は実測で十分（ローカル loopback の TCP close 経路）。
            if args.json:
                time.sleep(0.5)

    if args.json:
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return 0

    print(
        f"=== moomoo 口座状態 (market={args.market} trd_env={args.trd_env}) ==="
    )
    print(f"取得日時: {end_str}, 履歴期間: 過去 {args.days} 日")

    _print_section_header("口座サマリ")
    if result["accinfo"]:
        for k, v in result["accinfo"][0].items():
            print(f"  {k:20} = {v}")
    else:
        print("  (取得失敗)")

    _print_section_header(f"ポジション ({len(result['positions'])} 件)")
    _print_records(result["positions"], "(なし)")

    _print_section_header(
        f"アクティブ注文 ({len(result['pending_orders'])} 件、未約定)"
    )
    _print_records(result["pending_orders"], "(なし)")

    _print_section_header(
        f"直近 {args.days} 日の注文履歴 ({len(result['recent_orders'])} 件)"
    )
    _print_records(result["recent_orders"], "(なし)")

    _print_section_header(f"直近の約定履歴 ({len(result['recent_deals'])} 件)")
    _print_records(result["recent_deals"], "(なし)")

    # 警告（API が一部 reject されたケース、例: SIMULATE での deal_list_query）
    if warnings:
        _print_section_header(f"警告 ({len(warnings)} 件)")
        for w in warnings:
            print(f"  {w}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
