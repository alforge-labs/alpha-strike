# Status API（read-only トレード状況確認）

issue #57 Phase 1。SSH + `scripts/show_simulate_status.py` を都度実行せずに、HTTP でトレード状況を確認するための read-only API。

## エンドポイント

| メソッド | パス | 内容 |
|---------|------|------|
| GET | `/status` | 口座サマリ（総資産/現金/買付余力）+ 保有建玉 + 直近注文（**実 order_status 付き**） |
| GET | `/status/events` | 保存済み JSONL イベント（SignalEvent / OrderEvent / FillEvent / TradeClosedEvent）を新しい順で |

### `/status`

クエリ:
- `trd_env` (任意): `SIMULATE` / `REAL`。省略時は `MOOMOO_TRD_ENV`。

応答例:

```json
{
  "broker": "moomoo",
  "trd_env": "SIMULATE",
  "account": {"total_assets": 1000005.58, "cash": 999557.31, "power": 1999562.88, "market_val": 448.27},
  "positions": [
    {"code": "US.AAPL", "qty": 1.0, "can_sell_qty": 1.0, "cost_price": 300.04, "nominal_price": 312.06, "market_val": 312.06, "pl_val": 12.02, "pl_ratio": 4.0}
  ],
  "recent_orders": [
    {"code": "US.GLD", "trd_side": "SELL", "order_type": "MARKET", "qty": 1.0, "order_status": "CANCELLED_ALL", "dealt_qty": 0.0, "dealt_avg_price": 0.0, "order_id": "366675", "create_time": "2026-05-29 16:01:21"}
  ]
}
```

> **設計原則**: webhook 受信ログ（submission）ではなく **broker（OpenD）由来の実 order/fill ステータス** を正とする。上の例のように `order_status=CANCELLED_ALL` / `dealt_qty=0.0` がそのまま見えるため、「注文成功ログなのに実は未約定」という乖離を即座に把握できる。

### `/status/events`

クエリ: `broker` / `event_type` / `ticker` / `strategy_id`（フィルタ）, `limit`（1〜500、既定 50）。

応答: `{"count": N, "events": [...]}`。

## 認証（二重防御）

### 1. コード層: Bearer トークン（必須）

全 `/status*` は `Authorization: Bearer <STATUS_API_TOKEN>` を必須とする。

- `STATUS_API_TOKEN` 未設定時は **503 で無効化**（fail-safe。デフォルトでは機微情報を一切公開しない）。
- 設定時、トークン不一致・欠落は 401。比較は定数時間（`hmac.compare_digest`）。

```bash
# トークン生成
openssl rand -hex 32
# .env に設定
STATUS_API_TOKEN=<生成した値>

# 呼び出し
curl -H "Authorization: Bearer $STATUS_API_TOKEN" https://strike.alforgelabs.com/status
```

### 2. ネットワーク層: Cloudflare Access（強く推奨）

口座残高・建玉はインターネット公開ホスト（`strike.alforgelabs.com`）上で返るため、ネットワーク層でも保護する。oracle-strike の SSH で既に使用している **Cloudflare Access（ゼロトラスト）** を `/status*` パスにも**パス単位で**適用する。

**設計上の要点:**

- `/status*` は origin が `Authorization: Bearer` を要求する。Access を前段に置くと curl は「**Access 認証 + Bearer**」の二段になる。
- curl / スクリプト運用が前提のため、**Access の認証方式は Service Token** を採用する（ブラウザの対話ログインだと origin の Bearer で 401 になる。ブラウザ閲覧したい場合は origin 側で Cloudflare Access JWT（`Cf-Access-Jwt-Assertion`）を検証する改修が別途必要）。
- `/webhook` は **Access 対象に含めない**（TradingView の POST を遮断しないよう、従来どおり [WAF IP 許可リスト](tradingview.md)（§2-A）で保護）。パスで分離するのが要点。

#### 2-1. ダッシュボードで設定する場合

> UI 名称はバージョンで変わるため、近い項目を実機で確認しながら進める。

1. **Service Token 作成**: Zero Trust → Access → Service Auth → **Service Tokens** → Create →
   名前 `alpha-strike-status` → **Client ID** / **Client Secret** を保管（Secret は一度だけ表示）。
2. **Access Application 作成**: Zero Trust → Access → Applications → Add → **Self-hosted** →
   - Application domain: `strike.alforgelabs.com` / **Path**: `status`（`/status` と `/status/events` を含む。サブパスが別扱いなら `status/events` のアプリも追加）。
   - **Path を空にしない**（空だと `/webhook` まで Access 対象になり TradingView が遮断される）。
3. **ポリシー**: Action **Service Auth** / Include **Service Token** = `alpha-strike-status`。

#### 2-2. Cloudflare API で自動化する場合

`Account → Access: Apps and Policies: Edit` + `Access: Service Tokens: Edit` 権限の API トークンを使う（`$CF_API_TOKEN`）。`$ACCOUNT_ID` は `GET /accounts` で取得。

```bash
AUTH=(-H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json")
BASE=https://api.cloudflare.com/client/v4/accounts/$ACCOUNT_ID

# (a) Service Token 作成（client_id / client_secret を控える。secret は応答時のみ）
curl -s "${AUTH[@]}" "$BASE/access/service_tokens" -d '{"name":"alpha-strike-status"}'

# (b) Access Application 作成（self_hosted, path=/status）→ 応答の id を APP_ID に
curl -s "${AUTH[@]}" "$BASE/access/apps" -d '{
  "name":"alpha-strike status","type":"self_hosted",
  "domain":"strike.alforgelabs.com/status","session_duration":"24h"
}'

# (c) /status/events 用に同様にもう 1 アプリ（domain を .../status/events に）

# (d) 各アプリにポリシー（service_token を include）
curl -s "${AUTH[@]}" "$BASE/access/apps/$APP_ID/policies" -d '{
  "name":"status service token","decision":"non_identity",
  "include":[{"service_token":{"token_id":"<SERVICE_TOKEN_ID>"}}]
}'
```

> Cloudflare API のフィールド名・エンドポイントはバージョンで変わりうる。最新は [Cloudflare API docs](https://developers.cloudflare.com/api/) で確認。

#### 2-3. 動作確認

```bash
# CF-Access ヘッダ無し → Cloudflare が 403/ログイン（ネットワーク層で遮断）
curl -s -o /dev/null -w "%{http_code}\n" https://strike.alforgelabs.com/status

# Access 通過 + Bearer 無し → origin 401（アプリ層で遮断）
curl -s -o /dev/null -w "%{http_code}\n" https://strike.alforgelabs.com/status \
  -H "CF-Access-Client-Id: <Client ID>" -H "CF-Access-Client-Secret: <Client Secret>"

# 両方あり → 200 + JSON
curl -s https://strike.alforgelabs.com/status \
  -H "CF-Access-Client-Id: <Client ID>" \
  -H "CF-Access-Client-Secret: <Client Secret>" \
  -H "Authorization: Bearer <STATUS_API_TOKEN>"
```

これで「ネットワーク層（Access Service Token）＋アプリ層（Bearer）」の二重防御になる。

## 運用ヘルパー: `scripts/status_curl.sh`

二段認証（Cloudflare Access Service Token + Bearer）の curl を 1 コマンドにまとめたヘルパー。
認証情報は op:// 参照（`op read`）か直接 env から解決する（値は標準出力に出さない）。

```bash
# ~/.zshrc 等に一度だけ（実値は 1Password に置く）
export STRIKE_CF_ID_REF="op://AlphaTrade/alpha-strike-status/CF-Access-Client-Id"
export STRIKE_CF_SECRET_REF="op://AlphaTrade/alpha-strike-status/CF-Access-Client-Secret"
export STRIKE_STATUS_TOKEN_REF="op://AlphaTrade/alpha-strike/STATUS_API_TOKEN"

# 実行
scripts/status_curl.sh            # /status（口座サマリ + 建玉 + 直近注文）
scripts/status_curl.sh events 20  # /status/events?limit=20
```

op を使わない場合は `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` / `STATUS_API_TOKEN` を直接 env で渡す。`STRIKE_BASE_URL` で接続先を上書きできる（既定 `https://strike.alforgelabs.com`）。jq があれば整形表示する。

## 環境変数

| 変数 | 用途 |
|------|------|
| `STATUS_API_TOKEN` | status API の Bearer トークン。未設定で API 無効（503） |
| `MOOMOO_HOST` / `MOOMOO_PORT` | OpenD 接続先（既定 127.0.0.1:11111） |
| `MOOMOO_TRD_ENV` | 既定の取引環境（SIMULATE / REAL） |

## Phase 2: 約定 reconcile（イベント永続化 + プッシュ通知）

moomoo の発注後、`ORDER_RECONCILE_DELAY_SECONDS`（既定 5 秒）待ってから OpenD で**最終 order status を照合（reconcile）**する。これにより「webhook は注文成功ログなのに実際は `CANCELLED_ALL`」のような **submission ≠ fill の盲点を source（alpha-strike）で確定**する。

### 1. 権威イベントの永続化（常時）

照合結果を **`OrderReconciledEvent`（`event_type=order_reconciled`）として常に JSONL に永続化**する（ntfy の有効/無効に依存しない）。

- フィールド: `order_status`（OpenD 由来の最終 status）/ `dealt_qty` / `dealt_avg_price` / `is_filled`（FILLED 系かつ dealt_qty>0）/ `broker_order_id` / `signal_id` / `order_id` ほか。
- order が見つからない場合は `order_status="NOT_FOUND"` / `is_filled=False` で記録（沈黙させない）。
- **下流の扱い**: 同一 order_id について `order_reconciled` が存在する場合、`fill_received`（submission 応答ベースで楽観的）より **`order_reconciled` を優先**する（forge live 等での live 集計の正となる）。

### 2. ntfy プッシュ通知（有効時のみ）

- `NTFY_TOPIC` 設定時のみ通知（未設定なら no-op）。失敗は握りつぶしてサーバーを落とさない。
- 通知内容: ticker / action / qty / `order_status` / `dealt_qty` / order_id。
  - `FILLED_ALL` / `FILLED_PART` → ✅（white_check_mark）/ `CANCELLED_ALL` 等 → ⚠️（warning, high）/ pending → ⏳ / 未発見 → ⏳ 照合不可。

いずれもバックグラウンドタスクで実行（webhook レスポンスをブロックしない）。

### 環境変数

| 変数 | 用途 |
|------|------|
| `NTFY_TOPIC` | ntfy トピック。未設定で通知無効 |
| `NTFY_SERVER` | ntfy サーバー（既定 https://ntfy.sh） |
| `ORDER_RECONCILE_DELAY_SECONDS` | 発注から status 照合までの待機秒数（既定 5） |

> 成行注文は市場開場時間中のみ約定する。**REAL の**米国市場はクローズ後に受けた注文を GTC で翌営業日寄付に持ち越すため（#76、`MOOMOO_TIME_IN_FORCE` 既定 `GTC`）、休場中の発注は reconcile 時点では pending（`SUBMITTED`）と表示され、翌営業日寄付で約定する。`DAY` 指定時（旧挙動）は持ち越されず cancelled になる。**SIMULATE（ペーパー）は moomoo 10.7 が GTC を拒否するため常に `DAY` で発注され、クローズ後シグナルは翌寄付に持ち越されず失効する（moomoo paper の仕様制約）。**
