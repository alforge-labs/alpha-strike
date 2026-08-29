# alpha-strike — Claude Code ガイド

> 親 `alpha-trade/CLAUDE.md` の 9-rule template・プロジェクト固有ガイド・ワークツリー / GitHub Flow / TDD / `uv` 等のルールに従うこと。本ファイルには alpha-strike 固有の事項のみ記載する。

## コマンド

```bash
# 依存パッケージのインストール
uv sync

# サーバー起動（開発）
uv run uvicorn alpha_strike.webhook_server:app --host 0.0.0.0 --port 8080 --reload

# テスト実行
uv run pytest

# 単一テストの実行
uv run pytest tests/test_oanda_handler.py -v

# Lint
uv run ruff check .
```

## アーキテクチャ

TradingView → `POST /webhook` → `webhook_server.py` → `OrderRouter` → `BrokerHandler` → 証券会社 API

ソースは src layout（`src/alpha_strike/`）。以下のパスはすべて `src/alpha_strike/` からの相対。

**エントリーポイント**:
- `webhook_server.py` — FastAPI アプリ定義（サーバー本体）
- `cli.py` — `alpha-strike` CLI（PyPI インストール後の起動エントリポイント）

**ブローカーハンドラー** (`handlers/`):
- `base.py` — `BrokerHandler` Protocol（DIP 用抽象インターフェース）
- `oanda_handler.py` — `OandaHandler` — OANDA REST API v20（`https://api-fxpractice.oanda.com` / `https://api-fxtrade.oanda.com`）
- `moomoo_handler.py` — `MoomooHandler` — moomoo/Futu OpenAPI（OpenD ローカルゲートウェイ経由）

**サービス** (`services/`):
- `order_service.py` — `OrderRouter`（Strategy パターン）: ブローカー名で対応ハンドラーへ委譲。`build_default_router()` で初期化。
- `fill_service.py` — `FillEventService`（SRP）: `FillEvent` 構築・FIFO 配分・`TradeClosedEvent` 生成。
- `idempotency.py` — `IdempotencyStore`: `signal_id` の重複 webhook を拒否。
- `sell_guard.py` — over-sell ガード（#74）: SELL 数量を broker 実保有でクランプ。
- `target_reconcile.py` — `target_qty` closed-loop 化（#80）: payload の目標数量と実保有の差分で発注数量を再解決。
- `market_state.py` — 市場オープン/クローズ判定（#89）: OpenD `get_market_state` ベース、`MarketStateProvider` Protocol で Fake 注入可。
- `carryover.py` — クローズ後シグナルの carry-over（#89）: SIMULATE で失効する注文を queue し、次の市場オープンで再発注するループ。
- `order_reconcile.py` / `pending_reconcile.py` — 約定照合と未終端注文（GTC 翌営業日約定等）の遅延再照合（#79）。
- `status_service.py` / `status_auth.py` — 口座ステータス API（`/status`）とそのトークン認証。
- `notifier.py` — ntfy 通知。
- `signal_watchdog.py` — シグナル途絶の検知と通知。**v1.3.0 以降はサーバー内の常駐ループではなく、`alpha-strike-watchdog` console script（systemd timer）から単発実行される**。本体のイベントループ凍結やプロセス停止に道連れにされないための分離。

**データモデル** (`models.py`):
- `WebhookPayload` — リクエストの入力スキーマ（`broker: Literal["oanda", "moomoo"]`）
- `OrderResult` — レスポンスの出力スキーマ
- `FillEvent`, `TradeClosedEvent`, `OrderEvent`, `SignalEvent` — イベントログ用モデル

## 重要な実装詳細

### OANDA instrument 変換

`handlers/oanda_handler.py` の `_to_oanda_instrument()` が `asset_class` に応じて変換を行う:
- `FX` / `COMMODITY`: `USDJPY` → `USD_JPY`（6文字を前3+_+後3）
- `US` / `INDEX`: `AAPL` → `AAPL_USD`（`_USD` を付与）
- その他: パススルー（OANDA形式で直接指定）

### OANDA の SELL 注文

`units` は SELL 時に負の値で送信する（例: `-100`）。

### moomoo のアセットクラス

`HK` → `OpenHKTradeContext`、それ以外 → `OpenUSTradeContext`。
moomoo の銘柄コードは `US.AAPL`、`HK.00700` 形式。

### OpenD の事前確認

`moomoo_handler.py` は発注前に OpenD（`MOOMOO_HOST:MOOMOO_PORT`）への TCP 接続確認（タイムアウト3秒）を行い、接続できない場合は `RuntimeError` を raise する。

## 環境変数

| 変数 | 説明 |
|---|---|
| `WEBHOOK_PASSPHRASE` | TradingView 認証パスフレーズ（必須）|
| `OANDA_API_KEY` | Personal Access Token |
| `OANDA_ACCOUNT_ID` | 口座ID |
| `OANDA_ENV` | `PRACTICE`（デフォルト）または `LIVE` |
| `MOOMOO_HOST` | OpenD ホスト（デフォルト: `127.0.0.1`）|
| `MOOMOO_PORT` | OpenD ポート（デフォルト: `11111`）|
| `MOOMOO_TRD_ENV` | `SIMULATE`（デフォルト）または `REAL` |
| `MOOMOO_TIME_IN_FORCE` | REAL の米国市場成行注文の有効期限。`GTC`（デフォルト）または `DAY`。HK / CRYPTO は常に `DAY`（#76）。**SIMULATE は moomoo 10.7 がペーパートレードの GTC を拒否するため、設定に関わらず `DAY` を強制**（#88） |
| `MOOMOO_TARGET_QTY_RECONCILE` | payload の `target_qty` を broker 実保有との差分で再解決する closed-loop 化（#80）。`1`（デフォルト）または `0` |
| `PENDING_RECONCILE_ENABLED` | 未終端注文（GTC 翌営業日約定等）の遅延再照合（#79）。`1`（デフォルト）または `0` |
| `PENDING_RECONCILE_INTERVAL_SECONDS` | 遅延再照合の間隔秒（デフォルト `600`） |
| `CARRYOVER_ENABLED` | クローズ後シグナルの carry-over 再発注ループ（#89）。`1`（デフォルト）または `0` |
| `CARRYOVER_RESUBMIT_INTERVAL_SECONDS` / `CARRYOVER_LOOKBACK_HOURS` / `CARRYOVER_MAX_RESUBMITS` | carry-over ループの間隔・遡及窓・再発注上限。遡及窓は土日（市場休場）を除いた実効時間で計測するため、金曜クローズ後シグナルも週末をまたいで再発注される（祝日は対象外） |
| `SIGNAL_WATCHDOG_ENABLED` | シグナル途絶の監視。`1`（デフォルト）または `0`。TradingView アラートのサイレント失効を検知して ntfy 通知する |
| `SIGNAL_WATCHDOG_INTERVAL_SECONDS` | 途絶チェックの想定実行間隔秒（デフォルト `3600`）。v1.3.0 以降は systemd timer が実際の間隔を決める |
| `SIGNAL_WATCHDOG_THRESHOLD_HOURS` | 途絶と判定する実効時間（土日除外、デフォルト `60`）。正常な週末跨ぎは実効 29h、米国祝日込みで 53h のため 2 セッション欠落で発報する |
| `SIGNAL_WATCHDOG_RENOTIFY_HOURS` | 途絶継続中の再通知の最小間隔（デフォルト `24`） |
| `SIGNAL_WATCHDOG_BROKER` | 監視対象 broker（デフォルト `moomoo`）。イベントの書き込み先ファイル名にも使う |
| `STATUS_API_TOKEN` | `/status` API の Bearer トークン |
