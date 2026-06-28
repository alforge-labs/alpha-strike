"""alpha-strike CLI エントリポイント.

PyPI からインストール後、以下のコマンドで Webhook サーバーを起動できる：

    alpha-strike                       # 既定: 0.0.0.0:8080
    alpha-strike --host 127.0.0.1      # ホスト指定
    alpha-strike --port 9000           # ポート指定
    alpha-strike --version             # バージョン表示
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from alpha_strike import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alpha-strike",
        description=(
            "alpha-strike — TradingView Webhook を moomoo / OANDA へ"
            "ルーティングする FastAPI サーバー"
        ),
    )
    parser.add_argument(
        "--host",
        default=os.getenv("ALPHA_STRIKE_HOST", "0.0.0.0"),
        help="バインドするホスト (既定: 0.0.0.0、環境変数 ALPHA_STRIKE_HOST でも上書き可)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("ALPHA_STRIKE_PORT", "8080")),
        help="バインドするポート (既定: 8080、環境変数 ALPHA_STRIKE_PORT でも上書き可)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="開発時のホットリロードを有効化",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI エントリポイント. uvicorn でサーバーを起動する。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    print(
        "alpha-strike — Powered by AlphaForge（戦略の作成・最適化・WFT は https://alforgelabs.com）",
        flush=True,
    )
    uvicorn.run(
        "alpha_strike.webhook_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - python -m alpha_strike 実行時
    sys.exit(main())
