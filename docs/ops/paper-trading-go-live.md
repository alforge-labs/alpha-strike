# ペーパートレード本格運用 Go-Live チェックリスト

alpha-strike を **moomoo SIMULATE 口座でのペーパートレード本格運用** に投入する前に、以下を上から順に確認すること。

> **対象環境**: VM `oracle-strike` (`alpha-strike-01`) / Oracle Cloud Always Free / E2.1.Micro
> **公開 URL**: `https://strike.alforgelabs.com/webhook`
> **broker**: moomoo SIMULATE（米国株中心、香港株も可）
> **想定利用者**: 個人運用、TradingView Essential プラン以上から 1 戦略を流す段階

---

## 0. 事前準備

- [ ] TradingView Essential 以上のアカウントを保有している（Webhook 機能必須）
- [ ] moomoo SIMULATE 口座が有効（`scripts/show_simulate_status.py` で総資産が取得できる）
- [ ] 1Password の `AlphaTrade` vault に `alpha-strike` アイテムがあり、`WEBHOOK_PASSPHRASE` / `MOOMOO_LOGIN_ACCOUNT` 等が登録済み

---

## 1. インフラ層（VM + Cloudflare）

### 1-1. サービス起動状態

```bash
ssh oracle-strike
sudo systemctl is-active cloudflared alpha-strike moomoo-opend
```

- [ ] `cloudflared`: `active`
- [ ] `alpha-strike`: `active`
- [ ] `moomoo-opend`: `active`

### 1-2. Tunnel ルーティング

```bash
curl -i https://strike.alforgelabs.com/health
```

- [ ] HTTP 200、ボディが `{"status":"ok"}` 系（`webhook_server.py:310` の `/health` レスポンス）
- [ ] Cloudflare ヘッダー（`cf-ray`、`server: cloudflare`）が返る

### 1-3. リソース余裕

```bash
ssh oracle-strike "free -m && df -h /"
```

- [ ] メモリ使用 < 800MB（swap への突発スパイクを許容）
- [ ] ディスク使用 < 80%（`/var/log/alpha-strike` の event_logger ログがディスク食わないか）

### 1-4. 監視 cron

```bash
ssh oracle-strike "crontab -l | grep -E 'check_memory|run_apt_maintenance'"
```

- [ ] `*/5 * * * * /home/ubuntu/dev/alpha-strike/scripts/check_memory.sh` が登録済み
- [ ] 週次 `run_apt_maintenance.sh` が日曜などに登録済み
- [ ] `~/.ntfy.env` に通知用 `NTFY_TOPIC` が設定済み、テスト通知が届いた

### 1-5. NSG（OCI 側）

- [ ] Ingress ルールが 0 件（Cloudflare Tunnel 完全 egress only）
- [ ] Egress ルールは `0.0.0.0/0` 全許可

---

## 2. アプリケーション層（alpha-strike）

### 2-1. 環境変数

```bash
ssh oracle-strike "sudo cat /etc/alpha-strike/.env | sed 's/=.*/=***/'"
```

- [ ] `WEBHOOK_PASSPHRASE` 設定済み、32+ 文字のランダム文字列
- [ ] `MOOMOO_HOST=127.0.0.1` `MOOMOO_PORT=11111`
- [ ] `MOOMOO_TRADE_PWD_MD5` が設定済み
- [ ] `MOOMOO_TRADE_ENV=SIMULATE`（**LIVE になっていないか必ず確認**）
- [ ] OANDA を使わないなら `OANDA_API_KEY`/`OANDA_ACCOUNT_ID` は空でも可（起動時に WARN が出るのみ、ペーパー運用では問題なし）

### 2-2. /webhook 認証テスト（外部から）

```bash
# Mac から
curl -i -X POST https://strike.alforgelabs.com/webhook \
  -H "Content-Type: application/json" \
  -d '{"passphrase":"WRONG","broker":"moomoo","asset_class":"US","action":"buy","ticker":"US.AAPL","quantity":1,"run_mode":"paper"}'
```

- [ ] レスポンスが **401 Unauthorized**

### 2-3. /webhook 発注テスト（passphrase 正、SIMULATE 1 株）

**推奨**: `scripts/go_live_smoke.sh` で一括実行（health → 401 → 200 → 着弾確認 → 取消 を対話的に進める）

```bash
./scripts/go_live_smoke.sh             # 各段で y/n 確認
./scripts/go_live_smoke.sh --dry-run   # 401 確認まで（実発注なし）
```

- [ ] スクリプトが `✅ go-live スモークテスト完了` で終了
- [ ] レスポンス `status=success`、`broker_order_id` が返った
- [ ] `show_simulate_status` の出力に `recent_orders` の該当発注が出現
- [ ] テスト発注が `cleanup_simulate_orders` で取り消された

### 2-4. event_logger JSONL 出力

```bash
ssh oracle-strike "ls -la /home/ubuntu/dev/alpha-strike/events/*.jsonl 2>/dev/null | tail -3"
```

- [ ] 最新の `events_YYYY-MM-DD.jsonl` に上記スモークテストの `signal_received` / `order_recorded` イベントが追記されている

---

## 3. moomoo OpenD 層

### 3-1. デバイストークン

```bash
ssh oracle-strike "ls -la ~/.com.moomoo.OpenD/F3CNN/ | head"
```

- [ ] デバイストークン（`device.dat` など）が存在し、最終更新が最新の認証日時に一致
- [ ] systemd ユニットの `ExecStart` に `-login_by_remember=1` が含まれる

### 3-2. SIMULATE 口座状態

```bash
ssh oracle-strike "cd ~/dev/alpha-strike && .venv/bin/python scripts/show_simulate_status.py"
```

- [ ] `total_assets` が取得できる（既定 1,000,000 USD 前後）
- [ ] `pending_orders` 件数が想定内（クリーンアップ済みで 0 件 or テスト発注分のみ）
- [ ] `warnings` に `deal_list_query` 失敗のみ（SIMULATE 仕様、無害）

### 3-3. OpenD ログ

```bash
ssh oracle-strike "sudo journalctl -u moomoo-opend -n 50 --no-pager | grep -iE 'err|warn|disconnect' | tail -20"
```

- [ ] 直近で `disconnect` の連発がない（あれば session 切断、再起動を検討）

---

## 4. TradingView 層

### 4-1. アラート設定

- [ ] 対象戦略の Pine スクリプトを TradingView にデプロイ済み（バックテストでパフォーマンスを確認済み）
- [ ] **Notifications** の **Webhook URL** に `https://strike.alforgelabs.com/webhook` を入力
- [ ] **Message** 欄に正しい JSON（または Pine 側 `alert()` で組み立て）
- [ ] アラートの有効期限（**Expiration**）を **Open-ended** に設定
- [ ] **Once Per Bar Close** で発火タイミングを足確定時に統一

### 4-2. Cloudflare WAF Custom Rule

- [ ] [TradingView アラート設定ガイド §2-A](../tradingview.md#2-a-cloudflare-waf-の-custom-rule推奨) の Custom Rule を作成済み
- [ ] TradingView 公式 IP の最新値を確認（[Help Center](https://www.tradingview.com/support/solutions/43000529348-about-webhooks/)）

### 4-3. 疎通テスト（TradingView 経由）

- [ ] 「Alert 編集 → **Test alert**」ボタンを押し、TradingView 側で「Webhook delivered」が出る
- [ ] alpha-strike 側 `journalctl -u alpha-strike` に `Webhook受信` ログが出る
- [ ] 不要なら直後に `cleanup_simulate_orders.py` で取消

---

## 5. 運用ルール

### 5-1. 日次（毎営業日終了時）

- [ ] `scripts/show_simulate_status.py --json --days 1 | jq '.recent_orders'` で当日約定確認
- [ ] event_logger JSONL を `journal/` 側にバックアップ（後日 alpha-forge との pnl 突合に使用）
- [ ] ntfy 通知履歴を確認、CRIT が来ていれば調査

### 5-2. 週次（日曜深夜）

- [ ] `run_apt_maintenance.sh` 結果を ntfy 通知で確認
- [ ] OpenD のメモリリーク兆候を確認（`systemctl status moomoo-opend | grep Memory`）
- [ ] `pending_orders` の長期残存がないか確認

### 5-3. インシデント対応

| 症状 | 一次対応 |
|---|---|
| **異常発注の検知（最緊急）** | **kill switch**: `echo "理由" \| sudo tee /etc/alpha-strike/MAINTENANCE` で即時 503 化（restart 不要、service は活かしたまま）→ `cleanup_simulate_orders.py` で取消 → 原因究明後に `sudo rm /etc/alpha-strike/MAINTENANCE` で復旧 |
| Webhook 502 連発 | `systemctl status alpha-strike moomoo-opend`、必要なら `systemctl restart alpha-strike` |
| OpenD 接続切れ | `systemctl restart moomoo-opend`、デバイストークン期限切れなら Mac で再認証 → rsync |
| 自宅 IP 経由で SSH 不可 | DNS キャッシュ問題（[vm-provisioning.md §11](./vm-provisioning.md)）か Cloudflare Access PIN メール再取得 |
| ディスク逼迫 | `events/*.jsonl` の古い分を `gzip` してアーカイブ、必要に応じて `journald` の保持期間短縮 |

#### Kill switch（受付停止モード）の詳細

異常発注を検知した際の **第一手** として、サービスを止めずに新規発注のみ拒否できる kill switch を使う。Webhook → 503 を返すことで TradingView 側の retry も止まる（持続的な 5xx は自動 disable される）。

| 起動方法 | 切替 | 用途 |
|---|---|---|
| ファイルフラグ `/etc/alpha-strike/MAINTENANCE` | restart 不要・即時 | **緊急時の主要手段**。ファイル内容が 503 detail に含まれるので TradingView 側のエラーログに理由を残せる |
| 環境変数 `MAINTENANCE_MODE=1` | `.env` 編集 + restart | 計画停止時など、systemd 起動時から固定したい場合 |

```bash
# 停止
echo "ticker AAPL runaway: investigating" | sudo tee /etc/alpha-strike/MAINTENANCE
# /webhook が即時 503 を返すようになる
curl -i https://strike.alforgelabs.com/webhook  # → 503

# 解除
sudo rm /etc/alpha-strike/MAINTENANCE
```

> **重要**: maintenance mode 中も `/health` は 200 を返す（外部ヘルスチェック / Cloudflare Tunnel 維持のため）。`passphrase` 検証より **前** に kill switch が判定されるので、maintenance 中の passphrase 試行はログに残らない。

### 5-4. 中止判断

以下のいずれかが発生した場合は TradingView アラートを **全停止** し原因究明：

- [ ] 同一ティッカーの想定外連続発注（5 回 / 5 分以上）
- [ ] `pending_orders` が 24 時間以上滞留
- [ ] `total_assets` が想定外の急減（戦略の最大ドローダウンを超える）
- [ ] Cloudflare WAF / Access から原因不明の `403` が連発

---

## 6. LIVE 移行への前提（参考、本ドキュメントの範囲外）

ペーパートレードで **3 ヶ月以上の安定稼働 + alpha-forge バックテストとの pnl 乖離が 5% 以内** を確認した後に LIVE 移行を検討。LIVE では以下が追加で必要：

- moomoo 本番口座での `MOOMOO_TRADE_ENV=REAL` 切替
- 二段階認証の手順整備
- 想定外発注の即時遮断機構（`/etc/alpha-strike/.env` に kill switch を追加するなど）
- 月次の損益レポート自動化（alpha-forge journal 側）

---

## 関連ドキュメント

- [TradingView アラート設定ガイド](../tradingview.md)
- [VM プロビジョニング手順書](./vm-provisioning.md)
- [moomoo OpenD セットアップ](../moomoo_futud.md)
