"""バージョンメタ（_meta.json）出力のテスト。

alpha-visualizer は `alpha-forge live sync-events` の rsync 経由でこのファイルを
読み、メンテナンス画面へ alpha-strike のバージョンを表示する。
"""
from __future__ import annotations

import json
import pathlib

from alpha_strike.event_logger import JsonlEventLogger


def test_write_version_metaがバージョンを書き出す(tmp_path: pathlib.Path) -> None:
    JsonlEventLogger(tmp_path).write_version_meta("1.0.4")
    payload = json.loads((tmp_path / "_meta.json").read_text(encoding="utf-8"))
    assert payload["component"] == "alpha-strike"
    assert payload["version"] == "1.0.4"
    assert payload["started_at"]


def test_meta_jsonはload_eventsに混ざらない(tmp_path: pathlib.Path) -> None:
    """_meta.json がイベント走査に混入すると取り込みが壊れる（設計 §3 の前提）。

    load_events は `glob("*.jsonl")` なので `.json` は対象外だが、将来この
    glob が `*.json*` などへ緩められると alpha-strike と alpha-forge の
    両方のイベント取り込みが同時に壊れる。ここで固定する。
    """
    logger = JsonlEventLogger(tmp_path)
    logger.write_version_meta("1.0.4")
    (tmp_path / "2026-08-10.moomoo.jsonl").write_text(
        '{"event_type": "signal", "event_id": "e1"}\n', encoding="utf-8"
    )
    events = logger.load_events()
    assert len(events) == 1
    assert events[0]["event_id"] == "e1"


def test_書き込み失敗は例外を投げない(tmp_path: pathlib.Path) -> None:
    """バージョン表示は補助情報。発注サーバーの起動を止めてはいけない。"""
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("", encoding="utf-8")
    # ファイルを親ディレクトリとして扱わせ、mkdir を失敗させる
    target = blocked / "events"
    JsonlEventLogger(target).write_version_meta("1.0.4")
    # 例外を投げないだけでなく、中途半端なファイルも残さない
    assert not (target / "_meta.json").exists()
