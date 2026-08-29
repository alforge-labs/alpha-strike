"""alpha-strike CLI エントリポイント.

PyPI からインストール後、以下のコマンドで Webhook サーバーを起動できる：

    alpha-strike                       # 既定: 0.0.0.0:8080
    alpha-strike --host 127.0.0.1      # ホスト指定
    alpha-strike --port 9000           # ポート指定
    alpha-strike --version             # バージョン表示

シグナル途絶監視は別の console script として単発実行する（systemd timer から毎時呼ぶ）：

    alpha-strike-watchdog               # 1 回だけ実行して終了（常駐しない）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn
from dotenv import load_dotenv

from alpha_strike import __version__
from alpha_strike.event_logger import JsonlEventLogger
from alpha_strike.services.notifier import NtfyNotifier
from alpha_strike.services.signal_watchdog import (
    get_signal_watchdog_broker,
    get_signal_watchdog_renotify_hours,
    get_signal_watchdog_threshold_hours,
    is_signal_watchdog_enabled,
    load_watchdog_state,
    run_signal_watchdog_once,
)

logger = logging.getLogger(__name__)


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
    print(
        "記録したライブイベントは alpha-visualizer の Live 画面で可視化できます"
        "（alpha-forge live sync-events → import-events / replay で取込）",
        flush=True,
    )
    uvicorn.run(
        "alpha_strike.webhook_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def watchdog_main() -> int:
    """シグナル途絶監視の単発実行。systemd timer から毎時呼ばれる。

    プロセス内の常駐ループではなく別プロセスにすることで、alpha-strike 本体の
    イベントループが OpenD の同期呼び出しで凍結しても、またプロセスが落ちても、
    監視だけは独立して動き続ける（2026-08-23 の障害はこれが無くて 5 営業日気づけなかった）。

    引数は取らない。設定は ``SIGNAL_WATCHDOG_*`` 環境変数から読む。cwd の ``.env`` を
    ``webhook_server.py`` と同じ方法で読み込む（呼び忘れると本体プロセスとは別の
    ``LIVE_EVENTS_PATH`` を見てしまい、``find_last_signal`` が (None, None) を返して
    fail-safe で永久に沈黙する — 監視しているつもりで何も見ていない状態になる）。

    Returns:
        常に 0。途絶したかどうかは通知とイベントログで表現する。非ゼロにすると systemd が
        timer を failed 扱いにし、「監視が動いている」ことと「途絶している」ことの区別が
        つかなくなるため。
    """
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        if not is_signal_watchdog_enabled():
            logger.info("signal watchdog は無効化されています")
            return 0
        notifier = NtfyNotifier()
        if not notifier.enabled:
            logger.warning(
                "NTFY_TOPIC 未設定のため通知は飛びません（検知イベントの記録のみ行います）"
            )
        event_logger = JsonlEventLogger()
        broker = get_signal_watchdog_broker()
        state = load_watchdog_state(event_logger, broker=broker)
        run_signal_watchdog_once(
            event_logger=event_logger,
            notifier=notifier,
            state=state,
            threshold_hours=get_signal_watchdog_threshold_hours(),
            renotify_hours=get_signal_watchdog_renotify_hours(),
            broker=broker,
        )
    except Exception as exc:  # noqa: BLE001 — timer の次回実行を止めない
        logger.warning("signal watchdog の実行でエラー: %s", exc)
    return 0


if __name__ == "__main__":  # pragma: no cover - python -m alpha_strike 実行時
    sys.exit(main())
