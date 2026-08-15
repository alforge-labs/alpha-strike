# シグナル途絶 watchdog 設計

- 作成日: 2026-08-15
- 対象: `alpha-strike`
- 関連: #57 Phase 2 (ntfy), #79 (pending reconcile), #89 (carry-over)

## 1. 背景

ペーパートレード（moomoo SIMULATE / `beat_qqq_hedged_v1`）で、TradingView からの webhook が
届かなくなる障害が **2 回発生し、いずれも人手の点検で初めて気づいた**。

| 回 | 最終シグナル | 気づいた日 | 取りこぼし | 真因 |
|---|---|---|---|---|
| 1 回目 | 2026-06-27 05:01 | 2026-07-08 | 7 営業日 | TradingView アラート失効 |
| 2 回目 | 2026-08-08 05:01 | 2026-08-15 | 4 営業日 | TradingView アラート失効 |

TradingView は現行プランでアラート有効期限が最大 1 ヶ月で、期限が切れると**サイレントに配信を停止する**。
alpha-strike 側は webhook サーバも OpenD も正常で `/status` は HTTP 200 を返し続けるため、
**「イベントログにシグナルが来ない」という形でしか症状が出ない**。監視が無いため検知は人手依存だった。

延長しても 1 ヶ月後に必ず再発する構造のため、途絶を自動検知して通知する仕組みを入れる。

## 2. 目的と成功基準

**目的**: シグナル途絶を自動検知し、人手の点検を待たずに通知する。

**成功基準**:

1. シグナルが 2 取引セッション分届かないと通知が飛ぶ
2. 正常な週末跨ぎ・米国祝日では通知が飛ばない（誤報ゼロ）
3. 通知を見た時点で次の行動（TradingView の期限確認）が分かる
4. 検知履歴がイベントログに残り、事後に「何営業日落ちたか」を追える

過去 2 回に本機能があった場合の検知タイミング（後述のしきい値 60 実効時間で計算）:

| 回 | 実際に気づいた日 | 本機能での通知 | 短縮 |
|---|---|---|---|
| 1 回目 | 2026-07-08 | **2026-07-01 12:00** | 7 日 |
| 2 回目 | 2026-08-15 | **2026-08-12 12:00** | 3 日 |

## 3. スコープ

**含む**: シグナル途絶（原因系①: サーバ正常・シグナルだけ来ない）の検知と通知。

**含まない**: VM / サービス停止（原因系②）の検知。サーバ内 watchdog はサーバごと死ぬため
構造的に検知できない。外部からの死活監視が必要で、別課題として切り出す。

## 4. 検知ロジックの根拠

シグナルは毎営業日 05:01 JST（= 16:01 ET の日足クローズ）に届く。イベントの `occurred_at` は
**naive JST**（例: `2026-08-10T22:34:03` = 米国寄付 09:34 ET）。

土日を除外した「実効時間」で測ったときの間隔:

| 区間 | 暦時間 | 実効時間（土日除外） |
|---|---:|---:|
| 平日 → 翌平日 | 24h | **24h** |
| 土 05:01 → 火 05:01（週末跨ぎ） | 72h | **約 29h** |
| 月曜が米国祝日 | 96h | 約 53h |
| 週中の祝日（独立記念日など） | 48h | 48h |

正常時の実効間隔は最大 29h、祝日が 1 日挟まって最大 53h。
**しきい値 60 実効時間**なら祝日を吸収しつつ、2 セッション欠落で発報する。

米国市場カレンダーは導入しない。依存を 1 つ増やす価値に対し、土日除外で祝日 53h まで
吸収できれば十分と判断した（既存 `_weekend_hours_between` の docstring が示す YAGNI 方針を踏襲）。

## 5. アーキテクチャ

```
src/alpha_strike/services/
├── market_hours.py      ★新規 — 土日除外の実効時間計算
├── signal_watchdog.py   ★新規 — 判定ロジック + 常駐ループ
└── carryover.py         （改）_weekend_hours_between を market_hours へ移し import に置換
```

`webhook_server.py` の `lifespan` に 3 本目の常駐タスクとして登録する。
`pending_reconcile_loop` / `carryover_resubmit_loop` と同型（`asyncio.create_task` →
shutdown で `cancel()` + `suppress(CancelledError)`）。

### 5.1 `market_hours.py`

`_weekend_hours_between` は現在 `carryover.py` の private 関数。watchdog から private を跨いで
使うと依存方向が濁るため、両者が依存する下位ユーティリティとして独立させる（SRP / DIP）。

```python
def weekend_hours_between(start: datetime, end: datetime) -> float:
    """start〜end に含まれる土日(市場休場)の時間数を返す。"""

def effective_hours_between(start: datetime, end: datetime) -> float:
    """土日を除外した実効経過時間。end <= start なら 0.0。"""
```

`carryover.py` は `_weekend_hours_between` の定義を削除し `weekend_hours_between` を import する。
**振る舞いは変えない**（純粋な移動）。既存の直接テストは無く carry-over テスト経由の間接検証のみのため、
移動と同時に境界値の直接テストを新設する。

### 5.2 `signal_watchdog.py`

判定は副作用ゼロの純粋関数に隔離する。

```python
@dataclass(frozen=True)
class SignalOutageVerdict:
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
    """実効経過が threshold_hours を「超えたら」途絶と判定する。

    last_signal_at が None（シグナル 0 件）は途絶と判定しない（is_outage=False）。
    初回デプロイ直後やログ空の状態で即発報するのを避けるため。
    """
```

ループ側の I/O:

- 最新シグナルの取得: `event_logger.load_events(broker=..., event_type="signal_received", limit=1)`
  （既存 API をそのまま利用。新規 I/O 実装は無い）
- `load_events` は生 JSON の `list[dict]` を返すため `occurred_at` は **str**。
  `datetime.fromisoformat(str(...))` でパースする（`carryover.py` と同じ扱い）
- 現在時刻は naive な `datetime.now()`。テスト用に `now` を注入可能にする（`carryover.py` と同じ）

### 5.3 ループの状態

| 状態 | 保持場所 | 再起動時 |
|---|---|---|
| 最終通知時刻（再通知抑制用） | メモリ | 消える → 次回チェックで再発報 |
| 途絶中フラグ（復旧通知用） | メモリ | 消える → 復旧通知が飛ばない場合がある |

いずれも永続化しない。再起動で失われても**安全側（鳴る方向）に倒れる**ため（YAGNI）。

## 6. 環境変数

既存の命名規約（`CARRYOVER_*` / `PENDING_RECONCILE_*`）に揃える。

| 変数 | 既定 | 説明 |
|---|---|---|
| `SIGNAL_WATCHDOG_ENABLED` | `1` | 有効/無効。`1/true/yes/on` を真とする（既存 `_TRUTHY` と同じ） |
| `SIGNAL_WATCHDOG_INTERVAL_SECONDS` | `3600` | チェック間隔。しきい値 60h に対し 1 時間毎で十分 |
| `SIGNAL_WATCHDOG_THRESHOLD_HOURS` | `60` | 実効時間しきい値 |
| `SIGNAL_WATCHDOG_RENOTIFY_HOURS` | `24` | 途絶継続中の再通知の最小間隔 |
| `SIGNAL_WATCHDOG_BROKER` | `moomoo` | 監視対象 broker。`load_events` の絞り込みとイベント書き込み先に使う |

既定を `1`（有効）にする根拠: `NTFY_TOPIC` 未設定なら通知は no-op なので、配布ユーザーに実害が無い。
既存 `CARRYOVER_ENABLED` / `PENDING_RECONCILE_ENABLED` も既定 `1`。

数値のパースは既存 `_float_env` と同様に、不正値なら警告ログを出して既定値へフォールバックする。

## 7. 通知仕様

既存の `NtfyNotifier` をそのまま使う（非 ASCII は `encode_header_value` が RFC 2047 で包む）。

**検知時**（`priority=high`, `tags=["warning"]`）:

```
Title: ⚠️ シグナル途絶を検知
最後のシグナル受信から 77.0 実効時間（土日除外）が経過しました。
最終受信: 2026-08-08 05:01 JST (signal_id=20260807-093000)
TradingView アラートの有効期限切れの可能性があります。期限を確認してください。
```

**復旧時**（`tags=["white_check_mark"]`、1 回だけ）:

```
Title: ✅ シグナル受信を再開
最終受信: 2026-08-12 05:01 JST (signal_id=20260811-093000)
```

再通知は `SIGNAL_WATCHDOG_RENOTIFY_HOURS` 未満なら送らない。復旧通知は
「前周回で途絶中 かつ 今周回で解消」のときのみ送る。

## 8. イベントモデル

`event_logger.append()` は `event.broker` を見て `YYYY-MM-DD.{broker}.jsonl` に振り分ける。
`broker` を持たないイベントは `.unknown.jsonl` に落ち、alpha-forge 側の `glob("*.jsonl")` に
混入するため、`broker` を必須フィールドとして持たせる。

```python
class SignalOutageDetectedEvent(BaseModel):
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

`outage_state` に検知/復旧の両方を持たせる構成は `SignalCarryoverQueuedEvent.carryover_state`
（`queued` / `abandoned`）と同じ設計に揃えている。

イベントは**通知を試みた周回でのみ**書く（再通知抑制で沈黙した周回では書かない）。
検知時は `outage_state="detected"`、復旧時は `outage_state="recovered"` を 1 件書く。

「試みた」であって「成功した」ではない。`NTFY_TOPIC` 未設定で no-op になった場合も、
ntfy への POST が失敗した場合も**イベントは書く**。通知経路が壊れていても検知履歴だけは
残り、後から `sync-events` で回収できる。

下流影響: alpha-forge の `live import-events` / `convert-check` は `event_type` で絞り込むため、
未知の型は無視される。equity 計算には影響しない。

## 9. エラーハンドリング

既存 2 ループと同じ方針。

- ループ本体は全例外を握って `logger.warning` → 次周回へ（**発注フローを絶対に壊さない**）
- `asyncio.CancelledError` のみ再送出（shutdown 用）
- 通知失敗は `notify()` が `False` を返す（例外を投げない）。`False` のとき `logger.warning`
- `SIGNAL_WATCHDOG_ENABLED` が真かつ `NTFY_TOPIC` 未設定のときは**起動時に `logger.warning`**。
  「監視しているつもりで通知先が無い」状態を検知可能にする
- イベントログ読み込み失敗時は `load_events` が `[]` を返す → `last_signal_at=None` →
  **途絶と判定しない**（誤報を避ける fail-safe）

## 10. テスト計画

TDD で先にテストを書く。

### `tests/test_market_hours.py`（新規）

| ケース | 期待 |
|---|---|
| 平日 24h | 実効 24h |
| 週末跨ぎ（土 05:01 → 火 05:01、暦 72h） | 実効 約 29h |
| 週末を完全に内包（金 18:00 → 月 09:00） | 実効 15h |
| `end <= start` | 0.0 |

### `tests/test_signal_watchdog.py`（新規）

判定（`evaluate_signal_outage`）— **誤報の回帰テストが最重要**:

| ケース | 実効時間 | 期待 |
|---|---:|---|
| 平日の通常間隔 | 24h | 非検知 |
| **週末跨ぎ（土 05:01 → 火 05:01、暦 72h）** | 約 29h | **非検知** |
| **月曜が米国祝日** | 約 53h | **非検知** |
| しきい値ちょうど（60.0h） | 60h | 非検知（「超えたら」検知） |
| 実際の障害（8/8 05:01 → 8/13 05:01、暦 120h） | 約 77h | **検知** |
| シグナル 0 件（`last_signal_at=None`） | — | 非検知 |

ループ（`signal_watchdog_loop`）— 既存 `test_carryover.py` に倣い Fake notifier / 時刻注入で 1 周回:

- 再通知抑制: 1 回目通知 → 12h 後は沈黙 → 25h 後に再通知
- 復旧通知が 1 回だけ飛ぶ
- 通知を試みた周回だけ `signal_outage_detected` イベントが書かれる（沈黙した周回では書かれない）
- `NTFY_TOPIC` 未設定でも `outage_state="detected"` イベントは書かれる（ループは落ちない）
- `load_events` が例外を投げてもループが落ちず次周回へ進む
- `SIGNAL_WATCHDOG_ENABLED=0` でループが起動しない

### 既存テストの回帰

`_weekend_hours_between` の移動が carry-over の振る舞いを変えていないこと
（`tests/test_carryover.py` / `tests/test_webhook_carryover.py` が緑のまま）。

## 11. ドキュメント同期（親 CLAUDE.md 指針 10）

1. `alpha-strike/README.md` の環境変数表に 5 変数を追記
2. `alpha-strike/CLAUDE.md` の環境変数表に 5 変数を追記
3. `alpha-strike/CHANGELOG.md` に追記
4. `alforge-labs/mkdocs_src/ja/guides/alpha-strike-setup.md` と **`en/` 版**の両方を更新
5. `alforge-labs` で `uv run mkdocs build -f mkdocs.ja.yml` と `-f mkdocs.en.yml` を実行し、
   **ビルド成果物もコミットに含める**

alpha-strike と alforge-labs は独立リポジトリのため PR は 2 本になるが、同じ作業単位として揃えてマージする。

## 12. リリースとデプロイ

- ブランチ: `feat/signal-outage-watchdog`（ワークツリーで隔離・親 CLAUDE.md 指針 9）
- alpha-strike の PR には CI が走らないため、**マージ前にローカルでフルゲート**
  （`uv run pytest` + `uv run ruff check .`）
- バージョン: `1.1.0` → `1.2.0`（機能追加）。`bump-my-version bump minor` → タグ push →
  GitHub Actions が PyPI publish

**VM への反映は本作業の範囲外**とする。本番サーバへの SSH と `systemctl restart` は
市場休場時間帯を選んで実施する不可逆な本番操作のため、手順を提示して人間が実行する。

```bash
# VM 側（市場休場時間帯に実施）
~/.local/bin/uv pip install --python /opt/alpha-strike/.venv/bin/python -U \
  "alpha-strike==1.2.0" "futu-api==10.06.6608"
sudo systemctl restart alpha-strike
curl -s localhost:8080/health
```

既定値のまま動くため `/etc/alpha-strike/.env` への追記は不要。しきい値を変えるときだけ追記する。

## 13. 受け入れ基準

- [ ] `uv run pytest` 緑・`uv run ruff check .` クリーン
- [ ] 週末跨ぎ（暦 72h）で通知が飛ばないテストが存在し緑
- [ ] 月曜が米国祝日のケース（実効 53h）で通知が飛ばないテストが存在し緑
- [ ] 2026-08-08 05:01 起点の実データ相当ケースで通知が飛ぶテストが存在し緑
- [ ] carry-over の既存テストが緑のまま（`_weekend_hours_between` 移動の回帰なし）
- [ ] README / CLAUDE.md / CHANGELOG / mkdocs_src(ja,en) が更新され、mkdocs ビルド成果物も含まれる

## 14. やらないこと（YAGNI）

- 米国市場カレンダーの導入（依存追加に見合わない。土日除外で祝日 53h まで吸収できる）
- 通知状態の永続化（再起動で失われても安全側に倒れる）
- VM / サービス停止の検知（構造的にサーバ内 watchdog では不可能。別課題）
- TradingView アラート有効期限の事前リマインダー（期限日の手動記録が必要で、
  記録漏れが新しい失敗点になる。途絶検知があれば実害は 2〜3 日に収まる）
