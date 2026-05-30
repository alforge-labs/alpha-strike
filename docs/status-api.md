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

口座残高・建玉はインターネット公開ホスト（`strike.alforgelabs.com`）上で返るため、ネットワーク層でも保護する。oracle-strike の SSH で既に使用している **Cloudflare Access（ゼロトラスト）** を `/status*` パスにも適用する:

1. Cloudflare Zero Trust → Access → Applications で `strike.alforgelabs.com/status*`（および `/status/events`）を対象に Self-hosted Application を作成。
2. 許可ポリシー（例: 自分の Email / Google ログイン）を設定。
3. `/webhook` は対象に**含めない**（TradingView からの POST を Access が遮断しないよう、従来どおり WAF の IP 許可リストで保護）。

> Cloudflare の UI 名称はバージョンで変わるため、実機で確認のこと。`/webhook`（WAF IP 許可）と `/status*`（Access 認証）を**パスで分離**するのが要点。

## 環境変数

| 変数 | 用途 |
|------|------|
| `STATUS_API_TOKEN` | status API の Bearer トークン。未設定で API 無効（503） |
| `MOOMOO_HOST` / `MOOMOO_PORT` | OpenD 接続先（既定 127.0.0.1:11111） |
| `MOOMOO_TRD_ENV` | 既定の取引環境（SIMULATE / REAL） |

## Phase 2（予定）

発注後に OpenD で最終 order status を照合し、Fill / Cancelled を ntfy にプッシュ通知する（issue #57 B）。
