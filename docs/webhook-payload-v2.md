# Webhook Payload v2 Draft

このドキュメントは、`alpha-strike` を live trading analysis 対応へ拡張するための webhook payload 改訂案です。

---

## 目的

現在の payload は注文執行には十分ですが、後から以下を分析するには情報が不足しています。

- どの戦略がシグナルを出したか
- どの戦略バージョンで運用していたか
- バックテストや最適化時のスナップショットとどう結びつくか
- シグナルは出たが注文失敗したケース
- 実売買とバックテスト成績の差

そのため、既存の最小 payload を保ちつつ、分析用メタデータを追加する v2 を導入します。

---

## 後方互換ポリシー

- 既存の v1 payload は引き続き受け付ける
- 新規フィールドは最初は optional とする
- 実装初期は「追加情報があれば記録する」方針を取る
- 将来的に live tracking を標準化する段階で一部項目を必須化する

---

## v1

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

---

## v2 推奨 payload

```json
{
  "passphrase": "your-secret-passphrase",
  "broker": "oanda",
  "asset_class": "FX",
  "action": "buy",
  "ticker": "USDJPY",
  "quantity": 1000,
  "strategy_id": "sma_crossover_v1",
  "strategy_version": "1.2.0",
  "snapshot_id": "snap_20260329190300123456",
  "signal_id": "sig_usdjpy_20260330101500",
  "timeframe": "1h",
  "alert_timestamp": "2026-03-30T10:15:00+09:00",
  "run_mode": "live",
  "alert_name": "SMA Crossover Long",
  "order_comment": "generated-by-alpha-forge"
}
```

---

## フィールド一覧

| フィールド | 必須 | 説明 |
|---|---|---|
| `passphrase` | 必須 | 認証パスフレーズ |
| `broker` | 必須 | `oanda` または `moomoo` |
| `asset_class` | 必須 | アセットクラス |
| `action` | 必須 | `buy` / `sell` |
| `ticker` | 必須 | 銘柄コード |
| `quantity` | 必須 | 注文数量（delta = 増減量。`target_qty` 非対応バージョン向けのフォールバック値） |
| `target_qty` | 任意 | 目標絶対保有量（`>= 0`、#80）。指定時（moomoo のみ）は broker 実保有との差分から発注数量・方向を再解決する（closed-loop）。`0` は全決済。未指定なら従来どおり `quantity` を delta として発注 |
| `strategy_id` | 推奨 | `alpha-forge` の戦略ID |
| `strategy_version` | 推奨 | 戦略バージョン |
| `snapshot_id` | 推奨 | 戦略スナップショットID |
| `signal_id` | 推奨 | シグナル一意ID |
| `timeframe` | 任意 | `1m`, `1h`, `1d` など |
| `alert_timestamp` | 任意 | シグナル発火時刻 |
| `run_mode` | 任意 | `paper` / `live` |
| `alert_name` | 任意 | TradingView アラート名 |
| `order_comment` | 任意 | 追加メモ |

---

## 初回実装で優先する項目

まずは以下の 4 つを追加すれば十分です。

- `strategy_id`
- `strategy_version`
- `snapshot_id`
- `signal_id`

この 4 項目があれば、`alpha-strategies` に保存する live trade records を `alpha-forge` の戦略履歴と結びつけられます。

---

## サーバー側の扱い

### `signal_id` が無い場合

`alpha-strike` 側で採番する。

### `strategy_id` が無い場合

注文は実行するが、live analysis では「戦略不明」のイベントとして保存する。

### `run_mode=paper`

将来的には broker 送信をスキップし、記録だけ残すモードとして使える。
初期段階では単なるログ属性として持つだけでもよい。

---

## 実装メモ

Pydantic モデルは既存 `WebhookPayload` をそのまま拡張する案が第一候補です。

```python
class WebhookPayload(BaseModel):
    passphrase: str
    broker: Literal["oanda", "moomoo"]
    asset_class: Literal["FX", "COMMODITY", "US", "HK", "INDEX"]
    action: Literal["buy", "sell"]
    ticker: str
    quantity: float

    strategy_id: str | None = None
    strategy_version: str | None = None
    snapshot_id: str | None = None
    signal_id: str | None = None
    timeframe: str | None = None
    alert_timestamp: datetime | None = None
    run_mode: Literal["paper", "live"] = "live"
    alert_name: str | None = None
    order_comment: str | None = None
```

---

## 関連ドキュメント

- `alpha-forge/docs/live-trading-data-model.md`
