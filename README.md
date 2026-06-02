# alpha-strike

[![PyPI version](https://img.shields.io/pypi/v/alpha-strike.svg)](https://pypi.org/project/alpha-strike/)
[![CI](https://github.com/alforge-labs/alpha-strike/actions/workflows/ci.yml/badge.svg)](https://github.com/alforge-labs/alpha-strike/actions/workflows/ci.yml)
[![CodeQL](https://github.com/alforge-labs/alpha-strike/actions/workflows/codeql.yml/badge.svg)](https://github.com/alforge-labs/alpha-strike/actions/workflows/codeql.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/alpha-strike.svg)](https://pypi.org/project/alpha-strike/)

[English](README.en.md) | **日本語**

> **TradingView のアラートを Webhook で受け取り、moomoo / OANDA に自動発注するセルフホスト型ブリッジ**

`alpha-strike` は、TradingView Premium / Essential 以上のアラート機能から Webhook 経由でシグナルを受け取り、リクエストボディに基づいて **moomoo 証券（米国株・香港株・暗号資産）** または **OANDA 証券（FX・CFD）** へ自動発注する FastAPI ベースの Webhook サーバーです。

**Oracle Cloud Always Free + Cloudflare Tunnel + Cloudflare WAF Custom Rule** の構成で、自宅 IP の DHCP 変動に依存せず、月額 0 円でセルフホストできるリファレンス実装も提供しています。

```
TradingView Premium / Essential
       │  HTTPS Webhook
       ▼
Cloudflare WAF Custom Rule (TradingView IP allowlist)
       │
Cloudflare Tunnel (cloudflared)
       │  outbound only (NSG Ingress = 0)
       ▼
alpha-strike (FastAPI, this repo)
       │
       ├──► moomoo OpenD ─► moomoo SIMULATE / REAL (米国株 / 香港株 / 暗号資産)
       └──► OANDA REST v20 ─► OANDA PRACTICE / LIVE (FX / CFD)
```

## 主な機能

- **TradingView Webhook 受信** — `/webhook` エンドポイントで JSON ペイロードを受理、Pydantic で厳格バリデーション
- **passphrase 認証** — リクエストボディの `passphrase` を `WEBHOOK_PASSPHRASE` と HMAC 比較
- **multi-broker ルーティング** — `broker: "moomoo" | "oanda"` で発注先を選択。`asset_class` で `US` / `HK` / `CRYPTO` / `FX` / `COMMODITY` / `INDEX` を切替
- **Rate limiting** — `slowapi` で `10 req/min/IP` の上限を強制
- **リトライ + タイムアウト** — `tenacity` で broker API 一時障害に対する自動リトライ（OANDA: 指数バックオフ ×3、moomoo: 固定 2 秒 ×3）
- **JSONL イベントログ** — `SignalEvent` / `OrderEvent` / `FillEvent` / `TradeClosedEvent` を逐次追記、journal との pnl 突合に利用可
- **メモリ・サービス監視** — `scripts/check_memory.sh` を cron 登録すれば 5 分毎にメモリ・swap・サービス・OOM を ntfy 通知
- **本格運用向けデプロイ手順** — 初回構築は `docs/ops/vm-provisioning.md`（Oracle Cloud E2.1.Micro + Cloudflare Tunnel + systemd）、**新バージョンの反映（PyPI → VM 更新 + 再起動）は `docs/ops/deployment.md`** を参照

## クイックスタート

### ローカルで試す（最短）

### PyPI から（推奨）

```bash
# uv（推奨）
uv add alpha-strike

# または pipx でグローバル CLI として
pipx install alpha-strike

# または venv に直接
pip install alpha-strike
```

起動:

```bash
WEBHOOK_PASSPHRASE=your-secret-passphrase alpha-strike

# ホスト / ポート指定
WEBHOOK_PASSPHRASE=your-secret-passphrase alpha-strike --host 127.0.0.1 --port 9000

# 疎通確認
curl http://localhost:8080/health
# → {"status":"ok"}
```

### ソースから（開発時）

```bash
git clone https://github.com/alforge-labs/alpha-strike.git
cd alpha-strike
uv sync

# .env を設定
echo 'WEBHOOK_PASSPHRASE=your-secret-passphrase' > .env
echo 'MOOMOO_TRD_ENV=SIMULATE' >> .env  # moomoo を使う場合
echo 'OANDA_ENV=PRACTICE'     >> .env   # OANDA を使う場合

# CLI から起動
uv run alpha-strike

# ホットリロード（開発時）
uv run alpha-strike --reload
```

### Oracle Cloud + Cloudflare Tunnel で本格運用

ペーパートレード本格運用までの完全手順は公式ドキュメントを参照：

- 📖 [alpha-strike セットアップガイド](https://alforgelabs.com/ja/docs/guides/alpha-strike-setup/) — VM プロビジョニング・Cloudflare Tunnel・WAF・OpenD・systemd の全手順
- 📖 [TradingView × alpha-strike Integration](https://alforgelabs.com/ja/docs/guides/tradingview-alpha-strike/) — Webhook ペイロード仕様と Pine v6 テンプレート

## 環境変数

| 変数 | 必須 | 説明 |
|---|---|---|
| `WEBHOOK_PASSPHRASE` | ✅ | TradingView アラートの認証用パスフレーズ（32 文字以上のランダム文字列推奨） |
| `ALPHA_STRIKE_HOST` | — | バインドホスト（既定 `0.0.0.0`） |
| `ALPHA_STRIKE_PORT` | — | バインドポート（既定 `8080`） |
| `MOOMOO_HOST` | moomoo 使用時 | OpenD のホスト（既定 `127.0.0.1`） |
| `MOOMOO_PORT` | moomoo 使用時 | OpenD のポート（既定 `11111`） |
| `MOOMOO_TRD_ENV` | moomoo 使用時 | `SIMULATE`（デモ）または `REAL`（本番） |
| `OANDA_API_KEY` | OANDA 使用時 | Personal Access Token |
| `OANDA_ACCOUNT_ID` | OANDA 使用時 | 口座 ID |
| `OANDA_ENV` | OANDA 使用時 | `PRACTICE`（デモ）または `LIVE`（本番） |
| `LIVE_EVENTS_PATH` | — | JSONL イベントログ保存先（既定 `/app/data/events`。docker-compose の volumes と一致させる） |
| `STATUS_API_TOKEN` | — | `/status`・`/status/events` の Bearer トークン（#57）。未設定時はこれらのエンドポイントを 503 で無効化（fail-safe・デフォルト非公開） |
| `NTFY_TOPIC` | — | 約定プッシュ通知の ntfy トピック（#57 Phase 2）。未設定なら通知は no-op |
| `NTFY_SERVER` | — | ntfy サーバー（既定 `https://ntfy.sh`） |
| `ORDER_RECONCILE_DELAY_SECONDS` | — | 発注後に order status を照合するまでの待機秒数（既定 `5`） |

> **重要**: 検証時は必ず `MOOMOO_TRD_ENV=SIMULATE` / `OANDA_ENV=PRACTICE` を使用してください。本番口座での誤発注は本ソフトウェアの責任範囲外です。

## Webhook ペイロード仕様

詳細は [docs/tradingview.md](docs/tradingview.md) と [docs/webhook-payload-v2.md](docs/webhook-payload-v2.md) を参照。最小例：

```json
{
  "passphrase": "your-secret-passphrase",
  "broker": "moomoo",
  "asset_class": "US",
  "action": "buy",
  "ticker": "US.AAPL",
  "quantity": 10,
  "run_mode": "paper",
  "strategy_id": "demo_buy_v1"
}
```

| `asset_class` | broker | ticker 例 |
|---|---|---|
| `US` | moomoo / oanda | `US.AAPL` / `AAPL` |
| `HK` | moomoo | `HK.00700` |
| `CRYPTO` | moomoo | `CC.BTC` / `CC.ETH` / `CC.XRP` |
| `FX` | oanda | `USDJPY` |
| `COMMODITY` | oanda | `XAUUSD` |
| `INDEX` | oanda | `NAS100` |

## ドキュメント

- 📖 [公式ドキュメント](https://alforgelabs.com/ja/docs/) — Alforge Labs ドキュメント集約
- 📖 [alpha-strike セットアップガイド](https://alforgelabs.com/ja/docs/guides/alpha-strike-setup/) — 本格運用までの完全手順
- 📖 [Webhook ペイロード仕様](docs/tradingview.md) — JSON フィールドの詳細
- 📖 [VM プロビジョニング手順書](docs/ops/vm-provisioning.md) — Oracle Cloud E2.1.Micro + Cloudflare Tunnel
- 📖 [ペーパートレード Go-Live チェックリスト](docs/ops/paper-trading-go-live.md) — 本番運用前の確認項目
- 📖 [moomoo OpenD セットアップ](docs/moomoo_futud.md) — OpenD CLI インストールとデバイストークン認証

## 開発に参加する

- **コントリビューションガイド**: [CONTRIBUTING.md](CONTRIBUTING.md)
- **セキュリティ報告**: [SECURITY.md](SECURITY.md)
- **行動規範**: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)（Contributor Covenant v2.1）
- **変更履歴**: [CHANGELOG.md](CHANGELOG.md)

## 開発環境

```bash
# 依存関係インストール
uv sync

# テスト + Lint
uv run pytest tests/ -q
uv run ruff check .

# PyPI 配布用 wheel / sdist をローカル生成
uv build
# → dist/alpha_strike-X.Y.Z-py3-none-any.whl + .tar.gz
```

## 関連プロジェクト

- 🌐 [alforgelabs.com](https://alforgelabs.com/) — Alforge Labs 公式サイト
- 📊 [alpha-visualizer](https://github.com/alforge-labs/alpha-visualizer) — AlphaForge バックテスト結果の Web 可視化ツール（Apache-2.0）
- 🧪 [AlphaForge](https://alforgelabs.com/ja/docs/) — バックテスト・最適化エンジン（商用ライセンス）

## 免責事項

本ソフトウェアは現状のまま（AS IS）提供され、いかなる種類の保証もありません。**自動売買は元本を超える損失を生じる可能性があります**。本ソフトウェアを利用して発生した直接・間接の損害（金銭的損失を含む）について、著作権者およびコントリビューターは一切の責任を負いません。本ソフトウェアの利用は完全に自己責任で行ってください。

各 broker の利用規約・取引時間・規制を必ず遵守してください。米国居住者の moomoo crypto 利用には FinCEN MSB 規制が適用される等、規制要件は利用者自身で確認・遵守する責任があります。

## ライセンス

[Apache License 2.0](LICENSE) © [alforge-labs](https://github.com/alforge-labs)
