# Alpha-Strike

TradingView のアラートを受け取り、**OANDA証券**または**moomoo証券**へ自動発注する Webhook サーバーです。

## 概要

```
TradingView アラート
       ↓ (HTTP POST / JSON)
  Webhook サーバー (FastAPI)
       ↓                ↓
  OANDA REST API    moomoo OpenD
  (FX・CFD)         (米国株・香港株)
```

対応ブローカー:
- **OANDA証券** — FX・株式CFD・指数CFD・商品CFD（REST API v20）
- **moomoo証券** — 米国株・香港株（Futu OpenAPI / OpenD）

## クイックスタート

### 1. 依存パッケージのインストール

```bash
uv sync
```

### 2. 環境変数の設定

```bash
cp .env.example .env
```

`.env` を編集して認証情報を設定してください:

| 変数 | 必須 | 説明 |
|---|---|---|
| `WEBHOOK_PASSPHRASE` | 必須 | TradingView アラートの認証パスフレーズ |
| `LIVE_EVENTS_PATH` | 任意 | `SignalEvent` / `OrderEvent` / `FillEvent` の JSONL 保存先 |
| `OANDA_API_KEY` | OANDA使用時 | Personal Access Token |
| `OANDA_ACCOUNT_ID` | OANDA使用時 | 口座ID |
| `OANDA_ENV` | OANDA使用時 | `PRACTICE`（デモ）または `LIVE`（本番）|
| `MOOMOO_HOST` | moomoo使用時 | OpenD のホスト（デフォルト: `127.0.0.1`）|
| `MOOMOO_PORT` | moomoo使用時 | OpenD のポート（デフォルト: `11111`）|
| `MOOMOO_TRD_ENV` | moomoo使用時 | `SIMULATE`（デモ）または `REAL`（本番）|

> **重要**: テスト時は必ず `OANDA_ENV=PRACTICE` / `MOOMOO_TRD_ENV=SIMULATE` を設定してください。
> live trading analysis を使う場合は `LIVE_EVENTS_PATH` を `alpha-strategies/data/live/events` に向けてください。
> broker の同期レスポンスから約定価格が取れる場合は `FillEvent` も best-effort で保存されます。
> 現在は broker poller / callback から `POST /events/trade-closed` を呼ぶことで `TradeClosedEvent` を保存できます。
> moomoo では、同一 strategy / ticker の opposite-side fill を検出した場合、単一 open trade の split exit に加えて、複数 open lot をまたぐ close でも lot ごとの `TradeClosedEvent` を自動生成できます。
> OANDA でも、同一 strategy / ticker の opposite-side fill を検出した場合、単純な opposite-fill close に加えて、複数 open lot をまたぐ close を lot ごとに event 化します。
> opposite fill の数量が既存ポジションを上回る reversal では、クローズ分は `TradeClosedEvent` を出し、残数量は新しい `trade_id` を持つ `FillEvent` として残します。

### 3. サーバー起動

```bash
uv run uvicorn webhook_server:app --host 0.0.0.0 --port 8080 --reload
```

### 4. 動作確認

```bash
curl http://localhost:8080/health
# → {"status":"ok"}
```

Swagger UI: `http://localhost:8080/docs`

## Webhook ペイロード仕様

`POST /webhook` に以下の JSON を送信します。

```json
{
  "passphrase": "your-secret-passphrase",
  "broker": "oanda",
  "asset_class": "FX",
  "action": "buy",
  "ticker": "USDJPY",
  "quantity": 1000
}
```

| フィールド | 型 | 説明 |
|---|---|---|
| `passphrase` | string | 認証パスフレーズ（`.env` と一致させる）|
| `broker` | `"oanda"` \| `"moomoo"` | 発注先ブローカー |
| `asset_class` | string | アセットクラス（下記参照）|
| `action` | `"buy"` \| `"sell"` | 売買方向 |
| `ticker` | string | 銘柄コード |
| `quantity` | number | 注文数量（0より大きい値）|

## Trade Closed Event Ingestion

broker 側の照会や callback からクローズ情報を取り込むために、認証付き endpoint を用意しています。

`POST /events/trade-closed`

```json
{
  "passphrase": "your-secret-passphrase",
  "signal_id": "sig_usdjpy_20260330101500",
  "trade_id": "trd_20260330101502123456",
  "closed_at": "2026-03-31T11:05:00+09:00",
  "broker": "oanda",
  "asset_class": "FX",
  "action": "buy",
  "ticker": "USDJPY",
  "quantity": 1000,
  "entry_price": 149.235,
  "exit_price": 149.910,
  "gross_pnl": 675.0,
  "net_pnl": 655.0,
  "strategy_id": "sma_crossover_v1",
  "strategy_version": "1.2.0",
  "snapshot_id": "snap_20260329190300123456",
  "run_mode": "live",
  "commission": 20.0,
  "exit_reason": "signal_exit"
}
```

### OANDA の asset_class と ticker 変換

| asset_class | ticker 例 | OANDA instrument |
|---|---|---|
| `FX` | `USDJPY` | `USD_JPY` |
| `COMMODITY` | `XAUUSD` | `XAU_USD` |
| `US` | `AAPL` | `AAPL_USD` |
| `INDEX` | `NAS100` | `NAS100_USD` |
| その他 | `USD_JPY` | そのまま使用 |

### moomoo の asset_class と ticker 形式

| asset_class | ticker 形式 | 例 |
|---|---|---|
| `US` | `US.ティッカー` | `US.AAPL` |
| `HK` | `HK.XXXXX` | `HK.00700` |

## moomoo 証券を使う場合

**OpenD**（ローカルゲートウェイ）の起動が必要です。必ずサーバーより先に起動してください。

詳細は [docs/moomoo_futud.md](docs/moomoo_futud.md) を参照してください。

## TradingView の設定

アラートの「Webhook URL」と「Message」を設定します。詳細は [docs/tradingview.md](docs/tradingview.md) を参照してください。

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| [docs/setup.md](docs/setup.md) | 詳細なセットアップ手順 |
| [docs/tradingview.md](docs/tradingview.md) | TradingView アラート設定ガイド |
| [docs/webhook-payload-v2.md](docs/webhook-payload-v2.md) | live trading analysis 向け payload 拡張案 |
| [docs/moomoo_futud.md](docs/moomoo_futud.md) | OpenD セットアップガイド |

## 開発

```bash
# テスト実行
uv run pytest

# 型チェック・Lint（ruff が設定されている場合）
uv run ruff check .
```

## 要件

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- OANDA の Practice/Live 口座（OANDA使用時）
- moomoo の口座 + OpenD（moomoo使用時）
