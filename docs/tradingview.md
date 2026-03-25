# TradingView アラート設定ガイド

## Webhook URL の設定

TradingView のアラート作成画面で以下を設定します。

- **Webhook URL**: `http://<サーバーのIPまたはドメイン>:8080/webhook`
  - ローカルテストには ngrok などのトンネリングツールが必要です
  - 例: `https://xxxx.ngrok-free.app/webhook`

## アラートメッセージ（JSON Body）

アラートの「Message」欄に以下のJSON形式で入力してください。

### IG証券への発注

**買い注文（FX: USD/JPY）**
```json
{
  "passphrase": "your-secret-passphrase",
  "broker": "ig",
  "asset_class": "FX",
  "action": "buy",
  "ticker": "USDJPY",
  "quantity": 1
}
```

**売り注文（FX: USD/JPY）**
```json
{
  "passphrase": "your-secret-passphrase",
  "broker": "ig",
  "asset_class": "FX",
  "action": "sell",
  "ticker": "USDJPY",
  "quantity": 1
}
```

> **注意**: IG証券の `ticker`（`epic`）はIG独自の識別子です。
> 例: USD/JPYは `CS.D.USDJPY.MINI.IP` など。
> IG APIの「Market Navigation」または取引画面で確認してください。

### moomoo証券への発注

**買い注文（米国株: Apple）**
```json
{
  "passphrase": "your-secret-passphrase",
  "broker": "moomoo",
  "asset_class": "US",
  "action": "buy",
  "ticker": "US.AAPL",
  "quantity": 10
}
```

**売り注文（香港株: テンセント）**
```json
{
  "passphrase": "your-secret-passphrase",
  "broker": "moomoo",
  "asset_class": "HK",
  "action": "sell",
  "ticker": "HK.00700",
  "quantity": 100
}
```

> **注意**: moomoo（Futu）の銘柄コードは `市場.コード` 形式です。
> 米国株: `US.AAPL`、香港株: `HK.00700`、中国A株: `SH.600000` など。

## ローカルテスト用 curl コマンド

サーバーが `localhost:8080` で起動している場合、以下でテストできます。

**認証テスト（401を確認）**
```bash
curl -X POST http://localhost:8080/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "passphrase": "wrong-passphrase",
    "broker": "ig",
    "asset_class": "FX",
    "action": "buy",
    "ticker": "USDJPY",
    "quantity": 1
  }'
```

**IG証券テスト（DEMO口座）**
```bash
curl -X POST http://localhost:8080/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "passphrase": "your-secret-passphrase",
    "broker": "ig",
    "asset_class": "FX",
    "action": "buy",
    "ticker": "CS.D.USDJPY.MINI.IP",
    "quantity": 1
  }'
```

**moomoo証券テスト（SIMULATEデモ口座）**
```bash
curl -X POST http://localhost:8080/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "passphrase": "your-secret-passphrase",
    "broker": "moomoo",
    "asset_class": "US",
    "action": "buy",
    "ticker": "US.AAPL",
    "quantity": 10
  }'
```

## よくあるエラーと対処法

| HTTPステータス | 原因 | 対処法 |
|---|---|---|
| 401 Unauthorized | passphrase が一致しない | `.env` の `WEBHOOK_PASSPHRASE` とJSONの `passphrase` を確認 |
| 422 Unprocessable Entity | JSONフォーマットが不正 | `broker`/`action` の値を確認（小文字のみ） |
| 500 Internal Server Error | APIキーなど設定が未設定 | `.env` の各証券会社の設定を確認 |
| 502 Bad Gateway | 注文API呼び出し失敗 | ネットワーク・OpenD起動状態・API残高を確認 |

## TradingView から外部URLへの接続

TradingView は HTTPS のみサポートしています。
ローカルサーバーへの接続には ngrok を使用してください。

```bash
# ngrok で 8080 ポートをトンネル
ngrok http 8080
# → https://xxxx.ngrok-free.app が発行される
```

本番環境では SSL証明書を設定したリバースプロキシ（nginx など）の使用を推奨します。
