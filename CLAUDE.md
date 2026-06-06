# alpha-strike — Claude Code ガイド

> 親 `alpha-trade/CLAUDE.md` の 9-rule template・プロジェクト固有ガイド・ワークツリー / GitHub Flow / TDD / `uv` 等のルールに従うこと。本ファイルには alpha-strike 固有の事項のみ記載する。

## コマンド

```bash
# 依存パッケージのインストール
uv sync

# サーバー起動（開発）
uv run uvicorn webhook_server:app --host 0.0.0.0 --port 8080 --reload

# テスト実行
uv run pytest

# 単一テストの実行
uv run pytest tests/test_oanda_handler.py -v

# Lint
uv run ruff check .
```

## アーキテクチャ

TradingView → `POST /webhook` → `webhook_server.py` → `OrderRouter` → `BrokerHandler` → 証券会社 API

**エントリーポイント**:
- `webhook_server.py` — FastAPI アプリ定義（サーバー本体）
- `main.py` — PyInstaller バイナリ用ラッパー（uvicorn.run を直接呼び出す）

**ブローカーハンドラー** (`handlers/`):
- `base.py` — `BrokerHandler` Protocol（DIP 用抽象インターフェース）
- `oanda_handler.py` — `OandaHandler` — OANDA REST API v20（`https://api-fxpractice.oanda.com` / `https://api-fxtrade.oanda.com`）
- `moomoo_handler.py` — `MoomooHandler` — moomoo/Futu OpenAPI（OpenD ローカルゲートウェイ経由）

**サービス** (`services/`):
- `order_service.py` — `OrderRouter`（Strategy パターン）: ブローカー名で対応ハンドラーへ委譲。`build_default_router()` で初期化。
- `fill_service.py` — `FillEventService`（SRP）: `FillEvent` 構築・FIFO 配分・`TradeClosedEvent` 生成。

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
| `MOOMOO_TIME_IN_FORCE` | 米国市場の成行注文の有効期限。`GTC`（デフォルト）または `DAY`。HK / CRYPTO は常に `DAY`（#76） |
| `MOOMOO_TARGET_QTY_RECONCILE` | payload の `target_qty` を broker 実保有との差分で再解決する closed-loop 化（#80）。`1`（デフォルト）または `0` |
| `PENDING_RECONCILE_ENABLED` | 未終端注文（GTC 翌営業日約定等）の遅延再照合（#79）。`1`（デフォルト）または `0` |
| `PENDING_RECONCILE_INTERVAL_SECONDS` | 遅延再照合の間隔秒（デフォルト `600`） |
