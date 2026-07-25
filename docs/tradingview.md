# TradingView アラート設定ガイド

本ドキュメントは alpha-strike の Webhook サーバーを **TradingView Essential 以上のアラート機能** から呼び出すための設定手順をまとめたものです。

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

> **Essential 以上必須**: TradingView の Webhook URL は **Essential plan 以上** でのみ利用可能（Essential はアラート 20 件まで、Plus は 100 件まで、Premium は 400 件まで等の上限差はあるが、Webhook 機能自体は Essential から利用できる）。Free / Basic では Webhook 配信先を設定できない。

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

> **moomoo の銘柄コードは `市場.コード` 形式**。米国株: `US.AAPL`、香港株: `HK.00700`、中国 A 株: `SH.600000`、暗号資産: `CC.BTC` / `CC.ETH` / `CC.XRP`。TradingView の `{{ticker}}` は `AAPL` 形式なので、Pine 側で `"US." + syminfo.ticker` のように加工する（後述）。

**暗号資産（CRYPTO）を成行買い**

```json
{
  "passphrase": "<WEBHOOK_PASSPHRASE>",
  "broker": "moomoo",
  "asset_class": "CRYPTO",
  "action": "buy",
  "ticker": "CC.BTC",
  "quantity": 0.01,
  "run_mode": "paper",
  "strategy_id": "btc_ema_sma_trail40_v1"
}
```

> **暗号資産の前提**:
> - moomoo の crypto は 24/7 取引・unlimited T+0（米国居住者制限あり、`run_mode=paper` でも broker 側のアカウント有効化が前提）
> - SDK 内部では `OpenSecTradeContext(filter_trdmarket=TrdMarket.CRYPTO, security_firm=SecurityFirm.NONE)` を使用（`handlers/moomoo_handler.py` 参照）
> - 銘柄コード: `CC.BTC`, `CC.ETH`, `CC.XRP` 等（米国大文字 + `CC.` プレフィックス）

> ⚠️ **moomoo crypto は live (REAL) only — SIMULATE 不可**:
> moomoo の crypto trading API は live 環境専用で、`MOOMOO_TRD_ENV=SIMULATE` で
> crypto order を送ると SDK が `the type of environment param is wrong` を返す。
> alpha-strike は **OpenD 接続前にこの組み合わせを検出して `ValueError` で早期拒否** する
> （`handlers/moomoo_handler.py`、テスト: `test_crypto_with_simulate_raises_value_error_before_connect`）。
>
> ペーパー検証したい場合は以下のいずれか:
> - **BTC ETF (`US.IBIT` / `US.FBTC` / `US.BITO`) を `asset_class=US` で発注**（推奨）
> - `MOOMOO_TRD_ENV=REAL` で少額（0.001 BTC ≈ $80）から開始
> - alpha-strike を経由せず TradingView Paper Trading 内蔵を使う

**target_qty による closed-loop 数量解決（#80）**

`quantity`（増減量）に加えて任意フィールド `target_qty`（目標絶対保有量、`>= 0`）を併送すると、moomoo では broker 実保有との差分から発注数量・方向が再解決される。送信側の想定保有が 0 約定・部分約定・端数で実保有とズレても、次のシグナルで自動的に収束する。

```json
{
  "passphrase": "<WEBHOOK_PASSPHRASE>",
  "broker": "moomoo",
  "asset_class": "US",
  "action": "buy",
  "ticker": "US.AAPL",
  "quantity": 10,
  "target_qty": 47,
  "run_mode": "paper",
  "strategy_id": "demo_buy_v1"
}
```

> - 実保有 40 → `buy 7` に補正 / 実保有 50 → `sell 3` に補正 / 実保有 47 → skip（broker へ送らない）
> - `target_qty: 0` は全決済を意味する
> - `quantity` は `target_qty` 非対応バージョン向けのフォールバック値（増減量）として必須のまま
> - OANDA はポジション照会未実装のため `target_qty` を無視して従来どおり `quantity` を発注する
> - 無効化は env `MOOMOO_TARGET_QTY_RECONCILE=0`（旧 delta 解釈へのロールバック）

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
// asset_class が US / HK / CRYPTO のときは market prefix を付けて moomoo フォーマットに揃える
//   US     → "US.AAPL"
//   HK     → "HK.00700"
//   CRYPTO → "CC.BTC"
get_market_prefix(string ac) =>
    ac == "US"     ? "US." :
    ac == "HK"     ? "HK." :
    ac == "CRYPTO" ? "CC." :
                     ""

make_payload(string action, float qty) =>
    prefix = get_market_prefix(asset_class)
    ticker_full = prefix + syminfo.ticker
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

## 6. E2E 疎通テスト

### 6-1. 一括スモークテスト（推奨）

`scripts/go_live_smoke.sh` で `/health` → `/webhook 401` → `/webhook 200 (実発注)` → 着弾確認 → 取消 を一気通貫で実行できる：

```bash
# Mac から (1Password CLI 認証済み + ssh oracle-strike 疎通済みが前提)
./scripts/go_live_smoke.sh             # 各段で y/n 確認
./scripts/go_live_smoke.sh --yes       # 全段を自動実行
./scripts/go_live_smoke.sh --dry-run   # 401 確認まで（実発注なし）
```

### 6-2. 手動 curl

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

## 6-bis. Idempotency（重複発注の自動拒否）

TradingView Webhook は **ネットワーク再送・alert 再評価・Restart 連打** などで同一シグナルが複数回到達することがある。alpha-strike は **`(signal_id, broker, ticker, action)` の複合キーを idempotency key として使い**、TTL 内に同じ組み合わせが再到達した場合は broker に流さず 200 を返す（TradingView 側の自動リトライを止めるため、409 にはしない）。

> **なぜ複合キーなのか (#126)**: `signal_id` は通常 bar 単位で払い出すため、**同一バーで複数銘柄をリバランスする戦略では 3 銘柄が同じ `signal_id` を共有する**。`signal_id` 単独をキーにすると 2 銘柄目以降が重複として捨てられ、TradingView 側には "successfully delivered" と表示されたままリバランスだけが欠落する。銘柄・売買方向まで含めて 1 シグナルとして扱うことでこれを防いでいる。

### 動作仕様

| 条件 | 動作 |
|---|---|
| `signal_id` 指定あり + 初回到達 | 通常の発注フローを実行 |
| `signal_id` 指定あり + TTL 内に **同一 broker/ticker/action** で再到達 | broker 呼び出しスキップ、200 `{"status":"success", "message":"duplicate signal_id — already processed"}` を返す |
| `signal_id` 指定あり + TTL 内に **別の ticker または action** で到達 | 別シグナルとして通常の発注フローを実行 |
| `signal_id` 指定あり + TTL 経過後の再到達 | 通常の発注フローを実行（TTL 切れで履歴破棄済み） |
| `signal_id` 未指定 | idempotency 検証スキップ（従来通り、毎回 broker に流れる） |

### 設定

| 環境変数 | 既定 | 説明 |
|---|---|---|
| `IDEMPOTENCY_TTL_SECONDS` | `600` | 重複拒否対象とする保持期間（秒）。TradingView 自動リトライ最長間隔をカバーする値。プロセス内 in-memory のため restart 時は履歴破棄される |

### Pine スクリプトから一意な `signal_id` を生成する推奨パターン

bar 確定時刻 + strategy_id + timeframe で **同一バー内の再発火を必ず idempotency で弾ける** 形にする：

```pinescript
//@version=6
strategy("idempotent demo", overlay=true)

strategy_id = "demo_buy_v1"
timeframe   = timeframe.period
passphrase  = "<WEBHOOK_PASSPHRASE>"

make_signal_id() =>
    // bar 確定時刻を ISO 風文字列で signal_id に埋め込む
    strategy_id + "_" + timeframe + "_" + str.format_time(time, "yyyy-MM-dd_HH-mm")

make_payload(string action, float qty) =>
    sig = make_signal_id()
    '{"passphrase":"' + passphrase + '",' +
    '"broker":"moomoo",' +
    '"asset_class":"US",' +
    '"action":"' + action + '",' +
    '"ticker":"US." + syminfo.ticker + '",' +
    '"quantity":' + str.tostring(qty) + ',' +
    '"signal_id":"' + sig + '",' +
    '"strategy_id":"' + strategy_id + '",' +
    '"run_mode":"paper"}'
```

> **同一バー内の再発火を防ぐ効果**: 上記パターンで `signal_id` を `<strategy_id>_<timeframe>_<bar_open_time>` 形式にすると、同一バーで複数回 `alert()` 発火しても 2 回目以降は alpha-strike 側で必ず 200 + duplicate 扱いされる。
>
> **複数銘柄を同時にリバランスする場合**: この形式では銘柄が `signal_id` に含まれないため、同一バーの銘柄別アラートは同じ `signal_id` を共有する。alpha-strike は `ticker` / `action` まで含めて重複判定するので **そのままで正しく全銘柄が発注される** (#126)。`signal_id` に銘柄を含めても構わないが、必須ではない。

### `signal_id` が含まれていない場合

`signal_id` フィールドを payload に含めない（または空文字列）と、idempotency 検証はスキップされ alpha-strike 側で `sig_XXXXXX` を自動採番する。後方互換のため、既存の payload はそのまま動作するが、**重複発注リスクを低減したい場合は明示的に `signal_id` を含める** ことを強く推奨。

---

## 7. よくあるエラーと対処

| HTTPステータス | 原因 | 対処 |
|---|---|---|
| 401 Unauthorized | `passphrase` 不一致 | `/etc/alpha-strike/.env` の `WEBHOOK_PASSPHRASE` と TradingView Message 欄の値を再確認 |
| 422 Unprocessable Entity | JSON パース失敗 / Field validation 失敗 | `broker` `action` `run_mode` の値は小文字、`ticker` は `^[A-Z0-9_.]{1,20}$`、`quantity` は正数、`target_qty` は 0 以上 |
| 429 Too Many Requests | `slowapi` の rate limit (10/min/IP) 超過 | アラート頻度を抑える、または `webhook_server.py:97` の上限を見直す |
| **503 Service Unavailable** | **Kill switch (maintenance mode) が ON** | サーバー側で `/etc/alpha-strike/MAINTENANCE` ファイルが存在する、もしくは `MAINTENANCE_MODE=1` が設定されている。`sudo rm /etc/alpha-strike/MAINTENANCE` で解除（[paper-trading-go-live.md §5-3](./ops/paper-trading-go-live.md) 参照） |
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
