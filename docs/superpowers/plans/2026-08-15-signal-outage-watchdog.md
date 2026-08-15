# シグナル途絶 watchdog 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** TradingView アラートのサイレント失効によるシグナル途絶を自動検知し、ntfy で通知する。

**Architecture:** `webhook_server.py` の `lifespan` に 3 本目の常駐 asyncio タスクを追加する。ループは最新の `signal_received` イベントを読み、土日を除外した実効経過時間がしきい値（既定 60h）を超えたら ntfy 通知とイベント記録を行う。判定は副作用ゼロの純粋関数に隔離し、周回をまたぐ状態は frozen dataclass を返す形で持つ。

**Tech Stack:** Python 3 / FastAPI / Pydantic / asyncio / pytest / ruff / uv

設計書: [`docs/superpowers/specs/2026-08-15-signal-outage-watchdog-design.md`](../specs/2026-08-15-signal-outage-watchdog-design.md)

## Global Constraints

- 作業ディレクトリは `/Users/sakae/dev/alpha-trade/.claude/worktrees/signal-outage-watchdog`（ブランチ `feat/signal-outage-watchdog`）。
- コード内コメント・docstring・コミットメッセージはすべて**日本語**で書く。
- コミットは **Conventional Commits**（`feat:` / `fix:` / `refactor:` / `test:` / `docs:` / `chore:`）。`CHANGELOG.md` は手で編集しない（`release.sh` が git-cliff で再生成する）。
- **依存パッケージを追加しない。** `pyproject.toml` の `dependencies` は現在 7 個（fastapi / futu-api / python-dotenv / requests / slowapi / tenacity / uvicorn）のまま変更しない。米国市場カレンダーライブラリは導入しない。
- Python の実行はすべて `uv run`（`pip` / `poetry` は使わない）。
- すべての関数シグネチャに型注釈を付ける。`Any` は既存コードが `event_logger` / `notifier` に使っている箇所のみ踏襲する。
- **`datetime` は naive（JST）で扱う。** `datetime.now()` をそのまま使い、tz-aware にしない（イベントログの `occurred_at` が naive JST のため）。
- 状態を持つ値は `@dataclass(frozen=True)` にし、更新は新インスタンスを返す（ミューテーション禁止）。
- 環境変数名・ループ構造・エラー握り方は既存の `pending_reconcile.py` / `carryover.py` に合わせる。
- 各タスクの最後に `uv run pytest` と `uv run ruff check .` を通す。alpha-strike の PR には CI が走らないため、ローカルゲートが唯一の防壁。

---

### Task 1: 実効時間計算を `market_hours.py` へ切り出す

`_weekend_hours_between` は現在 `carryover.py` の private 関数。watchdog から private を跨いで使うと依存方向が濁るため、両者が依存する下位ユーティリティとして独立させる。**振る舞いは変えない純粋な移動**。

**Files:**
- Create: `src/alpha_strike/services/market_hours.py`
- Create: `tests/test_market_hours.py`
- Modify: `src/alpha_strike/services/carryover.py`（`_weekend_hours_between` の定義を削除・呼び出しを置換）

**Interfaces:**
- Consumes: なし（最下層）
- Produces:
  - `weekend_hours_between(start: datetime, end: datetime) -> float`
  - `effective_hours_between(start: datetime, end: datetime) -> float`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_market_hours.py` を新規作成:

```python
"""土日除外の実効時間計算のユニットテスト。

WHY: carry-over の lookback (#89) と signal watchdog の途絶判定が共有する土台。
暦時間で測ると金曜クローズ後のシグナルは土日だけで 48h を超え、月曜寄付前に
stale 判定されて取りこぼす。「週末を跨いでも実効時間はほとんど進まない」ことを
境界値で固定し、どちらの機能も週末で誤動作しないことを保証する。

日付は実データに合わせた実在の曜日を使う:
2026-08-07(金) / 08-08(土) / 08-09(日) / 08-10(月) / 08-11(火) / 08-12(水)
"""

from __future__ import annotations

from datetime import datetime

import pytest

from alpha_strike.services.market_hours import (
    effective_hours_between,
    weekend_hours_between,
)


class TestWeekendHoursBetween:
    def test_平日のみなら土日は0時間(self):
        start = datetime(2026, 8, 11, 5, 0)  # 火
        end = datetime(2026, 8, 12, 5, 0)  # 水
        assert weekend_hours_between(start, end) == pytest.approx(0.0)

    def test_土曜途中から火曜までは土日43時間(self):
        # 土 05:00〜24:00 = 19h + 日 24h = 43h
        start = datetime(2026, 8, 8, 5, 0)  # 土
        end = datetime(2026, 8, 11, 5, 0)  # 火
        assert weekend_hours_between(start, end) == pytest.approx(43.0)

    def test_endがstart以前なら0(self):
        start = datetime(2026, 8, 11, 5, 0)
        end = datetime(2026, 8, 10, 5, 0)
        assert weekend_hours_between(start, end) == pytest.approx(0.0)


class TestEffectiveHoursBetween:
    def test_平日24時間はそのまま24実効時間(self):
        start = datetime(2026, 8, 11, 5, 0)  # 火
        end = datetime(2026, 8, 12, 5, 0)  # 水
        assert effective_hours_between(start, end) == pytest.approx(24.0)

    def test_週末跨ぎは暦72時間でも実効29時間(self):
        """正常運用で最大の間隔。ここを誤検知すると毎週末アラートが鳴る。"""
        start = datetime(2026, 8, 8, 5, 0)  # 土（= 金の米国クローズ分）
        end = datetime(2026, 8, 11, 5, 0)  # 火（= 月の米国クローズ分）
        assert effective_hours_between(start, end) == pytest.approx(29.0)

    def test_週末を完全に内包すると48時間が差し引かれる(self):
        start = datetime(2026, 8, 7, 18, 0)  # 金 18:00
        end = datetime(2026, 8, 10, 9, 0)  # 月 09:00
        # 暦 63h - 土日 48h = 15h
        assert effective_hours_between(start, end) == pytest.approx(15.0)

    def test_endがstart以前なら0(self):
        start = datetime(2026, 8, 11, 5, 0)
        end = datetime(2026, 8, 10, 5, 0)
        assert effective_hours_between(start, end) == pytest.approx(0.0)
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_market_hours.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'alpha_strike.services.market_hours'`

- [ ] **Step 3: `market_hours.py` を実装**

`src/alpha_strike/services/market_hours.py` を新規作成:

```python
"""市場休場（土日）を除外した実効経過時間の計算。

carry-over の lookback (#89) と signal watchdog の途絶判定が共有する。暦時間で測ると
金曜クローズ後のシグナルは土日だけで 48h を超え、月曜寄付前に stale 判定されてしまう。

祝日は考慮しない（YAGNI）。米国市場カレンダーを持ち込むほどの精度は不要で、土日除外なら
「祝日 1 日を挟んだ最大 53 実効時間」まで吸収でき、運用上はこれで足りる。
"""

from __future__ import annotations

from datetime import datetime, timedelta


def weekend_hours_between(start: datetime, end: datetime) -> float:
    """``start``〜``end`` に含まれる土日(市場休場)の時間数を返す。"""
    if end <= start:
        return 0.0
    total = 0.0
    cur = start
    while cur < end:
        day_end = min(
            end,
            (cur + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ),
        )
        if cur.weekday() >= 5:  # 5=土, 6=日
            total += (day_end - cur).total_seconds() / 3600.0
        cur = day_end
    return total


def effective_hours_between(start: datetime, end: datetime) -> float:
    """土日を除外した実効経過時間。``end <= start`` なら 0.0。"""
    if end <= start:
        return 0.0
    elapsed = (end - start).total_seconds() / 3600.0
    return elapsed - weekend_hours_between(start, end)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_market_hours.py -v`
Expected: PASS（7 件）

- [ ] **Step 5: `carryover.py` を新モジュールへ委譲**

`src/alpha_strike/services/carryover.py` を 3 箇所修正する。

(a) import に追加（`from alpha_strike.services.market_state import ...` の直前、アルファベット順で `market_hours` が `market_state` より先）:

```python
from alpha_strike.services.market_hours import effective_hours_between
```

(b) `_weekend_hours_between` の関数定義（`def _weekend_hours_between(...)` から `return total` まで）を**丸ごと削除**する。

(c) 実効時間の計算を置換する。削除前:

```python
        elapsed_hours = (current - occurred).total_seconds() / 3600.0
        effective_hours = elapsed_hours - _weekend_hours_between(occurred, current)
```

置換後:

```python
        effective_hours = effective_hours_between(occurred, current)
```

`elapsed_hours` はこの 2 行以外で使われていないため、変数ごと消してよい。

- [ ] **Step 6: 未使用になった import を削除**

`timedelta` は `carryover.py` 内で `_weekend_hours_between` の中でしか使われていない（25 行目の import と 145 行目の使用のみ）。関数を削除すると未使用になるため、import を書き換える。

変更前:

```python
from datetime import datetime, timedelta
```

変更後:

```python
from datetime import datetime
```

Run: `uv run ruff check src/alpha_strike/services/carryover.py`
Expected: 何も出力されない（F401 が出たら消し漏れ）

- [ ] **Step 7: carry-over の既存テストが緑のままか確認（回帰チェック）**

Run: `uv run pytest tests/test_carryover.py tests/test_webhook_carryover.py -v`
Expected: PASS（振る舞いを変えていないので全件通るはず。1 件でも落ちたら移動が等価でない）

- [ ] **Step 8: フルゲート**

Run: `uv run pytest && uv run ruff check .`
Expected: 全件 PASS・lint クリーン

- [ ] **Step 9: コミット**

```bash
git add src/alpha_strike/services/market_hours.py tests/test_market_hours.py src/alpha_strike/services/carryover.py
git commit -m "refactor: 土日除外の実効時間計算を market_hours へ切り出す

signal watchdog が同じ計算を必要とするが、carryover の private 関数を跨いで使うと依存方向が濁る。両者が依存する下位ユーティリティとして独立させ、境界値の直接テストを新設する。振る舞いは変えていない。"
```

---

### Task 2: 途絶判定の純粋関数を実装する

副作用ゼロの判定関数を先に固める。**誤検知の回帰テストがこのタスクの本体**で、週末跨ぎと米国祝日で鳴らないことをここで固定する。

**Files:**
- Create: `src/alpha_strike/services/signal_watchdog.py`
- Create: `tests/test_signal_watchdog.py`

**Interfaces:**
- Consumes: `effective_hours_between(start, end) -> float`（Task 1）
- Produces:
  - `SignalOutageVerdict`（frozen dataclass: `is_outage: bool`, `last_signal_at: datetime | None`, `last_signal_id: str | None`, `effective_hours: float`, `threshold_hours: float`）
  - `evaluate_signal_outage(last_signal_at, now, *, threshold_hours, last_signal_id=None) -> SignalOutageVerdict`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_signal_watchdog.py` を新規作成:

```python
"""シグナル途絶 watchdog のユニットテスト。

WHY: TradingView のアラートは現行プランで最大 1 ヶ月しか設定できず、期限が切れると
サイレントに配信を停止する。サーバも OpenD も正常なまま webhook だけ止まるため、
イベントログの「シグナルが来ない」以外に症状が出ず、過去 2 回とも人手の点検でしか
気づけなかった（7 営業日 / 4 営業日の取りこぼし）。

このテストが守るのは 2 つ:
1. 本物の途絶で必ず鳴ること（鳴らなければ機能が存在しないのと同じ）
2. 正常な週末跨ぎ・米国祝日で鳴らないこと（誤報が続くと通知を無視するようになり、
   監視そのものが死ぬ）

日付は実在の曜日を使う:
2026-08-07(金) / 08-08(土) / 08-09(日) / 08-10(月) / 08-11(火) / 08-12(水) / 08-13(木)
"""

from __future__ import annotations

from datetime import datetime

import pytest

from alpha_strike.services.signal_watchdog import evaluate_signal_outage

_THRESHOLD = 60.0


class TestEvaluateSignalOutage:
    def test_平日の通常間隔では途絶と判定しない(self):
        v = evaluate_signal_outage(
            datetime(2026, 8, 11, 5, 0),  # 火
            datetime(2026, 8, 12, 5, 0),  # 水
            threshold_hours=_THRESHOLD,
        )
        assert v.is_outage is False
        assert v.effective_hours == pytest.approx(24.0)

    def test_週末跨ぎは暦72時間でも途絶と判定しない(self):
        """正常運用で最大の間隔。ここで鳴ると毎週末アラートが飛ぶ。"""
        v = evaluate_signal_outage(
            datetime(2026, 8, 8, 5, 0),  # 土（金の米国クローズ分）
            datetime(2026, 8, 11, 5, 0),  # 火（月の米国クローズ分）
            threshold_hours=_THRESHOLD,
        )
        assert v.is_outage is False
        assert v.effective_hours == pytest.approx(29.0)

    def test_月曜が米国祝日でも途絶と判定しない(self):
        """祝日は市場カレンダーを持たないので実効 53h を閾値で吸収する。"""
        v = evaluate_signal_outage(
            datetime(2026, 8, 8, 5, 0),  # 土
            datetime(2026, 8, 12, 5, 0),  # 水（月が休場でシグナルが 1 回飛ぶ）
            threshold_hours=_THRESHOLD,
        )
        assert v.is_outage is False
        assert v.effective_hours == pytest.approx(53.0)

    def test_しきい値ちょうどは途絶と判定しない(self):
        """境界は「超えたら」検知。等号で鳴らすと祝日ケースと 1 秒差で衝突する。"""
        v = evaluate_signal_outage(
            datetime(2026, 8, 10, 0, 0),  # 月 00:00
            datetime(2026, 8, 12, 12, 0),  # 水 12:00 = 実効ちょうど 60h
            threshold_hours=_THRESHOLD,
        )
        assert v.effective_hours == pytest.approx(60.0)
        assert v.is_outage is False

    def test_実際の障害ケースでは途絶と判定する(self):
        """2026-08-08 05:01 を最後に途絶した実障害の再現。"""
        v = evaluate_signal_outage(
            datetime(2026, 8, 8, 5, 0),  # 土
            datetime(2026, 8, 13, 5, 0),  # 木
            threshold_hours=_THRESHOLD,
            last_signal_id="20260807-093000",
        )
        assert v.is_outage is True
        assert v.effective_hours == pytest.approx(77.0)
        assert v.last_signal_id == "20260807-093000"

    def test_シグナルが1件も無ければ途絶と判定しない(self):
        """初回デプロイ直後やログ空で即発報すると誤報になる（fail-safe）。"""
        v = evaluate_signal_outage(
            None, datetime(2026, 8, 13, 5, 0), threshold_hours=_THRESHOLD
        )
        assert v.is_outage is False
        assert v.last_signal_at is None
        assert v.effective_hours == pytest.approx(0.0)

    def test_判定結果はイミュータブル(self):
        v = evaluate_signal_outage(
            datetime(2026, 8, 11, 5, 0),
            datetime(2026, 8, 12, 5, 0),
            threshold_hours=_THRESHOLD,
        )
        with pytest.raises(Exception):
            v.is_outage = True  # type: ignore[misc]
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_signal_watchdog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'alpha_strike.services.signal_watchdog'`

- [ ] **Step 3: 判定関数を実装**

`src/alpha_strike/services/signal_watchdog.py` を新規作成:

```python
"""TradingView シグナルの途絶を検知して通知する watchdog。

背景: TradingView は現行プランでアラートの有効期限が最大 1 ヶ月で、期限が切れると
**サイレントに配信を停止する**。alpha-strike 側は webhook サーバも OpenD も正常で
``/status`` は HTTP 200 を返し続けるため、「イベントログにシグナルが来ない」という
形でしか症状が出ない。実際に 2 回（2026-06-27 起点で 7 営業日、2026-08-08 起点で
4 営業日）取りこぼし、いずれも人手の点検でしか気づけなかった。

判定は「最後の ``signal_received`` からの **土日除外実効時間** がしきい値超か」。
シグナルは毎営業日 16:01 ET（= 05:01 JST）に届くため、実効時間で測ると正常な間隔は
最大 29h（週末跨ぎ）、米国祝日が 1 日挟まって最大 53h。既定しきい値 60h なら
誤報ゼロで 2 セッション欠落を捕捉できる。

VM / サービス停止は検知できない（watchdog もサーバごと死ぬため）。それは外部からの
死活監視が要る別課題。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alpha_strike.services.market_hours import effective_hours_between


@dataclass(frozen=True)
class SignalOutageVerdict:
    """1 回分の途絶判定結果。"""

    is_outage: bool
    last_signal_at: datetime | None
    last_signal_id: str | None
    effective_hours: float
    threshold_hours: float


def evaluate_signal_outage(
    last_signal_at: datetime | None,
    now: datetime,
    *,
    threshold_hours: float,
    last_signal_id: str | None = None,
) -> SignalOutageVerdict:
    """実効経過が ``threshold_hours`` を **超えたら** 途絶と判定する。

    ``last_signal_at`` が ``None``（シグナル 0 件）は途絶と判定しない。初回デプロイ
    直後やログ空の状態で即発報すると誤報になるため（fail-safe）。

    境界は等号を含めない。しきい値ちょうど（祝日ケースの上限付近）で鳴らさないことで、
    祝日 1 日を挟んだ 53 実効時間との間に余裕を残す。
    """
    if last_signal_at is None:
        return SignalOutageVerdict(
            is_outage=False,
            last_signal_at=None,
            last_signal_id=None,
            effective_hours=0.0,
            threshold_hours=threshold_hours,
        )
    effective = effective_hours_between(last_signal_at, now)
    return SignalOutageVerdict(
        is_outage=effective > threshold_hours,
        last_signal_at=last_signal_at,
        last_signal_id=last_signal_id,
        effective_hours=effective,
        threshold_hours=threshold_hours,
    )
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_signal_watchdog.py -v`
Expected: PASS（7 件）

- [ ] **Step 5: フルゲート**

Run: `uv run pytest && uv run ruff check .`
Expected: 全件 PASS・lint クリーン

- [ ] **Step 6: コミット**

```bash
git add src/alpha_strike/services/signal_watchdog.py tests/test_signal_watchdog.py
git commit -m "feat: シグナル途絶の判定ロジックを追加

最後の signal_received からの土日除外実効時間がしきい値超かで判定する純粋関数を追加する。実データ上、正常な実効間隔は最大 29h（週末跨ぎ）、米国祝日込みで最大 53h のため既定 60h で誤報ゼロと 2 セッション欠落の捕捉を両立する。

週末跨ぎ・祝日・しきい値ちょうどで鳴らないことをテストで固定した。誤報が続くと通知を無視するようになり監視そのものが死ぬため、ここは本物の検知と同じ重みで守る。"
```

---

### Task 3: 環境変数ヘルパーと最新シグナル取得を実装する

ループが必要とする設定値の読み取りと、イベントログからの入力取得。

**Files:**
- Modify: `src/alpha_strike/services/signal_watchdog.py`
- Modify: `tests/test_signal_watchdog.py`

**Interfaces:**
- Consumes: `event_logger.load_events(broker=..., event_type=..., limit=...) -> list[dict]`（既存 `JsonlEventLogger`）
- Produces:
  - `DEFAULT_INTERVAL_SECONDS = 3600.0` / `DEFAULT_THRESHOLD_HOURS = 60.0` / `DEFAULT_RENOTIFY_HOURS = 24.0` / `DEFAULT_BROKER = "moomoo"`
  - `is_signal_watchdog_enabled() -> bool`
  - `get_signal_watchdog_interval() -> float`
  - `get_signal_watchdog_threshold_hours() -> float`
  - `get_signal_watchdog_renotify_hours() -> float`
  - `get_signal_watchdog_broker() -> str`
  - `find_last_signal(event_logger: Any, *, broker: str = DEFAULT_BROKER) -> tuple[datetime | None, str | None]`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_signal_watchdog.py` の import を差し替える:

```python
from alpha_strike.services.signal_watchdog import (
    DEFAULT_BROKER,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_THRESHOLD_HOURS,
    evaluate_signal_outage,
    find_last_signal,
    get_signal_watchdog_broker,
    get_signal_watchdog_interval,
    get_signal_watchdog_threshold_hours,
    is_signal_watchdog_enabled,
)
```

ファイル冒頭の import に `from unittest.mock import MagicMock` を追加し、末尾に以下を追記:

```python
def _signal_event(
    occurred_at: str = "2026-08-08T05:01:12.363660",
    signal_id: str = "20260807-093000",
) -> dict:
    return {
        "event_type": "signal_received",
        "event_id": "evt_1",
        "signal_id": signal_id,
        "occurred_at": occurred_at,
        "broker": "moomoo",
        "asset_class": "US",
        "action": "sell",
        "ticker": "US.TQQQ",
        "quantity": 15.0,
        "strategy_id": "beat_qqq_hedged_v1",
    }


def _logger_with(events: list[dict]) -> MagicMock:
    logger = MagicMock()
    logger.load_events.return_value = events
    return logger


class TestEnvHelpers:
    def test_既定は有効(self, monkeypatch):
        monkeypatch.delenv("SIGNAL_WATCHDOG_ENABLED", raising=False)
        assert is_signal_watchdog_enabled() is True

    def test_0で無効化できる(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_WATCHDOG_ENABLED", "0")
        assert is_signal_watchdog_enabled() is False

    def test_数値でない間隔は既定へフォールバック(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_WATCHDOG_INTERVAL_SECONDS", "abc")
        assert get_signal_watchdog_interval() == DEFAULT_INTERVAL_SECONDS

    def test_0以下のしきい値は既定へフォールバック(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_WATCHDOG_THRESHOLD_HOURS", "-5")
        assert get_signal_watchdog_threshold_hours() == DEFAULT_THRESHOLD_HOURS

    def test_不正なbrokerは既定へフォールバック(self, monkeypatch):
        """イベントモデルが Literal で弾く前に落とす。unknown.jsonl を作らせない。"""
        monkeypatch.setenv("SIGNAL_WATCHDOG_BROKER", "kabu")
        assert get_signal_watchdog_broker() == DEFAULT_BROKER

    def test_有効なbrokerはそのまま使う(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_WATCHDOG_BROKER", "oanda")
        assert get_signal_watchdog_broker() == "oanda"


class TestFindLastSignal:
    def test_最新のsignal_receivedを返す(self):
        logger = _logger_with([_signal_event()])
        at, sid = find_last_signal(logger, broker="moomoo")
        assert at == datetime(2026, 8, 8, 5, 1, 12, 363660)
        assert sid == "20260807-093000"
        logger.load_events.assert_called_once_with(
            broker="moomoo", event_type="signal_received", limit=1
        )

    def test_イベントが無ければNoneを返す(self):
        at, sid = find_last_signal(_logger_with([]), broker="moomoo")
        assert at is None
        assert sid is None

    def test_occurred_atが壊れていてもNoneを返す(self):
        """パース失敗で例外を投げると常駐ループが毎周回落ちる。"""
        logger = _logger_with([_signal_event(occurred_at="not-a-date")])
        at, sid = find_last_signal(logger, broker="moomoo")
        assert at is None
        assert sid is None
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_signal_watchdog.py -v`
Expected: FAIL — `ImportError: cannot import name 'find_last_signal'`

- [ ] **Step 3: ヘルパーを実装**

`src/alpha_strike/services/signal_watchdog.py` の import を差し替える:

```python
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from alpha_strike.services.market_hours import effective_hours_between

logger = logging.getLogger(__name__)

_ENABLED_ENV_VAR = "SIGNAL_WATCHDOG_ENABLED"
_INTERVAL_ENV_VAR = "SIGNAL_WATCHDOG_INTERVAL_SECONDS"
_THRESHOLD_ENV_VAR = "SIGNAL_WATCHDOG_THRESHOLD_HOURS"
_RENOTIFY_ENV_VAR = "SIGNAL_WATCHDOG_RENOTIFY_HOURS"
_BROKER_ENV_VAR = "SIGNAL_WATCHDOG_BROKER"
_TRUTHY = {"1", "true", "yes", "on"}
_VALID_BROKERS = {"oanda", "moomoo"}

DEFAULT_INTERVAL_SECONDS = 3600.0
DEFAULT_THRESHOLD_HOURS = 60.0
DEFAULT_RENOTIFY_HOURS = 24.0
DEFAULT_BROKER = "moomoo"
```

`evaluate_signal_outage` の定義の後ろに以下を追加:

```python
def is_signal_watchdog_enabled() -> bool:
    """途絶監視の有効可否。既定 ON。

    ``NTFY_TOPIC`` 未設定なら通知は no-op になるため、既定 ON でも配布ユーザーに
    実害は無い（``CARRYOVER_ENABLED`` / ``PENDING_RECONCILE_ENABLED`` と同じ方針）。
    """
    return os.getenv(_ENABLED_ENV_VAR, "1").strip().lower() in _TRUTHY


def _positive_float_env(name: str, default: float) -> float:
    """正の float 環境変数。不正値・0 以下は既定へフォールバックする。"""
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s が数値ではありません、既定の %s を使用", name, default)
        return default
    return value if value > 0 else default


def get_signal_watchdog_interval() -> float:
    """チェック間隔（秒）。既定 3600 秒（しきい値 60h に対し 1 時間毎で十分）。"""
    return _positive_float_env(_INTERVAL_ENV_VAR, DEFAULT_INTERVAL_SECONDS)


def get_signal_watchdog_threshold_hours() -> float:
    """途絶と判定する実効時間（土日除外）。既定 60 時間。"""
    return _positive_float_env(_THRESHOLD_ENV_VAR, DEFAULT_THRESHOLD_HOURS)


def get_signal_watchdog_renotify_hours() -> float:
    """途絶継続中の再通知の最小間隔（時間）。既定 24 時間。"""
    return _positive_float_env(_RENOTIFY_ENV_VAR, DEFAULT_RENOTIFY_HOURS)


def get_signal_watchdog_broker() -> str:
    """監視対象 broker。既定 moomoo。不正値は既定へフォールバック。

    イベントの ``broker`` にもこの値を使う。``event_logger.append`` は broker で
    書き込み先ファイルを決めるため、不正値を通すと ``.unknown.jsonl`` が生まれ
    alpha-forge 側の ``glob("*.jsonl")`` に混入する。
    """
    raw = os.getenv(_BROKER_ENV_VAR, DEFAULT_BROKER).strip().lower()
    if raw not in _VALID_BROKERS:
        logger.warning(
            "%s が不正な値のため既定 %s を使用", _BROKER_ENV_VAR, DEFAULT_BROKER
        )
        return DEFAULT_BROKER
    return raw


def find_last_signal(
    event_logger: Any, *, broker: str = DEFAULT_BROKER
) -> tuple[datetime | None, str | None]:
    """最新の ``signal_received`` の ``(occurred_at, signal_id)`` を返す。

    ``load_events`` は新しい順に返すため ``limit=1`` で最新 1 件が取れる。返り値は
    生 JSON 由来なので ``occurred_at`` は str。``fromisoformat`` でパースする
    （``carryover.py`` と同じ扱い）。

    見つからない・パースできない場合は ``(None, None)``。呼び出し側はこれを
    「途絶と判定しない」fail-safe として扱う。ここで例外を投げると常駐ループが
    毎周回エラーになる。
    """
    events = event_logger.load_events(
        broker=broker, event_type="signal_received", limit=1
    )
    if not events:
        return None, None
    event = events[0]
    try:
        occurred_at = datetime.fromisoformat(str(event.get("occurred_at")))
    except (TypeError, ValueError):
        logger.warning("signal_received の occurred_at をパースできませんでした")
        return None, None
    signal_id = event.get("signal_id")
    return occurred_at, str(signal_id) if signal_id is not None else None
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_signal_watchdog.py -v`
Expected: PASS（16 件）

- [ ] **Step 5: フルゲート**

Run: `uv run pytest && uv run ruff check .`
Expected: 全件 PASS・lint クリーン

- [ ] **Step 6: コミット**

```bash
git add src/alpha_strike/services/signal_watchdog.py tests/test_signal_watchdog.py
git commit -m "feat: signal watchdog の環境変数ヘルパーと最新シグナル取得を追加

既存の CARRYOVER_* / PENDING_RECONCILE_* と同じ命名・フォールバック方針で設定値を読む。SIGNAL_WATCHDOG_BROKER は不正値を既定へ倒す。イベントの書き込み先ファイル名が broker 由来のため、素通しすると .unknown.jsonl が生まれ alpha-forge の glob に混入する。

find_last_signal はパース失敗でも例外を投げず None を返す。常駐ループが毎周回落ちるのを避けるため。"
```

---

### Task 4: イベントモデルと 1 周回の実行を実装する

判定 → 通知 → イベント記録の 1 周回。再通知抑制と復旧通知をここで固める。

**Files:**
- Modify: `src/alpha_strike/models.py`（`SignalCarryoverQueuedEvent` の定義直後・281 行目付近に追加）
- Modify: `src/alpha_strike/services/signal_watchdog.py`
- Modify: `tests/test_signal_watchdog.py`

**Interfaces:**
- Consumes: `evaluate_signal_outage` / `find_last_signal` / `DEFAULT_*`（Task 2・3）、`_generate_id(prefix: str) -> str`（既存 `fill_service`）、`notifier.notify(title, message, *, tags, priority) -> bool` と `notifier.enabled`（既存 `NtfyNotifier`）
- Produces:
  - `SignalOutageDetectedEvent`（Pydantic モデル）
  - `SignalWatchdogState`（frozen dataclass: `last_notified_at: datetime | None = None`, `in_outage: bool = False`）
  - `run_signal_watchdog_once(*, event_logger, notifier=None, state, threshold_hours=..., renotify_hours=..., broker=..., now=None) -> SignalWatchdogState`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_signal_watchdog.py` の import に追加:

```python
from alpha_strike.services.signal_watchdog import (
    SignalWatchdogState,
    run_signal_watchdog_once,
)
```

末尾に追記:

```python
class _FakeNotifier:
    """notify の呼び出しを記録する Fake。enabled は NtfyNotifier と同じ意味。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[tuple[str, str]] = []

    def notify(self, title, message, *, tags=(), priority=None, **kwargs) -> bool:
        self.calls.append((title, message))
        return self.enabled


def _run(logger, notifier, state, now, renotify_hours=24.0):
    return run_signal_watchdog_once(
        event_logger=logger,
        notifier=notifier,
        state=state,
        threshold_hours=60.0,
        renotify_hours=renotify_hours,
        broker="moomoo",
        now=now,
    )


class TestRunSignalWatchdogOnce:
    def test_途絶を検知したら通知しイベントを1件書く(self):
        logger = _logger_with([_signal_event()])
        notifier = _FakeNotifier()
        state = _run(logger, notifier, SignalWatchdogState(), datetime(2026, 8, 13, 5, 0))

        assert len(notifier.calls) == 1
        title, message = notifier.calls[0]
        assert "途絶" in title
        assert "TradingView" in message  # 次の行動が分かる文面であること
        assert logger.append.call_count == 1
        written = logger.append.call_args[0][0]
        assert written.event_type == "signal_outage_detected"
        assert written.outage_state == "detected"
        assert written.broker == "moomoo"
        assert state.in_outage is True
        assert state.last_notified_at == datetime(2026, 8, 13, 5, 0)

    def test_正常時は通知もイベントも出さない(self):
        logger = _logger_with([_signal_event()])
        notifier = _FakeNotifier()
        state = _run(logger, notifier, SignalWatchdogState(), datetime(2026, 8, 11, 5, 0))

        assert notifier.calls == []
        assert logger.append.call_count == 0
        assert state.in_outage is False

    def test_再通知抑制中は沈黙しイベントも書かない(self):
        logger = _logger_with([_signal_event()])
        notifier = _FakeNotifier()
        first = _run(logger, notifier, SignalWatchdogState(), datetime(2026, 8, 13, 5, 0))
        second = _run(logger, notifier, first, datetime(2026, 8, 13, 17, 0))  # 12h 後

        assert len(notifier.calls) == 1  # 増えていない
        assert logger.append.call_count == 1
        assert second == first  # state は据え置き

    def test_再通知間隔を超えたら再び通知する(self):
        logger = _logger_with([_signal_event()])
        notifier = _FakeNotifier()
        first = _run(logger, notifier, SignalWatchdogState(), datetime(2026, 8, 13, 5, 0))
        _run(logger, notifier, first, datetime(2026, 8, 14, 6, 0))  # 25h 後

        assert len(notifier.calls) == 2
        assert logger.append.call_count == 2

    def test_復旧したら1回だけ通知する(self):
        logger = _logger_with([_signal_event()])
        notifier = _FakeNotifier()
        outage = _run(logger, notifier, SignalWatchdogState(), datetime(2026, 8, 13, 5, 0))

        # シグナルが再開した状態に差し替える
        logger.load_events.return_value = [
            _signal_event(occurred_at="2026-08-14T05:01:00", signal_id="20260813-093000")
        ]
        recovered = _run(logger, notifier, outage, datetime(2026, 8, 14, 6, 0))

        assert len(notifier.calls) == 2
        assert "再開" in notifier.calls[1][0]
        assert logger.append.call_args[0][0].outage_state == "recovered"
        assert recovered.in_outage is False
        assert recovered.last_notified_at is None

        # さらに次の周回では復旧通知を繰り返さない
        again = _run(logger, notifier, recovered, datetime(2026, 8, 14, 7, 0))
        assert len(notifier.calls) == 2
        assert again.in_outage is False

    def test_通知先が無くても検知イベントは書く(self):
        """NTFY_TOPIC 未設定でも検知履歴だけは残す（後から sync-events で回収できる）。"""
        logger = _logger_with([_signal_event()])
        notifier = _FakeNotifier(enabled=False)
        state = _run(logger, notifier, SignalWatchdogState(), datetime(2026, 8, 13, 5, 0))

        assert logger.append.call_count == 1
        assert state.in_outage is True

    def test_シグナル0件では何も起きない(self):
        logger = _logger_with([])
        notifier = _FakeNotifier()
        state = _run(logger, notifier, SignalWatchdogState(), datetime(2026, 8, 13, 5, 0))

        assert notifier.calls == []
        assert logger.append.call_count == 0
        assert state.in_outage is False

    def test_イベント追記に失敗しても通知済みの状態は進む(self):
        """記録失敗で state を戻すと、次周回で同じ通知を繰り返してしまう。"""
        logger = _logger_with([_signal_event()])
        logger.append.side_effect = OSError("disk full")
        notifier = _FakeNotifier()
        state = _run(logger, notifier, SignalWatchdogState(), datetime(2026, 8, 13, 5, 0))

        assert len(notifier.calls) == 1
        assert state.in_outage is True
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_signal_watchdog.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_signal_watchdog_once'`

- [ ] **Step 3: イベントモデルを追加**

`src/alpha_strike/models.py` の `SignalCarryoverQueuedEvent` クラス定義の直後（`class TradeClosedPayload` の直前）に追加:

```python
class SignalOutageDetectedEvent(BaseModel):
    """TradingView シグナルの途絶検知 / 復旧イベント。

    TradingView のアラートは現行プランで最大 1 ヶ月しか設定できず、期限が切れると
    サイレントに配信を停止する。サーバも OpenD も正常なまま webhook だけ止まるため、
    ``/status`` では異常が見えない。本イベントは ntfy 通知と対で残す検知の記録で、
    後から「何営業日落ちたか」を追えるようにする。

    ``outage_state``:
      - ``detected``: 途絶を検知して通知を試みた
      - ``recovered``: シグナル受信が再開して復旧通知を試みた

    下流（forge live replay / alpha-visualizer Live）は本イベントを **約定・保留の
    いずれでもない運用イベント** として扱い、equity には一切計上しない。
    """

    event_type: Literal["signal_outage_detected"] = "signal_outage_detected"
    event_id: str
    occurred_at: datetime
    broker: Literal["oanda", "moomoo"]
    outage_state: Literal["detected", "recovered"] = "detected"
    last_signal_at: datetime | None = None
    last_signal_id: str | None = None
    effective_hours: float
    threshold_hours: float
```

- [ ] **Step 4: 1 周回の実行を実装**

`src/alpha_strike/services/signal_watchdog.py` の import に追加:

```python
from alpha_strike.models import SignalOutageDetectedEvent
from alpha_strike.services.fill_service import _generate_id
```

モジュール定数に追加:

```python
_TIME_FMT = "%Y-%m-%d %H:%M"
```

ファイル末尾に追加:

```python
@dataclass(frozen=True)
class SignalWatchdogState:
    """周回をまたぐ通知抑制の状態。

    frozen なので更新は新インスタンスを返す。永続化しない: 再起動で失われても
    「もう一度鳴る」方向に倒れるだけで、取りこぼす方向には倒れない。
    """

    last_notified_at: datetime | None = None
    in_outage: bool = False


def _emit(
    event_logger: Any,
    notifier: Any,
    verdict: SignalOutageVerdict,
    current: datetime,
    broker: str,
    outage_state: str,
) -> None:
    """通知を試み、結果に関わらずイベントを 1 件追記する。

    「試みた」であって「成功した」ではない。``NTFY_TOPIC`` 未設定 (no-op) でも
    POST 失敗でもイベントは残す。通知経路が壊れていても検知履歴だけは追えるようにする。
    """
    last_at_str = (
        verdict.last_signal_at.strftime(_TIME_FMT)
        if verdict.last_signal_at is not None
        else "なし"
    )
    if outage_state == "detected":
        title = "⚠️ シグナル途絶を検知"
        message = (
            f"最後のシグナル受信から {verdict.effective_hours:.1f} 実効時間"
            "（土日除外）が経過しました。\n"
            f"最終受信: {last_at_str} JST (signal_id={verdict.last_signal_id})\n"
            "TradingView アラートの有効期限切れの可能性があります。"
            "期限を確認してください。"
        )
        tags = ["warning"]
        priority: str | None = "high"
        logger.warning(
            "シグナル途絶を検知: 実効 %.1fh > しきい値 %.1fh (最終受信=%s)",
            verdict.effective_hours,
            verdict.threshold_hours,
            last_at_str,
        )
    else:
        title = "✅ シグナル受信を再開"
        message = f"最終受信: {last_at_str} JST (signal_id={verdict.last_signal_id})"
        tags = ["white_check_mark"]
        priority = None
        logger.info("シグナル受信を再開: 最終受信=%s", last_at_str)

    if notifier is not None:
        sent = notifier.notify(title, message, tags=tags, priority=priority)
        if not sent and getattr(notifier, "enabled", False):
            logger.warning("signal watchdog の通知送信に失敗しました")

    try:
        event_logger.append(
            SignalOutageDetectedEvent(
                event_id=_generate_id("evt"),
                occurred_at=current,
                broker=broker,
                outage_state=outage_state,
                last_signal_at=verdict.last_signal_at,
                last_signal_id=verdict.last_signal_id,
                effective_hours=verdict.effective_hours,
                threshold_hours=verdict.threshold_hours,
            )
        )
    except Exception as exc:  # noqa: BLE001 — 記録失敗で通知状態まで巻き戻さない
        logger.warning("signal_outage_detected の追記に失敗: %s", exc)


def run_signal_watchdog_once(
    *,
    event_logger: Any,
    notifier: Any = None,
    state: SignalWatchdogState,
    threshold_hours: float = DEFAULT_THRESHOLD_HOURS,
    renotify_hours: float = DEFAULT_RENOTIFY_HOURS,
    broker: str = DEFAULT_BROKER,
    now: datetime | None = None,
) -> SignalWatchdogState:
    """1 周回分の判定・通知・イベント追記を行い、新しい state を返す。

    - 途絶中でも ``renotify_hours`` 未満なら沈黙する（通知もイベントも出さない）。
      鳴りっぱなしにすると通知を無視するようになり、監視そのものが死ぬ。
    - 途絶が解消した周回でのみ復旧通知を 1 回出す。
    """
    current = now if now is not None else datetime.now()
    last_at, last_id = find_last_signal(event_logger, broker=broker)
    verdict = evaluate_signal_outage(
        last_at, current, threshold_hours=threshold_hours, last_signal_id=last_id
    )

    if verdict.is_outage:
        if state.last_notified_at is not None:
            since_hours = (
                current - state.last_notified_at
            ).total_seconds() / 3600.0
            if since_hours < renotify_hours:
                return state
        _emit(event_logger, notifier, verdict, current, broker, "detected")
        return SignalWatchdogState(last_notified_at=current, in_outage=True)

    if state.in_outage:
        _emit(event_logger, notifier, verdict, current, broker, "recovered")
        return SignalWatchdogState(last_notified_at=None, in_outage=False)
    return state
```

- [ ] **Step 5: テストが通ることを確認**

Run: `uv run pytest tests/test_signal_watchdog.py -v`
Expected: PASS（24 件）

- [ ] **Step 6: フルゲート**

Run: `uv run pytest && uv run ruff check .`
Expected: 全件 PASS・lint クリーン

- [ ] **Step 7: コミット**

```bash
git add src/alpha_strike/models.py src/alpha_strike/services/signal_watchdog.py tests/test_signal_watchdog.py
git commit -m "feat: シグナル途絶の通知と検知イベント記録を追加

判定 → 通知 → イベント追記の 1 周回を実装する。通知を試みた周回だけ signal_outage_detected を 1 件書く（NTFY_TOPIC 未設定や POST 失敗でも書く）。通知は流れて消えるが、イベントログは残って後から何営業日落ちたかを追えるため。

再通知は既定 24h に 1 回へ抑制する。途絶が続く間ずっと鳴らすと通知を無視するようになり、監視そのものが死ぬ。復旧時は 1 回だけ通知して対処が効いたことを確認できるようにした。"
```

---

### Task 5: 常駐ループを実装し lifespan へ配線する

**Files:**
- Modify: `src/alpha_strike/services/signal_watchdog.py`
- Modify: `src/alpha_strike/webhook_server.py`
- Modify: `tests/test_signal_watchdog.py`

**Interfaces:**
- Consumes: `run_signal_watchdog_once` / `SignalWatchdogState` / `is_signal_watchdog_enabled` / `get_signal_watchdog_*`（Task 3・4）
- Produces: `signal_watchdog_loop(*, event_logger, notifier=None, interval_seconds=..., threshold_hours=..., renotify_hours=..., broker=...) -> None`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_signal_watchdog.py` の import に追加:

```python
import asyncio

from alpha_strike.services.signal_watchdog import signal_watchdog_loop
```

末尾に追記する。非同期テストのマーカーは **`@pytest.mark.anyio`**（`@pytest.mark.asyncio` ではない）。`tests/test_pending_reconcile.py::TestPendingReconcileLoop` と同じ書き方で、anyio の pytest プラグイン（4.13.0）が既定バックエンド asyncio で走らせる。

```python
class TestSignalWatchdogLoop:
    @pytest.mark.anyio
    async def test_起動直後に実行しintervalごとに繰り返しcancelで終わる(
        self, monkeypatch
    ):
        calls: list[int] = []

        def _fake_once(**kwargs):
            calls.append(1)
            return SignalWatchdogState()

        monkeypatch.setattr(
            "alpha_strike.services.signal_watchdog.run_signal_watchdog_once",
            _fake_once,
        )
        task = asyncio.create_task(
            signal_watchdog_loop(
                event_logger=MagicMock(), interval_seconds=0.01
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) >= 2

    @pytest.mark.anyio
    async def test_例外が出てもループは止まらない(self, monkeypatch):
        """1 周回の失敗で監視が永久に止まると、途絶を検知できなくなる。"""
        calls: list[int] = []

        def _boom(**kwargs):
            calls.append(1)
            raise RuntimeError("読み込み失敗")

        monkeypatch.setattr(
            "alpha_strike.services.signal_watchdog.run_signal_watchdog_once",
            _boom,
        )
        task = asyncio.create_task(
            signal_watchdog_loop(
                event_logger=MagicMock(), interval_seconds=0.01
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) >= 2  # 1 回目の例外後も呼ばれ続けている
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_signal_watchdog.py -v`
Expected: FAIL — `ImportError: cannot import name 'signal_watchdog_loop'`

- [ ] **Step 3: ループを実装**

`src/alpha_strike/services/signal_watchdog.py` の import に `import asyncio` を追加し、ファイル末尾に追加:

```python
async def signal_watchdog_loop(
    *,
    event_logger: Any,
    notifier: Any = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    threshold_hours: float = DEFAULT_THRESHOLD_HOURS,
    renotify_hours: float = DEFAULT_RENOTIFY_HOURS,
    broker: str = DEFAULT_BROKER,
) -> None:
    """シグナル途絶監視の常駐ループ。lifespan の background task として起動する。

    起動直後に 1 回目を実行し、以後 ``interval_seconds`` ごとに繰り返す。例外はログに
    残して継続し、``asyncio.CancelledError``（shutdown）でのみ終了する。例外時は
    ``state`` を更新しないため、次周回は同じ判定からやり直す。
    """
    state = SignalWatchdogState()
    while True:
        try:
            # イベントログの読み書きはファイル I/O のためイベントループから退避
            state = await asyncio.to_thread(
                run_signal_watchdog_once,
                event_logger=event_logger,
                notifier=notifier,
                state=state,
                threshold_hours=threshold_hours,
                renotify_hours=renotify_hours,
                broker=broker,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — ループは止めない
            logger.warning("signal watchdog loop でエラー: %s", exc)
        await asyncio.sleep(interval_seconds)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_signal_watchdog.py -v`
Expected: PASS（26 件）

- [ ] **Step 5: lifespan へ配線**

`src/alpha_strike/webhook_server.py` の import 群に追加（`from alpha_strike.services.notifier import NtfyNotifier` の近く、既存の import 順に合わせる）:

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

`lifespan` 内の carryover ブロック（`logger.info("carryover 再発注 有効 (interval=%ss)", co_interval)` の行）の直後、`event_logger.write_version_meta(__version__)` の直前に追加:

```python
    # シグナル途絶 watchdog: TradingView アラートのサイレント失効を検知して通知する。
    # サーバも OpenD も正常なまま webhook だけ止まるため、イベントログの「シグナルが
    # 来ない」以外に症状が出ない。過去 2 回とも人手の点検でしか気づけず、7 営業日 /
    # 4 営業日を取りこぼした。
    app.state.signal_watchdog_task = None
    if is_signal_watchdog_enabled():
        sw_interval = get_signal_watchdog_interval()
        if not app.state.notifier.enabled:
            # 「監視しているつもりで通知先が無い」状態を起動時に見えるようにする
            logger.warning(
                "signal watchdog 有効ですが NTFY_TOPIC 未設定のため通知は飛びません"
                "（検知イベントの記録のみ行います）"
            )
        app.state.signal_watchdog_task = asyncio.create_task(
            signal_watchdog_loop(
                event_logger=event_logger,
                notifier=app.state.notifier,
                interval_seconds=sw_interval,
                threshold_hours=get_signal_watchdog_threshold_hours(),
                renotify_hours=get_signal_watchdog_renotify_hours(),
                broker=get_signal_watchdog_broker(),
            )
        )
        logger.info("signal watchdog 有効 (interval=%ss)", sw_interval)
```

同じ `lifespan` の shutdown 側（`yield` の後、`carryover_resubmit_task` の cancel ブロックの直後、`logger.info("Alpha-Strike Webhook サーバー停止")` の直前）に追加:

```python
    if app.state.signal_watchdog_task is not None:
        app.state.signal_watchdog_task.cancel()
        with suppress(asyncio.CancelledError):
            await app.state.signal_watchdog_task
```

- [ ] **Step 6: サーバー起動系のテストが緑か確認**

Run: `uv run pytest tests/test_webhook_server.py tests/test_main.py -v`
Expected: PASS（lifespan にタスクが増えても既存の起動テストは通るはず）

- [ ] **Step 7: フルゲート**

Run: `uv run pytest && uv run ruff check .`
Expected: 全件 PASS・lint クリーン

- [ ] **Step 8: コミット**

```bash
git add src/alpha_strike/services/signal_watchdog.py src/alpha_strike/webhook_server.py tests/test_signal_watchdog.py
git commit -m "feat: シグナル途絶 watchdog を常駐ループとして起動する

pending_reconcile / carryover と同型の 3 本目の常駐タスクとして lifespan へ配線する。1 周回の失敗で監視が永久に止まると途絶を検知できなくなるため、例外は握ってログに残し次周回へ進む。

SIGNAL_WATCHDOG_ENABLED が真で NTFY_TOPIC が未設定のときは起動時に警告を出す。「監視しているつもりで通知先が無い」状態は、今回潰そうとしているサイレント失敗そのものなので起動時に見えるようにした。"
```

---

### Task 6: ドキュメントを同期する

親 `CLAUDE.md` 指針 10 により、alpha-strike の機能追加は同じ作業単位で alforge-labs のドキュメントとビルド成果物まで揃える。

**Files:**
- Modify: `README.md`（alpha-strike・環境変数表）
- Modify: `CLAUDE.md`（alpha-strike・環境変数表）
- Modify: `/Users/sakae/dev/alpha-trade/alforge-labs/mkdocs_src/ja/guides/alpha-strike-setup.md`
- Modify: `/Users/sakae/dev/alpha-trade/alforge-labs/mkdocs_src/en/guides/alpha-strike-setup.md`
- Modify: `/Users/sakae/dev/alpha-trade/alforge-labs/ja/docs/**`・`en/docs/**`（mkdocs ビルド成果物）

**Interfaces:**
- Consumes: Task 3 で定義した 5 つの環境変数名と既定値
- Produces: なし（ドキュメントのみ）

- [ ] **Step 1: alpha-strike の README を更新**

`README.md` の環境変数表は 3 列だが、中央列は**既定値ではなく必須条件**（`—` / `moomoo 使用時` など）で、既定値は説明文の中に `（既定 \`X\`）` の形で書く。既存行の書式に必ず合わせること。

`ORDER_RECONCILE_DELAY_SECONDS` 行の直後に 5 行追加:

```markdown
| `SIGNAL_WATCHDOG_ENABLED` | — | TradingView シグナルの途絶監視（既定 `1`=有効）。`0`/`false` で無効化。アラートのサイレント失効でシグナルだけ止まる障害を検知し ntfy 通知する |
| `SIGNAL_WATCHDOG_INTERVAL_SECONDS` | — | 途絶チェックの実行間隔秒（既定 `3600`）。起動直後にも 1 回実行する |
| `SIGNAL_WATCHDOG_THRESHOLD_HOURS` | — | 途絶と判定する実効時間（土日除外、既定 `60`）。正常な週末跨ぎは実効 29h、米国祝日を 1 日挟んでも 53h のため、2 セッション欠落で発報する |
| `SIGNAL_WATCHDOG_RENOTIFY_HOURS` | — | 途絶継続中の再通知の最小間隔（時間、既定 `24`）。鳴りっぱなしによる通知無視を防ぐ |
| `SIGNAL_WATCHDOG_BROKER` | — | 監視対象 broker（`moomoo`（既定）/ `oanda`）。検知イベントの書き込み先ファイル名にも使う |
```

- [ ] **Step 2: alpha-strike の CLAUDE.md を更新**

`CLAUDE.md` の環境変数表の `STATUS_API_TOKEN` 行の直前に、Step 1 と同じ 5 行を追加する（表の列構成が `| 変数 | 説明 |` の 2 列なので、既定値は説明文に畳み込む）:

```markdown
| `SIGNAL_WATCHDOG_ENABLED` | シグナル途絶の監視。`1`（デフォルト）または `0`。TradingView アラートのサイレント失効を検知して ntfy 通知する |
| `SIGNAL_WATCHDOG_INTERVAL_SECONDS` | 途絶チェックの間隔秒（デフォルト `3600`） |
| `SIGNAL_WATCHDOG_THRESHOLD_HOURS` | 途絶と判定する実効時間（土日除外、デフォルト `60`）。正常な週末跨ぎは実効 29h、米国祝日込みで 53h のため 2 セッション欠落で発報する |
| `SIGNAL_WATCHDOG_RENOTIFY_HOURS` | 途絶継続中の再通知の最小間隔（デフォルト `24`） |
| `SIGNAL_WATCHDOG_BROKER` | 監視対象 broker（デフォルト `moomoo`）。イベントの書き込み先ファイル名にも使う |
```

- [ ] **Step 3: mkdocs 日本語版を更新**

`/Users/sakae/dev/alpha-trade/alforge-labs/mkdocs_src/ja/guides/alpha-strike-setup.md` の `.env` サンプル内、`# 無効化する場合のみ設定: PENDING_RECONCILE_ENABLED=0` の行の直後に追加:

```
# TradingView シグナルの途絶監視（既定 ON、v1.2.0+）。TradingView のアラートは
# 現行プランで最大 1 ヶ月しか設定できず、期限切れでサイレントに配信が止まる。
# 最後のシグナルからの実効時間（土日除外）が 60h を超えたら ntfy で通知する。
# 調整は SIGNAL_WATCHDOG_THRESHOLD_HOURS=60 / SIGNAL_WATCHDOG_INTERVAL_SECONDS=3600 /
# SIGNAL_WATCHDOG_RENOTIFY_HOURS=24 / SIGNAL_WATCHDOG_BROKER=moomoo
# 無効化する場合のみ設定: SIGNAL_WATCHDOG_ENABLED=0
```

- [ ] **Step 4: mkdocs 英語版を更新**

`/Users/sakae/dev/alpha-trade/alforge-labs/mkdocs_src/en/guides/alpha-strike-setup.md` の `# Set PENDING_RECONCILE_ENABLED=0 only to disable.` の行の直後に追加:

```
# Signal outage watchdog (default ON, v1.2.0+). TradingView alerts expire after at
# most one month on the current plan and stop firing silently. Sends an ntfy alert
# when the effective time (weekends excluded) since the last signal exceeds 60h.
# Tune with SIGNAL_WATCHDOG_THRESHOLD_HOURS=60 / SIGNAL_WATCHDOG_INTERVAL_SECONDS=3600 /
# SIGNAL_WATCHDOG_RENOTIFY_HOURS=24 / SIGNAL_WATCHDOG_BROKER=moomoo.
# Set SIGNAL_WATCHDOG_ENABLED=0 only to disable.
```

- [ ] **Step 5: mkdocs をビルドする**

```bash
cd /Users/sakae/dev/alpha-trade/alforge-labs
uv run mkdocs build -f mkdocs.ja.yml
uv run mkdocs build -f mkdocs.en.yml
```

Expected: 両方ともエラーなく完了し、`ja/docs/` `en/docs/` 配下の成果物が更新される

- [ ] **Step 6: alpha-strike 側をコミット**

```bash
git add README.md CLAUDE.md
git commit -m "docs: signal watchdog の環境変数を README と CLAUDE.md へ追記"
```

- [ ] **Step 7: alforge-labs 側をコミット**

alforge-labs は独立リポジトリのため、**このワークツリーからは操作できない**。`/Users/sakae/dev/alpha-trade/alforge-labs` で別途ブランチを切ってコミットし、PR を出す。

```bash
cd /Users/sakae/dev/alpha-trade/alforge-labs
git checkout -b docs/signal-outage-watchdog
git add mkdocs_src/ja/guides/alpha-strike-setup.md mkdocs_src/en/guides/alpha-strike-setup.md ja/docs en/docs
git commit -m "docs: alpha-strike のシグナル途絶監視の設定を追記"
```

- [ ] **Step 8: 最終フルゲート**

```bash
cd /Users/sakae/dev/alpha-trade/.claude/worktrees/signal-outage-watchdog
uv run pytest && uv run ruff check .
```

Expected: 全件 PASS・lint クリーン

---

## 完了後の手順（人間が実施）

実装完了後、以下は本計画の範囲外として人間が実施する。

1. alpha-strike の PR を作成しマージ（`Closes` で対応 issue を紐付ける）
2. alforge-labs の PR を作成しマージ
3. `./release.sh` で `1.1.0` → `1.2.0` へバンプ（git-cliff が CHANGELOG を再生成）、タグ push で PyPI publish
4. **市場休場時間帯に** VM (`oracle-strike`) を更新:

```bash
~/.local/bin/uv pip install --python /opt/alpha-strike/.venv/bin/python -U \
  "alpha-strike==1.2.0" "futu-api==10.06.6608"
sudo systemctl restart alpha-strike
curl -s localhost:8080/health
```

既定値のまま動くため `/etc/alpha-strike/.env` への追記は不要。しきい値を変えるときだけ追記する。

5. 反映後、`journalctl -u alpha-strike | grep "signal watchdog"` で `signal watchdog 有効 (interval=3600.0s)` が出ていることを確認する
