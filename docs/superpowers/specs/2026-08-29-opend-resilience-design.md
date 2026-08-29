# OpenD 障害耐性の恒久対策 設計

- 作成日: 2026-08-29
- 対象: `alpha-strike`
- 関連: #57 Phase 2 (ntfy), #79 (pending reconcile), #89 (carry-over), #136 (signal watchdog)

## 1. 背景

2026-08-23 から 8/29 まで、ペーパートレードが **米国 5 営業日（8/24〜8/28）にわたり完全停止**した。
watchdog は 2026-08-15 に導入済みだったが、**この障害を通知できなかった**。

### 障害の経緯

| 日時 | 出来事 |
|---|---|
| 08-23 04:33 | カーネル自動更新（6.17.0-1019 → 1020）で VM 再起動。毎月日曜 04:32 頃の定期パターン |
| 08-23 04:36 | OpenD の remember トークン失効。`PicVerifyCode.png` 生成＝画像認証待ちで停止 |
| 08-23 04:34〜04:36 | alpha-strike が 3 回再起動。`Requires=moomoo-opend.service` により moomoo-opend のクラッシュループに巻き込まれた |
| 08-23〜08-27 | alpha-strike 不在。webhook は宛先なしで消失し、**シグナルが記録すら残らなかった** |
| 08-27 06:35 | 再起動。watchdog が途絶を検知（実効 78.6h）し毎時ログを出力 |
| 08-27 21:35 | **watchdog のログが途絶**。以後 8/29 まで無反応。`/status` `/status/events` とも 524 |
| 08-29 22:00 | OpenD 再認証により復旧 |

### watchdog が鳴らなかった理由

設計時（#136）に「イベントログしか読まないので OpenD に依存しない」としたが、これは
**コードレベルでのみ真**だった。実行時には同じ asyncio イベントループを共有しており、
**OpenD への同期呼び出しがイベントループを塞ぐと watchdog も道連れで停止する**。

`market_state` を使う案を「OpenD が落ちると watchdog も黙る」という理由で却下したのに、
同じ依存を別経路から作り込んでいた。

## 2. 根本原因

`webhook_server.py` の **async ハンドラ内に OpenD への同期呼び出しが 5 箇所**ある。

| 行 | 呼び出し | 経路 |
|---|---|---|
| 456 | `should_carryover()` → `market_state` | webhook 受付 |
| 477 | `resolve_target_order()` → `status_provider.get_status` | webhook 受付（#80） |
| 514 | `resolve_sell_quantity()` → `status_provider.get_status` | webhook 受付（#74） |
| 537 | `order_router.route(payload)` | **発注（取引経路）** |
| 822 | `provider.get_status(trd_env=...)` | `/status` 参照 |

うち 4 箇所が `receive_webhook` に集中している。

OpenD が画像認証待ちで無限リトライしている間、これらの呼び出しは戻らない。`async def` の中で
`await` なしに実行されるため、**1 回の呼び出しでイベントループ全体が凍結**する。凍結すると:

- `/webhook` が応答しない → **シグナルを受け取れない（取引が止まる）**
- `/status` `/status/events` が 524
- 常駐タスク（watchdog / carryover / pending_reconcile）が全停止

背景タスク側は全て `asyncio.to_thread` を正しく使っており、**欠陥はリクエストハンドラ 3 箇所に限定**される。

補助的な要因が 2 つある。

- `Requires=moomoo-opend.service`: moomoo-opend の再起動が alpha-strike を道連れにする
- `Restart=on-failure`: exit 0 で終了すると systemd が再起動しない（2026-07-16 に記録済みの既知課題）

## 3. 目的と成功基準

**目的**: OpenD 障害時に (a) 取引受付が止まらず、(b) 監視が黙らない状態にする。

**成功基準**:

1. OpenD が画像認証待ちで固まっても `/webhook` が応答し、`signal_received` が記録される
2. OpenD が固まっても `/status/events` が応答する（診断可能性の維持）
3. alpha-strike のプロセスが死んでも途絶通知が飛ぶ
4. alpha-strike が exit 0 で終了しても自動再起動する
5. 発注の直列性が現行と変わらない（同一バーの複数銘柄が並行実行されない）

今回の障害に本設計が入っていた場合: 8/23 の再起動後も alpha-strike は生き残り（`Wants=`）、
シグナルは記録され続け、別プロセス watchdog が 8/25 頃には途絶を通知していた。

## 4. スコープ

**含む**: 上記 3 変更。

**含まない**: **VM ごと停止した場合の検知**。systemd timer は VM が生きている前提で動く。
2026-07-24 の OCI 緊急メンテによる VM 停止のようなケースは外部監視が必要で、別課題とする。

## 5. 変更 1 — watchdog を別プロセスへ

### 5.1 構成

```
alpha-strike-watchdog                ★新規 console script（単発実行）
alpha-strike-watchdog.service        ★新規 systemd unit (Type=oneshot)
alpha-strike-watchdog.timer          ★新規 systemd timer (OnCalendar=hourly)
```

**サブコマンドではなく独立した console script にする。** 現行 CLI は `argparse` のフラットな
パーサで、systemd の `ExecStart=/opt/alpha-strike/.venv/bin/alpha-strike --host 0.0.0.0 --port 8080`
がその形に依存している。ここへサブパーサを導入すると本番の起動コマンドを壊すリスクがあるため、
`pyproject.toml` の `[project.scripts]` に 1 行足して別バイナリとして分離する。

```toml
[project.scripts]
alpha-strike = "alpha_strike.cli:main"
alpha-strike-watchdog = "alpha_strike.cli:watchdog_main"   # ★追加
```

判定ロジック（`evaluate_signal_outage` / `find_last_signal` / `_emit` / 環境変数ヘルパー）は
**既存のテスト済みコードをそのまま再利用**する。撤去するのは以下 2 つのみ。

- `services/signal_watchdog.py` の `signal_watchdog_loop`
- `webhook_server.py` の lifespan における起動・shutdown 配線

`run_signal_watchdog_once` はそのまま CLI から呼ぶ。

### 5.2 状態をイベントログから復元する

単発実行のプロセスは周回間の記憶を持てないため、再通知抑制の状態を**イベントログから復元**する。

```python
def load_watchdog_state(
    event_logger: Any, *, broker: str = DEFAULT_BROKER
) -> SignalWatchdogState:
    """最新の signal_outage_detected イベントから state を復元する。

    - 最新が outage_state="detected"  → in_outage=True,  last_notified_at=その occurred_at
    - 最新が outage_state="recovered" → in_outage=False, last_notified_at=None
    - イベントが 0 件                 → 初期状態
    """
```

新しい状態ファイルは作らない。既に書いているイベントを読むだけで済む。

**副作用として現行より改善する**: 現在はメモリ保持のため再起動で状態が消え、再通知抑制が
無効化されて即座に鳴り直していた。イベントログ由来なら再起動を跨いで正しく引き継がれる。

### 5.3 CLI の仕様

```
alpha-strike-watchdog
```

引数は取らない。設定はすべて既存の `SIGNAL_WATCHDOG_*` 環境変数から読む
（systemd unit の `EnvironmentFile=/etc/alpha-strike/.env` で本体と同じ設定を共有する）。

終了コードは**常に 0**。途絶を検知したかどうかは通知とイベントログで表現し、終了コードには
乗せない。非ゼロにすると systemd が failed 扱いにしてタイマーの状態が汚れ、「監視が動いている」
ことと「途絶している」ことの区別がつかなくなるため。

`SIGNAL_WATCHDOG_ENABLED=0` のときは何もせず 0 で終了する。

## 6. 変更 2 — イベントループ凍結の解消

### 6.1 ハンドラを `def` にする（本体は変更しない）

`receive_webhook`（365 行）と `get_status`（814 行）はどちらも `async def` だが、
**本体に `await` が 1 つも無い**。名前だけ非同期で、実体は完全な同期コードがイベントループ上で
実行されている。

FastAPI は **非 async の `def` エンドポイントを自動的に外部スレッドプールで実行**する。
したがって宣言を変えるだけで、ハンドラ本体を一切変更せずに全ての OpenD 呼び出しが
イベントループから外れる。変更は 2 行のみ。

```python
async def receive_webhook(  →  def receive_webhook(
async def get_status(       →  def get_status(
```

これで `receive_webhook` 内の 4 つの OpenD 依存呼び出し（`should_carryover` /
`resolve_target_order` / `resolve_sell_quantity` / `order_router.route`）が一括でスレッドへ移る。

**個別に `asyncio.to_thread` で包む案は採らない。** 5 箇所を書き換える差分が大きいうえ、
次項の原子性を壊すため。

### 6.2 原子性を壊さないためにロックを入れる

**現在 436 行（`signal_received` 記録）から 560 行までに `await` が 1 つも無く、この区間は
他リクエストに割り込まれず原子的に実行されている。** 個別に `to_thread` で包むと await 点が
生まれ、`resolve_target_order`（#80）と `resolve_sell_quantity`（#74）が並行リクエストと
同じ建玉を読む競合を新たに作る。`def` 化でも FastAPI のスレッドプールは並行実行するため、
同じ問題が起きる。

`threading.Lock` で broker 操作区間を直列化し、現行と同じ原子性を保つ。

```python
# webhook_server.py のモジュールレベル
_ORDER_LOCK = threading.Lock()

# receive_webhook 内、signal_received の記録より後
with _ORDER_LOCK:
    ...  # should_carryover 〜 route 〜 order_event 記録まで
```

**`app.state` ではなくモジュールレベルに置く。** テスト 6 ファイル（`test_webhook_server.py` /
`test_status_api.py` / `test_webhook_carryover.py` / `test_webhook_sell_guard.py` /
`test_webhook_target_reconcile.py` / `test_webhook_reconcile_notify.py`）が lifespan を経由せず
`app.state` を直接組み立てており、`app.state.order_lock` にすると 6 箇所すべてに初期化を
足す必要がある。ロックは設定値を持たないプロセス共通の資源なので、モジュールレベルで足りる。

**ロックは `signal_received` の記録（436 行）より後**に置く。OpenD が固まってロック保持者が
戻らなくなっても、シグナルの記録だけは全リクエストで成立する（成功基準 1）。

`IdempotencyStore` は既に内部で `threading.Lock` を持つスレッドセーフ実装なので追加の対処は
不要（`services/idempotency.py` の docstring に明記されている）。

### 6.3 `/status/events` は `async def` のまま残す

`/status/events` はイベントログのファイル読み取りだけで OpenD に触らない。イベントループ上に
残しておくことで、**スレッドプールが webhook で埋まっても診断用エンドポイントとして応答し続ける**。

実装時に判明した制約: `async def` のエンドポイントでも、同期 `def` の依存（`Depends`）を
挟むと FastAPI がその依存をスレッドプールで実行するため、結局プールのトークンを消費する。
だから `/status` と `/status/events` が共有する `require_status_token` も `async def` にした。

### 6.4 タイムアウトは入れない

Python はスレッドを中断できないため `asyncio.wait_for` はスレッドを放置したまま返り、
ワーカーを食い潰す。イベントループが解放されれば目的は達成できるので導入しない（YAGNI）。

## 7. 変更 3 — systemd の堅牢化

| 項目 | 現在 | 変更後 | 理由 |
|---|---|---|---|
| `Requires=moomoo-opend.service` | あり | **`Wants=`** | クラッシュループの伝播を止める。OpenD 障害中もシグナルを記録する |
| `Restart=on-failure` | | **`Restart=always`** | exit 0 でも再起動する |

`After=moomoo-opend.service` は維持する（起動順序の指定は引き続き必要）。

`Wants=` にすると OpenD 障害中は発注が失敗し続けるが、失敗として記録に残る。現在の
「シグナルが記録すら残らず消える」状態より診断可能性が高い。OpenD 復旧後は次のシグナルで
`target_reconcile` が目標へスナップするため、取りこぼしは自己修復する。

## 8. エラーハンドリング

- `watchdog_main` は全例外を握って警告ログを出し、終了コード 0 で終わる。タイマーの
  次回実行を止めないため
- `load_watchdog_state` はパース失敗時に初期状態を返す（誤報より沈黙を選ぶ既存方針を踏襲）
- `def` 化した 2 ハンドラの例外処理は現行と同一（本体を変更しないため）

## 9. テスト計画

TDD で先にテストを書く。

### `tests/test_signal_watchdog.py`（追記）

| ケース | 期待 |
|---|---|
| 最新が `detected` | `in_outage=True`・`last_notified_at` がその `occurred_at` |
| 最新が `recovered` | `in_outage=False`・`last_notified_at=None` |
| イベント 0 件 | 初期状態 |
| `occurred_at` が壊れている | 初期状態（例外を投げない） |
| `detected` の後に `recovered` がある | `recovered` が優先（新しい順に読むこと） |

### `tests/test_cli.py`（新規または追記）

- `alpha-strike-watchdog` が `SIGNAL_WATCHDOG_ENABLED=0` で何もせず 0 を返す
- 途絶検知時も終了コードが 0
- 例外が起きても終了コードが 0

### `tests/test_webhook_server.py`（追記）

- **発注が直列化されること**: 2 本の webhook を同時に投げ、`order_router.route` が
  重ならずに実行される（開始・終了時刻の重複が無い）
- 発注中もイベントループが解放されていること: 発注をブロックさせた状態で
  `/status/events` が応答する

### 既存テストの回帰

`signal_watchdog_loop` 撤去後も、既存 29 テストのうちループ以外の 27 件が緑のままであること。
`TestSignalWatchdogLoop` の 2 件（`tests/test_signal_watchdog.py:361` 以降）は削除する。

## 10. デプロイ手順

**市場休場時間帯に実施する。**

1. PyPI へ `1.3.0` をリリース（`release.sh minor`）
2. VM で新版を導入

```bash
~/.local/bin/uv pip install --python /opt/alpha-strike/.venv/bin/python -U \
  "alpha-strike==1.3.0" "futu-api==10.06.6608"
```

3. `/etc/systemd/system/alpha-strike.service` を編集（`Requires=` → `Wants=`、`Restart=always`）
4. 新規 unit / timer を配置して有効化

**systemd unit はリポジトリ管理外**である（既存の `alpha-strike.service` / `moomoo-opend.service` も
VM 上にしか存在せず、`docs/ops/` は `systemctl cat` / `systemctl edit` で参照・編集する前提で書かれている）。
この慣習に合わせ、**新規 timer / service の全文とアプリ本体 unit の差分は `docs/ops/deployment.md` に記載**する。
リポジトリに `.service` / `.timer` ファイルを新設することはしない（既存 2 unit だけ管理外という
中途半端な状態を避けるため）。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now alpha-strike-watchdog.timer
sudo systemctl restart alpha-strike
```

5. 確認

```bash
systemctl list-timers alpha-strike-watchdog.timer
sudo systemctl start alpha-strike-watchdog.service
journalctl -u alpha-strike-watchdog -n 20
```

`signal watchdog: 最終受信=...` が出れば機能している。

**VM への反映は本作業の範囲外**とし、手順を提示して人間が実行する。

## 11. 受け入れ基準

- [ ] `uv run pytest` 緑・`uv run ruff check .` クリーン
- [ ] `signal_watchdog_loop` と lifespan の配線が削除され、判定ロジックは再利用されている
- [ ] `load_watchdog_state` がイベントログから状態を復元するテストが緑
- [ ] 発注が直列化されることを検証するテストが緑
- [ ] 発注ブロック中に `/status/events` が応答することを検証するテストが緑
- [ ] 新規 timer / service の全文と `alpha-strike.service` の差分が `docs/ops/deployment.md` に記載されている
- [ ] README / CLAUDE.md / mkdocs_src(ja,en) が更新され、mkdocs ビルド成果物も含まれる

## 12. やらないこと（YAGNI）

- **VM ごと停止した場合の外部監視**（別課題。systemd timer は VM 生存が前提）
- **OpenD 呼び出しのタイムアウト**（スレッドを中断できず、ワーカーを食い潰すため）
- **OpenD トークンの自動更新**（画像認証が必要で自動化できない）
- **カーネル自動更新の抑止**（セキュリティ更新を止める方が高リスク。再起動後に自動復帰する
  ようにするのが正しい対処）
