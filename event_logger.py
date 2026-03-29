"""JSON Lines 形式で live trading イベントを保存する。"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class JsonlEventLogger:
    """JSONL ファイルにイベントを書き出す軽量ロガー。"""

    def __init__(self, base_path: str | Path | None = None) -> None:
        self.base_path = Path(base_path) if base_path is not None else None

    def _resolve_base_path(self) -> Path:
        env_path = os.getenv("LIVE_EVENTS_PATH")
        return self.base_path or Path(env_path or "./data/live/events")

    def append(self, event: BaseModel) -> None:
        """イベントを日次・ブローカー別 JSONL に追記する。失敗時は警告ログのみ残す。"""
        try:
            occurred_at = getattr(event, "occurred_at", datetime.now())
            broker = getattr(event, "broker", "unknown")
            day = occurred_at.strftime("%Y-%m-%d")
            path = self._resolve_base_path() / f"{day}.{broker}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(event.model_dump_json())
                fp.write("\n")
        except Exception as exc:
            logger.warning("イベントログ保存に失敗しました: %s", exc)
