# TradingView アラート設定ガイド

本ドキュメントは alpha-strike の Webhook サーバーを **TradingView Premium 以上のアラート機能** から呼び出すための設定手順をまとめたものです。

> **前提**: VM (`oracle-strike` = `alpha-strike-01`) が [VM プロビジョニング手順書](./ops/vm-provisioning.md) の §0〜§9 まで完了し、`https://strike.alforgelabs.com` で Cloudflare Tunnel 経由の公開 URL が稼働していること。OANDA / moomoo の認証情報も `/etc/alpha-strike/.env` に投入済みであること。

---

## 1. Webhook URL

| 項目 | 値 |
|---|---|
| URL | `https://strike.alforgelabs.com/webhook` |
| 経路 | TradingView → Cloudflare Edge → cloudflared (VM) → `localhost:8080` |
| 認証 | リクエストボディ内 `passphrase` フィールド（`/etc/alpha-strike/.env` の `WEBHOOK_PASSPHRASE` と一致） |
| Rate limit | `10 req/min/IP`（`slowapi` で実装、`webhook_server.py:97`） |

> **TradingView が HTTPS を要求**: TradingView のアラート機能は HTTPS のみ。`http://` は受理されない。Cloudflare Tunnel が自動で証明書を提供するため、別途証明書管理は不要。

---

## 2. Cloudflare Access の設定（重要）

TradingView の送信元 IP は固定 4 つが公開されている。`strike.alforgelabs.com` には **Cloudflare Access の認証ポリシーを設定しない**（= 公開エンドポイント）。代わりに以下のいずれかでアクセスを絞る。

### 2-A. Cloudflare WAF の Custom Rule（推奨）

Cloudflare ダッシュボード → **Security → WAF → Custom rules** で以下を作成：

| 項目 | 値 |
|---|---|
| Rule name | `Allow TradingView to /webhook` |
| If incoming requests match | `(http.host eq "strike.alforgelabs.com" and http.request.uri.path eq "/webhook" and not ip.src in {52.89.214.238 34.212.75.30 54.218.53.128 52.32.178.7})` |
| Then take action | `Block` |

> **TradingView 公式の送信元 IP**: 上記 4 つは [TradingView 公式 Help Center](https://www.tradingview.com/support/solutions/43000529348-about-webhooks/) で公開されている値。変更時はこのページで再確認すること。
>
> **passphrase との二重防御**: WAF で IP を絞っても、リクエストボディの `passphrase` 検証は必須。万一 TradingView の IP が変更されたときに即時遮断を解除できるよう、`passphrase` を強い秘密値（32+ 文字のランダム文字列）に設定しておく。

### 2-B. Cloudflare Access の Service Token（代替）

WAF Custom Rule が Free plan の枠を超える場合や、より柔軟なログを取りたい場合は Access Application + Service Token 方式も使える。ただし TradingView の Webhook はカスタムヘッダー送信に対応していないため、**通常は 2-A を選択**。

---

## 3. TradingView アラートの作成手順

1. TradingView のチャート画面で対象銘柄を表示
2. 右サイドメニューから **Alert（時計+ベル アイコン）** → **Create Alert**
3. **Condition** で Pine Script のストラテジー（`alert()` 関数 / `strategy.entry()` を含むもの）を選択
4. **Notifications** タブで **Webhook URL** にチェック → URL に `https://strike.alforgelabs.com/webhook` を入力
5. **Message** 欄に下記「アラートメッセージ JSON」を貼り付ける
6. **Create** で保存

> **Premium 以上必須**: TradingView の Webhook URL は **Premium plan 以上** でのみ利用可能。Pro / Pro+ では `alert()` の `message` 欄は使えるが Webhook 配信先は設定不可。

---

## 4. アラートメッセージ JSON

`WebhookPayload` のスキーマ定義は [`models.py`](../models.py) を参照。

### 4-1. moomoo SIMULATE（ペーパートレード）

**米国株を成行買い**

```json
{
  "passphrase": "<WEBHOOK_PASSPHRASE>",
  "broker": "moomoo",
  "asset_class": "US",
  "action": "buy",
  "ticker": "US.AAPL",
  "quantity": 10,
  "run_mode": "paper",
  "strategy_id": "demo_buy_v1",
  "alert_name": "{{strategy.order.alert_message}}"
}
```

**香港株を成行売り**

```json
{
  "passphrase": "<WEBHOOK_PASSPHRASE>",
  "broker": "moomoo",
  "asset_class": "HK",
  "action": "sell",
  "ticker": "HK.00700",
  "quantity": 100,
  "run_mode": "paper",
  "strategy_id": "demo_sell_v1"
}
```

> **moomoo の銘柄コードは `市場.コード` 形式**。米国株: `US.AAPL`、香港株: `HK.00700`、中国 A 株: `SH.600000`。TradingView の `{{ticker}}` は `AAPL` 形式なので、Pine 側で `"US." + syminfo.ticker` のように加工する（後述）。

### 4-2. OANDA PRACTICE（FX デモ口座）

**USD/JPY 1000 通貨買い**

```json
{
  "passphrase": "<WEBHOOK_PASSPHRASE>",
  "broker": "oanda",
  "asset_class": "FX",
  "action": "buy",
  "ticker": "USDJPY",
  "quantity": 1000,
  "run_mode": "paper",
  "strategy_id": "fx_demo_v1"
}
```

> **OANDA instrument 自動変換**: `asset_class` が `FX` / `COMMODITY` のときは `USDJPY` → `USD_JPY`、`US` / `INDEX` のときは `AAPL` → `AAPL_USD` に自動変換される。変換ロジックは `services/order_service.py` を参照。

### 4-3. 動的フィールド（TradingView プレースホルダ）

TradingView は `{{...}}` 形式のプレースホルダをアラート発火時に自動で埋め込む。代表例：

| プレースホルダ | 内容 | 用途 |
|---|---|---|
| `{{ticker}}` | 銘柄（例: `AAPL`） | `ticker` フィールドに展開 |
| `{{strategy.order.action}}` | `buy` / `sell` | `action` フィールドに展開 |
| `{{strategy.order.contracts}}` | 注文数量 | `quantity` フィールドに展開 |
| `{{strategy.position_size}}` | ポジションサイズ | 補助情報 |
| `{{strategy.order.alert_message}}` | Pine 側で `alert()` に渡した文字列 | `alert_name` などに展開 |
| `{{time}}` | アラート発火時刻（UTC ISO） | `alert_timestamp` に展開 |

> **JSON 内に直接埋め込めない**: TradingView のプレースホルダは数値や引用符を含むため、JSON の構造を壊さないように **文字列フィールドのみで使う** のが安全。`quantity` のような数値型に `{{strategy.order.contracts}}` を入れると、TradingView 側の出力次第で 422 になることがある。確実に動的化したい場合は Pine 側で完全な JSON を組み立てて `alert()` の引数に渡す（§5 参照）。

---

## 5. Pine Script から完全な JSON を生成する

Pine v6 で alpha-strike Webhook 用 JSON を組み立てる最小テンプレート：

```pinescript
//@version=6
strategy("alpha-strike webhook demo", overlay=true)

// === 設定値 ===
passphrase   = "<WEBHOOK_PASSPHRASE>"   // Pine の input.string() で隠す運用も検討
broker       = "moomoo"                 // "oanda" | "moomoo"
asset_class  = "US"
strategy_id  = "demo_buy_v1"
run_mode     = "paper"

// === シグナル例: RSI クロス ===
rsi_val = ta.rsi(close, 14)
long_signal  = ta.crossover(rsi_val,  30)
short_signal = ta.crossunder(rsi_val, 70)

// === JSON 生成ヘルパー ===
make_payload(string action, int qty) =>
    ticker_full = (asset_class == "US" or asset_class == "HK")
                   ? asset_class + "." + syminfo.ticker
                   : syminfo.ticker
    '{"passphrase":"' + passphrase + '",' +
    '"broker":"' + broker + '",' +
    '"asset_class":"' + asset_class + '",' +
    '"action":"' + action + '",' +
    '"ticker":"' + ticker_full + '",' +
    '"quantity":' + str.tostring(qty) + ',' +
    '"strategy_id":"' + strategy_id + '",' +
    '"run_mode":"' + run_mode + '"}'

// === 発注 + アラート ===
if long_signal
    strategy.entry("LONG", strategy.long, qty = 10)
    alert(make_payload("buy", 10), alert.freq_once_per_bar_close)

if short_signal
    strategy.close("LONG")
    alert(make_payload("sell", 10), alert.freq_once_per_bar_close)
```

> **`alert()` をストラテジー本体で発火** すると、TradingView アラートの **Message 欄を空にしたまま** `alert()` で組み立てた JSON がそのまま Webhook へ送られる。
>
> **`alert.freq_once_per_bar_close`** を使うと足確定時のみ発火し、リペイント・重複発注を抑制できる。

---

## 6. E2E 疎通テスト（手動 curl）

VM のサービスが起動した状態で、Mac から外部疎通を確認：

```bash
# 1. 認証失敗（passphrase 不一致 → 401）
curl -i -X POST https://strike.alforgelabs.com/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "passphrase": "WRONG",
    "broker": "moomoo",
    "asset_class": "US",
    "action": "buy",
    "ticker": "US.AAPL",
    "quantity": 1,
    "run_mode": "paper"
  }'

# 2. 認証成功（moomoo SIMULATE へ実発注 → 200 + order_id 返却）
WEBHOOK_PASSPHRASE=$(op item get "alpha-strike" --vault AlphaTrade --fields WEBHOOK_PASSPHRASE --reveal)
curl -i -X POST https://strike.alforgelabs.com/webhook \
  -H "Content-Type: application/json" \
  -d "{
    \"passphrase\": \"$WEBHOOK_PASSPHRASE\",
    \"broker\": \"moomoo\",
    \"asset_class\": \"US\",
    \"action\": \"buy\",
    \"ticker\": \"US.AAPL\",
    \"quantity\": 1,
    \"run_mode\": \"paper\",
    \"strategy_id\": \"e2e_smoke\"
  }"
```

レスポンス例（成功時）：

```json
{
  "status": "success",
  "broker": "moomoo",
  "ticker": "US.AAPL",
  "message": "{'order_id': 12345678}",
  "signal_id": "sig_xxxxxxxxxxxxxxxxxxxx",
  "order_id": "ord_xxxxxxxxxxxxxxxxxxxx",
  "broker_order_id": "12345678",
  "event_id": "evt_xxxxxxxxxxxxxxxxxxxx"
}
```

発注確認は VM 側で：

```bash
ssh oracle-strike
cd ~/dev/alpha-strike
.venv/bin/python scripts/show_simulate_status.py
```

---

## 7. よくあるエラーと対処

| HTTPステータス | 原因 | 対処 |
|---|---|---|
| 401 Unauthorized | `passphrase` 不一致 | `/etc/alpha-strike/.env` の `WEBHOOK_PASSPHRASE` と TradingView Message 欄の値を再確認 |
| 422 Unprocessable Entity | JSON パース失敗 / Field validation 失敗 | `broker` `action` `run_mode` の値は小文字、`ticker` は `^[A-Z0-9_.]{1,20}$`、`quantity` は正数 |
| 429 Too Many Requests | `slowapi` の rate limit (10/min/IP) 超過 | アラート頻度を抑える、または `webhook_server.py:97` の上限を見直す |
| 500 Internal Server Error | broker 認証情報未設定 | `journalctl -u alpha-strike -n 100 --no-pager` でエラー詳細を確認 |
| 502 Bad Gateway | broker API 呼び出し失敗 | `moomoo`: OpenD が起動しているか (`systemctl status moomoo-opend`)、`oanda`: API key の有効性 |
| 502 Bad Gateway（Cloudflare） | alpha-strike が落ちている | `systemctl status alpha-strike`、`journalctl -u alpha-strike` |
| 403 Forbidden（Cloudflare） | WAF Custom Rule で遮断 | 送信元 IP が `52.89.214.238 / 34.212.75.30 / 54.218.53.128 / 52.32.178.7` のいずれかか確認 |

---

## 8. 関連ドキュメント

- [VM プロビジョニング手順書](./ops/vm-provisioning.md) — Cloudflare Tunnel・SSH Access・OS セットアップ
- [Webhook ペイロード v2 仕様](./webhook-payload-v2.md) — `WebhookPayload` の詳細フィールド
- [moomoo OpenD セットアップ](./moomoo_futud.md) — OpenD CLI のインストールとデバイストークン認証
- [本格運用チェックリスト](./ops/paper-trading-go-live.md) — ペーパートレード本番運用の事前確認項目
