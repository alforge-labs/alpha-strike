# OpenD 障害耐性の恒久対策 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** OpenD 障害時に取引受付が止まらず、監視も黙らない状態にする。

**Architecture:** 3 つの独立した変更を行う。(1) signal watchdog を systemd timer による別プロセスへ移し、再通知抑制の状態はイベントログから復元する。(2) `receive_webhook` と `get_status` の宣言を `async def` から `def` に変え、FastAPI のスレッドプールで実行させることで OpenD 呼び出しをイベントループから外す。原子性はモジュールレベルの `threading.Lock` で維持する。(3) systemd を `Wants=` / `Restart=always` に変更する。

**Tech Stack:** Python 3 / FastAPI / Pydantic / asyncio / systemd / pytest / ruff / uv

設計書: [`docs/superpowers/specs/2026-08-29-opend-resilience-design.md`](../specs/2026-08-29-opend-resilience-design.md)

## Global Constraints

- 作業ディレクトリは `/Users/sakae/dev/alpha-trade/.claude/worktrees/opend-resilience`（ブランチ `fix/opend-resilience`）。
- コード内コメント・docstring・コミットメッセージはすべて**日本語**で書く。
- コミットは **Conventional Commits**（`feat:` / `fix:` / `refactor:` / `test:` / `docs:` / `chore:`）。`CHANGELOG.md` は手で編集しない（`release.sh` が git-cliff で再生成する）。
- **依存パッケージを追加しない。** `pyproject.toml` の `dependencies` は現在 7 個（fastapi / futu-api / python-dotenv / requests / slowapi / tenacity / uvicorn）のまま変更しない。
- Python の実行はすべて `uv run`（`pip` / `poetry` は使わない）。
- すべての関数シグネチャに型注釈を付ける。`Any` は既存コードが `event_logger` / `notifier` に使っている箇所のみ踏襲する。
- **`datetime` は naive（JST）で扱う。** tz-aware にしない。
- 状態を持つ値は `@dataclass(frozen=True)` にし、更新は新インスタンスを返す。
- **非同期テストのマーカーは `@pytest.mark.anyio`**（`@pytest.mark.asyncio` ではない）。各テストファイルに `anyio_backend` フィクスチャ（`return "asyncio"`）が必要。
- 既存の `pending_reconcile.py` / `carryover.py` のスタイル（docstring の書き方、エラーの握り方）に合わせる。
- **systemd unit ファイルはリポジトリに置かない。** 既存 unit も VM 上にしか無く、`docs/ops/` に記述する慣習に従う。
- 各タスクの最後に `uv run pytest` と `uv run ruff check .` を通す。alpha-strike の PR には CI が走るが、ローカルゲートも必ず通す。

---

### Task 1: `load_watchdog_state` — 状態をイベントログから復元する

別プロセス化すると周回間の記憶を持てないため、再通知抑制の状態を自分が書いたイベントから復元する。新しい状態ファイルは作らない。

**Files:**
- Modify: `src/alpha_strike/services/signal_watchdog.py`（`SignalWatchdogState` 定義の直後に追加）
- Modify: `tests/test_signal_watchdog.py`

**Interfaces:**
- Consumes: `SignalWatchdogState`（`last_notified_at: datetime | None = None`, `in_outage: bool = False`）、`DEFAULT_BROKER = "moomoo"`、`event_logger.load_events(*, broker, event_type, limit) -> list[dict]`
- Produces: `load_watchdog_state(event_logger: Any, *, broker: str = DEFAULT_BROKER) -> SignalWatchdogState`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_signal_watchdog.py` の末尾に追記する。既存のヘルパー `_logger_with(events)` を再利用する。

```python
def _outage_event(
    outage_state: str = "detected",
    occurred_at: str = "2026-08-27T06:35:25.893709",
) -> dict:
    return {
        "event_type": "signal_outage_detected",
        "event_id": "evt_1",
        "occurred_at": occurred_at,
        "broker": "moomoo",
        "outage_state": outage_state,
        "last_signal_at": "2026-08-22T05:01:02.781919",
        "last_signal_id": "20260821-093000",
        "effective_hours": 78.59,
        "threshold_hours": 60.0,
    }


class TestLoadWatchdogState:
    """WHY: 別プロセス実行の watchdog は周回間の記憶を持てない。自分が書いたイベントを
    唯一の状態ソースにすることで、再通知抑制がプロセスをまたいで働く。ここが壊れると
    1 時間ごとに通知が飛び続け、通知を無視するようになって監視自体が死ぬ。"""

    def test_最新がdetectedなら途絶中として復元する(self):
        logger = _logger_with([_outage_event("detected")])
        state = load_watchdog_state(logger, broker="moomoo")
        assert state.in_outage is True
        assert state.last_notified_at == datetime(2026, 8, 27, 6, 35, 25, 893709)
        logger.load_events.assert_called_once_with(
            broker="moomoo", event_type="signal_outage_detected", limit=1
        )

    def test_最新がrecoveredなら初期状態(self):
        state = load_watchdog_state(_logger_with([_outage_event("recovered")]))
        assert state.in_outage is False
        assert state.last_notified_at is None

    def test_イベントが0件なら初期状態(self):
        state = load_watchdog_state(_logger_with([]))
        assert state.in_outage is False
        assert state.last_notified_at is None

    def test_occurred_atが壊れていても例外を投げず初期状態(self):
        """パース失敗で例外を投げると timer 実行が failed になり監視が止まる。"""
        logger = _logger_with([_outage_event("detected", occurred_at="not-a-date")])
        state = load_watchdog_state(logger)
        assert state.in_outage is False
        assert state.last_notified_at is None

    def test_復旧後に再検知した場合は最新のdetectedを使う(self):
        """load_events は新しい順に返す。先頭 1 件だけを見れば足りることを固定する。"""
        logger = _logger_with(
            [
                _outage_event("detected", occurred_at="2026-08-29T12:00:00"),
                _outage_event("recovered", occurred_at="2026-08-28T05:05:00"),
            ]
        )
        state = load_watchdog_state(logger)
        assert state.in_outage is True
        assert state.last_notified_at == datetime(2026, 8, 29, 12, 0, 0)
```

`tests/test_signal_watchdog.py` の import に `load_watchdog_state` を追加する。

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_signal_watchdog.py -k LoadWatchdogState -v`
Expected: FAIL — `ImportError: cannot import name 'load_watchdog_state'`

- [ ] **Step 3: 実装する**

`src/alpha_strike/services/signal_watchdog.py` の `SignalWatchdogState` クラス定義の直後（`_emit` の定義より前）に追加する。

```python
def load_watchdog_state(
    event_logger: Any, *, broker: str = DEFAULT_BROKER
) -> SignalWatchdogState:
    """最新の ``signal_outage_detected`` から再通知抑制の状態を復元する。

    単発実行の watchdog はプロセスをまたいで状態を持てないため、自分が書いたイベントを
    唯一の状態ソースとして使う。新しい状態ファイルは作らない。

    ``load_events`` は新しい順に返すので先頭 1 件を見れば足りる。``recovered``・0 件・
    パース失敗はいずれも初期状態を返す（誤報より沈黙を選ぶ既存方針を踏襲）。ここで例外を
    投げると systemd timer が failed になり監視が止まる。
    """
    events = event_logger.load_events(
        broker=broker, event_type="signal_outage_detected", limit=1
    )
    if not events:
        return SignalWatchdogState()
    event = events[0]
    if str(event.get("outage_state")) != "detected":
        return SignalWatchdogState()
    try:
        occurred_at = datetime.fromisoformat(str(event.get("occurred_at")))
    except (TypeError, ValueError):
        logger.warning(
            "signal_outage_detected の occurred_at をパースできませんでした"
        )
        return SignalWatchdogState()
    return SignalWatchdogState(last_notified_at=occurred_at, in_outage=True)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_signal_watchdog.py -k LoadWatchdogState -v`
Expected: PASS（5 件）

- [ ] **Step 5: フルゲート**

Run: `uv run pytest && uv run ruff check .`
Expected: 全件 PASS・lint クリーン

- [ ] **Step 6: コミット**

```bash
git add src/alpha_strike/services/signal_watchdog.py tests/test_signal_watchdog.py
git commit -m "feat: watchdog の状態をイベントログから復元できるようにする

別プロセス化すると周回間の記憶を持てないため、自分が書いた signal_outage_detected を唯一の状態ソースにする。新しい状態ファイルは作らない。

副作用として現行より改善する。今はメモリ保持のため再起動で状態が消え、再通知抑制が無効化されて即座に鳴り直していた。イベントログ由来なら再起動を跨いで引き継がれる。"
```

---

### Task 2: `alpha-strike-watchdog` — 単発実行の console script

**Files:**
- Modify: `src/alpha_strike/cli.py`
- Modify: `pyproject.toml`（`[project.scripts]`、43-44 行付近）
- Create: `tests/test_cli_watchdog.py`

**Interfaces:**
- Consumes: `load_watchdog_state`（Task 1）、`run_signal_watchdog_once` / `is_signal_watchdog_enabled` / `get_signal_watchdog_broker` / `get_signal_watchdog_threshold_hours` / `get_signal_watchdog_renotify_hours`（既存）、`JsonlEventLogger`（`alpha_strike.event_logger`）、`NtfyNotifier`（`alpha_strike.services.notifier`）
- Produces: `watchdog_main(argv: list[str] | None = None) -> int`（**常に 0 を返す**）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_cli_watchdog.py` を新規作成する。

```python
"""alpha-strike-watchdog（単発実行 CLI）のテスト。

WHY: systemd timer から呼ばれる。終了コードが非ゼロだと timer が failed 扱いになり、
「監視が動いている」ことと「途絶している」ことの区別がつかなくなる。途絶の有無は通知と
イベントログで表現し、終了コードには乗せない、という契約をここで固定する。
"""

from __future__ import annotations

import pytest

from alpha_strike.cli import watchdog_main


class TestWatchdogMain:
    def test_無効化されていれば何もせず0を返す(self, monkeypatch):
        called: list[int] = []
        monkeypatch.setenv("SIGNAL_WATCHDOG_ENABLED", "0")
        monkeypatch.setattr(
            "alpha_strike.cli.run_signal_watchdog_once",
            lambda **kw: called.append(1),
        )
        assert watchdog_main([]) == 0
        assert called == []

    def test_途絶を検知しても0を返す(self, monkeypatch):
        """検知は通知とイベントログで表現する。終了コードには乗せない。"""
        monkeypatch.setenv("SIGNAL_WATCHDOG_ENABLED", "1")
        monkeypatch.setattr(
            "alpha_strike.cli.load_watchdog_state", lambda *a, **kw: object()
        )
        monkeypatch.setattr(
            "alpha_strike.cli.run_signal_watchdog_once", lambda **kw: object()
        )
        assert watchdog_main([]) == 0

    def test_例外が起きても0を返す(self, monkeypatch):
        """timer の次回実行を止めないため、失敗しても 0 で終わる。"""
        monkeypatch.setenv("SIGNAL_WATCHDOG_ENABLED", "1")

        def _boom(*a, **kw):
            raise RuntimeError("イベントログ読込失敗")

        monkeypatch.setattr("alpha_strike.cli.load_watchdog_state", _boom)
        assert watchdog_main([]) == 0

    def test_run_signal_watchdog_onceへ設定値が渡る(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setenv("SIGNAL_WATCHDOG_ENABLED", "1")
        monkeypatch.setenv("SIGNAL_WATCHDOG_THRESHOLD_HOURS", "72")
        monkeypatch.setenv("SIGNAL_WATCHDOG_RENOTIFY_HOURS", "12")
        monkeypatch.setenv("SIGNAL_WATCHDOG_BROKER", "moomoo")
        monkeypatch.setattr(
            "alpha_strike.cli.load_watchdog_state", lambda *a, **kw: "STATE"
        )
        monkeypatch.setattr(
            "alpha_strike.cli.run_signal_watchdog_once",
            lambda **kw: captured.update(kw),
        )
        assert watchdog_main([]) == 0
        assert captured["threshold_hours"] == 72.0
        assert captured["renotify_hours"] == 12.0
        assert captured["broker"] == "moomoo"
        assert captured["state"] == "STATE"
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_cli_watchdog.py -v`
Expected: FAIL — `ImportError: cannot import name 'watchdog_main'`

- [ ] **Step 3: `cli.py` に実装する**

`src/alpha_strike/cli.py` の import 群に追加する。

```python
import logging

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
```

ファイル末尾（`if __name__ == "__main__":` ブロックより前）に追加する。

```python
def watchdog_main(argv: list[str] | None = None) -> int:
    """シグナル途絶監視の単発実行。systemd timer から毎時呼ばれる。

    プロセス内の常駐ループではなく別プロセスにすることで、alpha-strike 本体の
    イベントループが OpenD の同期呼び出しで凍結しても、またプロセスが落ちても、
    監視だけは独立して動き続ける（2026-08-23 の障害はこれが無くて 5 営業日気づけなかった）。

    引数は取らない。設定は ``SIGNAL_WATCHDOG_*`` 環境変数から読む。

    Returns:
        常に 0。途絶したかどうかは通知とイベントログで表現する。非ゼロにすると systemd が
        timer を failed 扱いにし、「監視が動いている」ことと「途絶している」ことの区別が
        つかなくなるため。
    """
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
```

- [ ] **Step 4: `pyproject.toml` にエントリポイントを追加**

43-44 行付近を書き換える。変更前:

```toml
[project.scripts]
alpha-strike = "alpha_strike.cli:main"
```

変更後:

```toml
[project.scripts]
alpha-strike = "alpha_strike.cli:main"
alpha-strike-watchdog = "alpha_strike.cli:watchdog_main"
```

**既存の `alpha-strike` 行は変更しない。** systemd の `ExecStart=/opt/alpha-strike/.venv/bin/alpha-strike --host 0.0.0.0 --port 8080` がこの形に依存している。

- [ ] **Step 5: テストが通ることを確認**

Run: `uv run pytest tests/test_cli_watchdog.py -v`
Expected: PASS（4 件）

- [ ] **Step 6: 実際にエントリポイントが生えることを確認**

Run: `uv sync && uv run alpha-strike-watchdog`
Expected: 終了コード 0。`SIGNAL_WATCHDOG_ENABLED` 未設定（既定 ON）かつ `NTFY_TOPIC` 未設定なら「NTFY_TOPIC 未設定のため通知は飛びません」の警告が出る。異常終了しないこと。

- [ ] **Step 7: フルゲート**

Run: `uv run pytest && uv run ruff check .`
Expected: 全件 PASS・lint クリーン

- [ ] **Step 8: コミット**

```bash
git add src/alpha_strike/cli.py pyproject.toml tests/test_cli_watchdog.py
git commit -m "feat: alpha-strike-watchdog を単発実行の console script として追加

systemd timer から毎時呼ぶ。本体のイベントループが OpenD で凍結してもプロセスが落ちても、監視だけは独立して動き続ける。

サブコマンドではなく独立したエントリポイントにした。現行 CLI は argparse のフラットなパーサで、systemd の ExecStart がその形に依存しているため、サブパーサを導入すると本番の起動コマンドを壊すリスクがある。

終了コードは常に 0 にした。非ゼロだと systemd が timer を failed 扱いにし、「監視が動いている」ことと「途絶している」ことの区別がつかなくなる。"
```

---

### Task 3: プロセス内の常駐ループを撤去する

Task 2 で置き換えが用意できたので、重複する常駐ループを取り除く。

**Files:**
- Modify: `src/alpha_strike/services/signal_watchdog.py`（`signal_watchdog_loop` を削除、294-326 行）
- Modify: `src/alpha_strike/webhook_server.py`（import 75-82 行・lifespan 235-258 行・shutdown 273-276 行）
- Modify: `tests/test_signal_watchdog.py`（`TestSignalWatchdogLoop` を削除、361 行以降）

**Interfaces:**
- Consumes: なし
- Produces: なし（削除のみ）

- [ ] **Step 1: ループのテストを削除する**

`tests/test_signal_watchdog.py` の `class TestSignalWatchdogLoop:`（361 行）からファイル末尾（409 行）までを削除する。このクラスには `test_起動直後に実行しintervalごとに繰り返しcancelで終わる` と `test_例外が出てもループは止まらない` の 2 件がある。

併せて不要になる import を削除する。ファイル冒頭の `import asyncio` と、`signal_watchdog_loop` の import を削る（`asyncio` が他で使われていないことを確認すること）。

- [ ] **Step 2: `signal_watchdog_loop` を削除する**

`src/alpha_strike/services/signal_watchdog.py` の `async def signal_watchdog_loop(`（294 行）からファイル末尾（326 行）までを削除する。

併せてファイル冒頭の `import asyncio` を削除する（このモジュールでは `signal_watchdog_loop` 以外に `asyncio` を使っていない）。

- [ ] **Step 3: lifespan の配線を削除する**

`src/alpha_strike/webhook_server.py` を 3 箇所修正する。

(a) import（75-82 行）から `signal_watchdog_loop` を削除し、他の 5 つも使われなくなるので **import ブロックごと削除**する。

削除する行:

```python
from alpha_strike.services.signal_watchdog import (
    get_signal_watchdog_broker,
    get_signal_watchdog_interval,
    get_signal_watchdog_renotify_hours,
    get_signal_watchdog_threshold_hours,
    is_signal_watchdog_enabled,
    signal_watchdog_loop,
)
```

(b) lifespan 内の watchdog ブロック（235-258 行）を削除する。`# シグナル途絶 watchdog:` で始まるコメントから `logger.info("signal watchdog 有効 (interval=%ss)", sw_interval)` までが対象。直後の `# 同期済み _meta.json から alpha-visualizer がバージョンを読む` は残す。

(c) shutdown 側（273-276 行）を削除する。

```python
    if app.state.signal_watchdog_task is not None:
        app.state.signal_watchdog_task.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.signal_watchdog_task
```

- [ ] **Step 4: 参照が残っていないことを確認**

Run: `grep -rn "signal_watchdog_loop\|signal_watchdog_task\|get_signal_watchdog_interval" src/ tests/`
Expected: 何も出力されない

`get_signal_watchdog_interval` と `DEFAULT_INTERVAL_SECONDS` は使われなくなるが、**削除しない**。timer の間隔設計の根拠として `SIGNAL_WATCHDOG_INTERVAL_SECONDS` はドキュメントに残り、既存テスト（`test_数値でない間隔は既定へフォールバック`）も参照している。

- [ ] **Step 5: テストが通ることを確認**

Run: `uv run pytest tests/test_signal_watchdog.py -v`
Expected: PASS（32 件 = 既存 29 − ループ 2 + Task 1 の 5）

- [ ] **Step 6: サーバー起動系の回帰を確認**

Run: `uv run pytest tests/test_webhook_server.py tests/test_main.py -v`
Expected: PASS

- [ ] **Step 7: フルゲート**

Run: `uv run pytest && uv run ruff check .`
Expected: 全件 PASS・lint クリーン

- [ ] **Step 8: コミット**

```bash
git add src/alpha_strike/services/signal_watchdog.py src/alpha_strike/webhook_server.py tests/test_signal_watchdog.py
git commit -m "refactor: プロセス内の watchdog 常駐ループを撤去する

alpha-strike-watchdog（systemd timer）へ移したため重複する。判定ロジックは残し、常駐ループと lifespan の配線だけを削除する。

このループは OpenD の同期呼び出しがイベントループを塞ぐと道連れで止まっていた。2026-08-23 の障害では 8/27 21:35 を最後にログが途絶し、5 営業日の停止を通知できなかった。"
```

---

### Task 4: ハンドラを `def` にしてイベントループを解放する

**Files:**
- Modify: `src/alpha_strike/webhook_server.py`（`receive_webhook` 365 行・`get_status` 814 行・モジュールレベルのロック追加）
- Modify: `tests/test_webhook_server.py`

**Interfaces:**
- Consumes: なし
- Produces: `_ORDER_LOCK: threading.Lock`（`webhook_server` モジュールレベル。テストから `from alpha_strike.webhook_server import _ORDER_LOCK` で参照可能）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_webhook_server.py` の末尾に追記する。冒頭の import に `import asyncio`・`import threading`・`import time` を追加する。

```python
class TestEventLoopNotBlocked:
    """WHY: 2026-08-23 の障害では OpenD が画像認証待ちで無限リトライし、async ハンドラ内の
    同期呼び出しがイベントループを凍結させた。/webhook も /status も watchdog も止まり、
    米国 5 営業日の取引が失われた。ハンドラをスレッドプールへ逃がしつつ、発注の原子性
    （現行は 436-560 行に await が無く割り込まれない）は保つ、という 2 点を固定する。"""

    @pytest.mark.anyio
    async def test_発注は直列化される(self, client, monkeypatch):
        """並行実行すると target_reconcile と sell_guard が同じ建玉を二重に読む。"""
        inside = 0
        max_inside = 0

        def _slow_route(payload):
            nonlocal inside, max_inside
            inside += 1
            max_inside = max(max_inside, inside)
            time.sleep(0.05)
            inside -= 1
            return {"order_id": "test-order"}

        monkeypatch.setattr(app.state.order_router, "route", _slow_route)

        async def _post(i: int):
            body = dict(BASE_PAYLOAD, signal_id=f"sig_serial_{i}")
            return await client.post("/webhook", json=body)

        results = await asyncio.gather(_post(1), _post(2), _post(3))

        assert all(r.status_code == 200 for r in results)
        assert max_inside == 1, "発注が並行実行された（直列性が壊れている）"

    @pytest.mark.anyio
    async def test_発注がブロックしてもstatus_eventsは応答する(
        self, client, monkeypatch
    ):
        """イベントループが解放されていることの検証。凍結していればここで固まる。"""
        monkeypatch.setenv("STATUS_API_TOKEN", "test-token")
        started = threading.Event()
        release = threading.Event()

        def _blocking_route(payload):
            started.set()
            release.wait(timeout=5)
            return {"order_id": "test-order"}

        monkeypatch.setattr(app.state.order_router, "route", _blocking_route)

        body = dict(BASE_PAYLOAD, signal_id="sig_block_1")
        task = asyncio.create_task(client.post("/webhook", json=body))
        try:
            await asyncio.to_thread(started.wait, 5)
            resp = await asyncio.wait_for(
                client.get(
                    "/status/events",
                    headers={"Authorization": "Bearer test-token"},
                ),
                timeout=3,
            )
            assert resp.status_code == 200
        finally:
            release.set()
            await task
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_webhook_server.py -k EventLoopNotBlocked -v`
Expected: `test_発注がブロックしてもstatus_eventsは応答する` が `asyncio.TimeoutError` で FAIL する（現在はイベントループが塞がるため `/status/events` が返らない）。`test_発注は直列化される` は現状 PASS する（イベントループが直列化しているため）——このテストは Step 3 の変更で壊れないことを守るための回帰テスト。

- [ ] **Step 3: ロックを追加してハンドラを `def` にする**

`src/alpha_strike/webhook_server.py` を 4 箇所修正する。

(a) import に `threading` を追加する（標準ライブラリの import 群、`import sys` の近く）。

```python
import threading
```

(b) モジュールレベルにロックを定義する。`event_logger = JsonlEventLogger()`（103 行）の直後に置く。

```python
# 発注経路（should_carryover 〜 route）の直列化ロック。
#
# ハンドラを def にすると FastAPI がスレッドプールで並行実行するため、そのままだと
# resolve_target_order (#80) と resolve_sell_quantity (#74) が並行リクエストと同じ建玉を
# 読んでしまう。現行の async 実装は 436-560 行に await が 1 つも無く割り込まれないので、
# その原子性をロックで再現する。
#
# app.state ではなくモジュールレベルに置く。テスト 6 ファイルが lifespan を経由せず
# app.state を直接組み立てており、app.state.order_lock にすると全てに初期化が要る。
# ロックは設定値を持たないプロセス共通の資源なのでこれで足りる。
_ORDER_LOCK = threading.Lock()
```

(c) `receive_webhook`（365 行）の宣言を変える。

変更前:

```python
async def receive_webhook(
```

変更後:

```python
def receive_webhook(
```

**本体は変更しない。** `await` を 1 つも含まないため、宣言を変えるだけで FastAPI がスレッドプールで実行する。

(d) `get_status`（814 行）の宣言を同様に変える。

変更前:

```python
async def get_status(request: Request, trd_env: str | None = Query(default=None)) -> AccountStatus:
```

変更後:

```python
def get_status(request: Request, trd_env: str | None = Query(default=None)) -> AccountStatus:
```

**`get_status_events`（831 行）は `async def` のまま残す。** OpenD に触らずファイル読み取りだけなので、イベントループ上に置いておけばスレッドプールが webhook で埋まっても診断用に応答し続ける。

- [ ] **Step 4: 発注区間をロックで囲む**

`receive_webhook` 内で、`event_logger.append(signal_event)`（436 行）**より後**、`market_state_provider = getattr(...)`（455 行）から発注・イベント記録の完了までを `with _ORDER_LOCK:` で囲む。

**囲む範囲は 455 行から `receive_webhook` の末尾（696 行）まで**（697 行の `@app.post("/events/trade-closed", ...)` デコレータの直前まで）。この範囲を丸ごと 1 段インデントし、先頭に `with _ORDER_LOCK:` を置く。

開始位置（455 行）は次のとおり:

```python
    with _ORDER_LOCK:
        market_state_provider = getattr(request.app.state, "market_state_provider", None)
        if market_state_provider is not None and should_carryover(
            payload, market_state_provider, trd_env=os.getenv("MOOMOO_TRD_ENV", "SIMULATE")
        ):
            return _record_carryover_queued(payload, signal_id=signal_id)
```

範囲内には `return` が複数あるが（carry-over の早期リターン、成功時の `OrderResult`、例外時のレスポンス）、`with` ブロック内からの `return` はロックを正しく解放するので問題ない。`try` / `except` ブロックもそのまま内側に入る。

**ロックは `signal_received` の記録（436 行）より後に置くこと。** OpenD が固まってロック保持者が戻らなくなっても、シグナルの記録だけは全リクエストで成立する必要がある（今回の障害では 5 営業日分が記録すら残らず消えた）。

インデントを機械的に変えるので、以下 3 点を目視で確認してからテストへ進む。

1. `event_logger.append(signal_event)` が `with _ORDER_LOCK:` **より前**にあること
2. `with _ORDER_LOCK:` がファイル内に **1 箇所だけ**であること
3. 関数の末尾（696 行付近）まで一貫して 1 段深くなっていること

Run: `uv run ruff check src/alpha_strike/webhook_server.py`
Expected: 何も出力されない（インデント崩れがあれば構文エラーで検出される）

Run: `grep -n "with _ORDER_LOCK:\|event_logger.append(signal_event)" src/alpha_strike/webhook_server.py`
Expected: `event_logger.append(signal_event)` の行番号が `with _ORDER_LOCK:` より小さいこと

- [ ] **Step 5: テストが通ることを確認**

Run: `uv run pytest tests/test_webhook_server.py -v`
Expected: PASS（新規 2 件を含む全件）

- [ ] **Step 6: webhook 系の回帰を確認**

Run: `uv run pytest tests/test_webhook_carryover.py tests/test_webhook_sell_guard.py tests/test_webhook_target_reconcile.py tests/test_webhook_reconcile_notify.py tests/test_status_api.py -v`
Expected: PASS（`def` 化とインデント変更で既存の webhook 経路が壊れていないこと）

- [ ] **Step 7: フルゲート**

Run: `uv run pytest && uv run ruff check .`
Expected: 全件 PASS・lint クリーン

- [ ] **Step 8: コミット**

```bash
git add src/alpha_strike/webhook_server.py tests/test_webhook_server.py
git commit -m "fix: OpenD の同期呼び出しでイベントループが凍結するのを解消する

receive_webhook と get_status はどちらも async def だが本体に await が 1 つも無く、OpenD への同期呼び出し 5 箇所がイベントループ上で実行されていた。OpenD が画像認証待ちで無限リトライすると 1 回の呼び出しでループ全体が凍結し、/webhook も /status も常駐タスクも止まる。2026-08-23 の障害では米国 5 営業日の取引が失われた。

FastAPI は非 async の def エンドポイントをスレッドプールで実行するため、宣言を変えるだけで本体を触らずに 5 箇所すべてが外れる。

原子性は threading.Lock で維持する。現行は 436-560 行に await が無く割り込まれないため、そのままスレッド化すると resolve_target_order と resolve_sell_quantity が並行リクエストと同じ建玉を読む。ロックは signal_received の記録より後に置き、OpenD 障害中もシグナルは記録されるようにした。

/status/events は OpenD に触らないので async def のまま残し、スレッドプールが埋まっても診断できるようにする。"
```

---

### Task 5: ドキュメントを同期する

**Files:**
- Modify: `docs/ops/deployment.md`（新規 unit / timer の全文と本体 unit の差分）
- Modify: `README.md`・`README.en.md`（環境変数表）
- Modify: `CLAUDE.md`（環境変数表）
- Modify: `/Users/sakae/dev/alpha-trade/alforge-labs/mkdocs_src/ja/guides/alpha-strike-setup.md` と `en/` 版
- Modify: `/Users/sakae/dev/alpha-trade/alforge-labs/ja/docs/**`・`en/docs/**`（mkdocs ビルド成果物）

**Interfaces:**
- Consumes: Task 2 のエントリポイント名 `alpha-strike-watchdog`
- Produces: なし

- [ ] **Step 1: `docs/ops/deployment.md` に systemd の節を追加する**

`## 3. status API のネットワーク保護（Cloudflare Access）` の直前に新しい節を挿入する。

````markdown
## 2.5 signal watchdog の systemd timer（v1.3.0+）

シグナル途絶監視は **alpha-strike 本体とは別プロセス**で動く。本体のイベントループが OpenD の
同期呼び出しで凍結しても、プロセスが落ちても、監視だけは独立して動き続ける
（2026-08-23 の障害はこれが無くて 5 営業日気づけなかった）。

**unit はリポジトリ管理外**で、既存の `alpha-strike.service` と同じく VM 上にのみ存在する。

```bash
sudo tee /etc/systemd/system/alpha-strike-watchdog.service >/dev/null <<'EOF'
[Unit]
Description=alpha-strike signal outage watchdog (oneshot)

[Service]
Type=oneshot
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/alpha-strike
EnvironmentFile=/etc/alpha-strike/.env
ExecStart=/opt/alpha-strike/.venv/bin/alpha-strike-watchdog
StandardOutput=journal
StandardError=journal
EOF

sudo tee /etc/systemd/system/alpha-strike-watchdog.timer >/dev/null <<'EOF'
[Unit]
Description=Run alpha-strike signal outage watchdog hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now alpha-strike-watchdog.timer
```

`Persistent=true` にすると、VM 停止中に飛ばした実行を起動後に 1 回補完する。

### 本体 unit の変更（v1.3.0+）

`/etc/systemd/system/alpha-strike.service` を `sudo systemctl edit --full alpha-strike` で編集する。

| 項目 | 変更前 | 変更後 | 理由 |
|---|---|---|---|
| `Requires=moomoo-opend.service` | あり | **`Wants=moomoo-opend.service`** | moomoo-opend のクラッシュループに巻き込まれるのを止める。OpenD 障害中も webhook を受けてシグナルを記録する |
| `Restart=on-failure` | | **`Restart=always`** | OpenD 接続失敗で exit 0 終了すると on-failure では再起動されない |

`After=moomoo-opend.service` は維持する（起動順序の指定は引き続き必要）。

### 確認

```bash
systemctl list-timers alpha-strike-watchdog.timer
sudo systemctl start alpha-strike-watchdog.service   # 手動 1 回実行
journalctl -u alpha-strike-watchdog -n 20
```

`signal watchdog: 最終受信=... 実効 ...h / しきい値 ...h` が出れば機能している。
**起動ログだけでは「動いている」の確認にならない**ので、この行を見ること。
````

- [ ] **Step 2: `README.md` の環境変数表を更新する**

`SIGNAL_WATCHDOG_INTERVAL_SECONDS` の行の説明を、timer 前提の記述へ差し替える。変更前:

```markdown
| `SIGNAL_WATCHDOG_INTERVAL_SECONDS` | — | 途絶チェックの実行間隔秒（既定 `3600`）。起動直後にも 1 回実行する |
```

変更後:

```markdown
| `SIGNAL_WATCHDOG_INTERVAL_SECONDS` | — | 途絶チェックの想定実行間隔秒（既定 `3600`）。v1.3.0 以降、実際の実行間隔は systemd timer（`alpha-strike-watchdog.timer`）が決める |
```

- [ ] **Step 3: `README.en.md` の対応行を更新する**

```markdown
| `SIGNAL_WATCHDOG_INTERVAL_SECONDS` | — | Nominal watchdog interval in seconds (default `3600`). Since v1.3.0 the actual cadence is driven by the systemd timer (`alpha-strike-watchdog.timer`) |
```

- [ ] **Step 4: `CLAUDE.md` の環境変数表を更新する**

```markdown
| `SIGNAL_WATCHDOG_INTERVAL_SECONDS` | 途絶チェックの想定実行間隔秒（デフォルト `3600`）。v1.3.0 以降は systemd timer が実際の間隔を決める |
```

併せて「アーキテクチャ」節の `signal_watchdog.py` の説明を更新する。変更前の記述に `常駐ループ` の語があれば、次の文へ差し替える。

```markdown
- `signal_watchdog.py` — シグナル途絶の検知と通知。**v1.3.0 以降はサーバー内の常駐ループではなく、`alpha-strike-watchdog` console script（systemd timer）から単発実行される**。本体のイベントループ凍結やプロセス停止に道連れにされないための分離。
```

- [ ] **Step 5: mkdocs 日本語版を更新する**

`/Users/sakae/dev/alpha-trade/alforge-labs/mkdocs_src/ja/guides/alpha-strike-setup.md` の `.env` サンプル内、`# 無効化する場合のみ設定: SIGNAL_WATCHDOG_ENABLED=0` の行の直後に追加する。

```
# ※ v1.3.0 以降、途絶監視は alpha-strike 本体とは別プロセス（systemd timer
#   alpha-strike-watchdog.timer）で毎時実行される。本体のイベントループが OpenD の
#   同期呼び出しで凍結しても、プロセスが落ちても監視は動き続ける。
```

- [ ] **Step 6: mkdocs 英語版を更新する**

`/Users/sakae/dev/alpha-trade/alforge-labs/mkdocs_src/en/guides/alpha-strike-setup.md` の `# Set SIGNAL_WATCHDOG_ENABLED=0 only to disable.` の直後に追加する。

```
# Note: since v1.3.0 the watchdog runs in its own process (systemd timer
#   alpha-strike-watchdog.timer), hourly. It keeps working even if the main
#   event loop is frozen by a blocking OpenD call or the process dies.
```

- [ ] **Step 7: mkdocs をビルドする**

```bash
cd /Users/sakae/dev/alpha-trade/alforge-labs
uv run mkdocs build -f mkdocs.ja.yml
uv run mkdocs build -f mkdocs.en.yml
```

Expected: 両方ともエラーなく完了する。ビルドが失敗したら成果物をコミットせず報告すること。

- [ ] **Step 8: alpha-strike 側をコミット**

```bash
git add docs/ops/deployment.md README.md README.en.md CLAUDE.md
git commit -m "docs: watchdog の別プロセス化と systemd の変更を反映する"
```

- [ ] **Step 9: alforge-labs 側をコミット**

alforge-labs は独立リポジトリのため、このワークツリーからは操作できない。別途ブランチを切る。

```bash
cd /Users/sakae/dev/alpha-trade/alforge-labs
git checkout main && git pull
git checkout -b docs/opend-resilience
git add mkdocs_src/ja/guides/alpha-strike-setup.md mkdocs_src/en/guides/alpha-strike-setup.md ja/docs en/docs
git commit -m "docs: alpha-strike の watchdog 別プロセス化を追記"
```

**`git pull` を先に行うこと。** 前回（2026-08-29）は main が 2 コミット進んでいてビルド成果物が
コンフリクトした。ソースは競合せず生成物だけが競合するので、その場合は main を取り込んでから
mkdocs を再ビルドして解決する（生成物を手でマージしない）。

- [ ] **Step 10: 最終フルゲート**

```bash
cd /Users/sakae/dev/alpha-trade/.claude/worktrees/opend-resilience
uv run pytest && uv run ruff check .
```

Expected: 全件 PASS・lint クリーン

---

## 完了後の手順（人間が実施）

1. alpha-strike の PR を作成しマージ
2. alforge-labs の PR を作成しマージ
3. `./release.sh minor` で `1.2.0` → `1.3.0`（git-cliff が CHANGELOG を再生成、タグ push で PyPI publish）
4. **市場休場時間帯に** VM を更新する。手順は `docs/ops/deployment.md` の「2.5 signal watchdog の systemd timer」節に従う

```bash
~/.local/bin/uv pip install --python /opt/alpha-strike/.venv/bin/python -U \
  "alpha-strike==1.3.0" "futu-api==10.06.6608"
```

5. 本体 unit を `Wants=` / `Restart=always` へ変更し、timer を有効化して再起動
6. 確認: `journalctl -u alpha-strike-watchdog -n 20` に `signal watchdog: 最終受信=...` が出ること
