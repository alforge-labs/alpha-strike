"""JSON Lines 形式で live trading イベントを保存する。"""

from __future__ import annotations

import json
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

    def load_events(
        self,
        *,
        broker: str | None = None,
        event_type: str | None = None,
        ticker: str | None = None,
        strategy_id: str | None = None,
        limit: int | None = 200,
    ) -> list[dict]:
        """保存済み JSONL event を新しい順で読む。"""
        events: list[dict] = []
        try:
            base_path = self._resolve_base_path()
            if not base_path.exists():
                return []
            files = sorted(base_path.glob("*.jsonl"), reverse=True)
            for path in files:
                if broker and not path.name.endswith(f".{broker}.jsonl"):
                    continue
                for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                    if not line.strip():
                        continue
                    payload = json.loads(line)
                    if event_type and payload.get("event_type") != event_type:
                        continue
                    if ticker and payload.get("ticker") != ticker:
                        continue
                    if strategy_id and payload.get("strategy_id") != strategy_id:
                        continue
                    events.append(payload)
                    if limit is not None and len(events) >= limit:
                        return events
        except Exception as exc:
            logger.warning("イベントログ読込に失敗しました: %s", exc)
        return events
