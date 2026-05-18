# alpha-strike

[![CI](https://github.com/alforge-labs/alpha-strike/actions/workflows/ci.yml/badge.svg)](https://github.com/alforge-labs/alpha-strike/actions/workflows/ci.yml)
[![CodeQL](https://github.com/alforge-labs/alpha-strike/actions/workflows/codeql.yml/badge.svg)](https://github.com/alforge-labs/alpha-strike/actions/workflows/codeql.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

**English** | [日本語](README.md)

> **Self-hosted webhook bridge from TradingView alerts to moomoo / OANDA brokers**

`alpha-strike` is a FastAPI-based webhook server that receives signals from TradingView Premium / Essential alerts and routes orders to **moomoo securities (US / HK stocks, crypto)** or **OANDA securities (FX / CFD)** based on the JSON payload.

This repo also ships a **production-ready reference architecture using Oracle Cloud Always Free + Cloudflare Tunnel + Cloudflare WAF Custom Rule** that runs at zero monthly cost and is immune to home IP changes (DHCP).

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
       ├──► moomoo OpenD ─► moomoo SIMULATE / REAL (US / HK stocks, crypto)
       └──► OANDA REST v20 ─► OANDA PRACTICE / LIVE (FX / CFD)
```

## Features

- **TradingView webhook receiver** — accepts JSON payloads at `/webhook` with strict Pydantic validation
- **Passphrase authentication** — HMAC-compares the request body `passphrase` against `WEBHOOK_PASSPHRASE`
- **Multi-broker routing** — `broker: "moomoo" | "oanda"` selects the target. `asset_class` covers `US` / `HK` / `CRYPTO` / `FX` / `COMMODITY` / `INDEX`
- **Rate limiting** — `slowapi` enforces `10 req/min/IP`
- **Retry + timeout** — `tenacity` handles transient broker API failures (OANDA: exponential backoff ×3, moomoo: fixed 2s ×3)
- **JSONL event log** — appends `SignalEvent` / `OrderEvent` / `FillEvent` / `TradeClosedEvent` for downstream pnl reconciliation
- **Memory / service monitoring** — `scripts/check_memory.sh` (cron 5 min) ntfy-pushes memory, swap, service status, and OOM history
- **Production deployment guide** — `docs/ops/vm-provisioning.md` documents the full Oracle Cloud E2.1.Micro + Cloudflare Tunnel + systemd procedure

## Quick Start

### Local (fastest)

```bash
git clone https://github.com/alforge-labs/alpha-strike.git
cd alpha-strike
uv sync

# Required env
echo 'WEBHOOK_PASSPHRASE=your-secret-passphrase' > .env
echo 'MOOMOO_TRD_ENV=SIMULATE' >> .env  # if using moomoo
echo 'OANDA_ENV=PRACTICE'     >> .env   # if using OANDA

# Start
uv run uvicorn webhook_server:app --host 0.0.0.0 --port 8080

# Verify
curl http://localhost:8080/health
# → {"status":"ok"}
```

### Binary distribution (PyInstaller)

Download a single-file binary for your OS (~52 MB) from [GitHub Releases](https://github.com/alforge-labs/alpha-strike/releases).

```bash
# macOS / Linux
chmod +x alpha-strike-macos-arm64
WEBHOOK_PASSPHRASE=your-secret ./alpha-strike-macos-arm64

# Windows (PowerShell)
$env:WEBHOOK_PASSPHRASE = "your-secret"
.\alpha-strike-windows-x86_64.exe
```

### Production: Oracle Cloud + Cloudflare Tunnel

Full operational procedure for paper-trading production:

- 📖 [alpha-strike Setup Guide](https://alforgelabs.com/en/docs/guides/alpha-strike-setup/) — VM provisioning, Cloudflare Tunnel, WAF, OpenD, systemd
- 📖 [TradingView × alpha-strike Integration](https://alforgelabs.com/en/docs/guides/tradingview-alpha-strike/) — payload spec and Pine v6 template

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `WEBHOOK_PASSPHRASE` | ✅ | Authentication passphrase (32+ char random string recommended) |
| `ALPHA_STRIKE_HOST` | — | Bind host (default `0.0.0.0`) |
| `ALPHA_STRIKE_PORT` | — | Bind port (default `8080`) |
| `MOOMOO_HOST` | moomoo | OpenD host (default `127.0.0.1`) |
| `MOOMOO_PORT` | moomoo | OpenD port (default `11111`) |
| `MOOMOO_TRD_ENV` | moomoo | `SIMULATE` (paper) or `REAL` (live) |
| `MOOMOO_TRADE_PWD_MD5` | moomoo | MD5 of moomoo trading password |
| `OANDA_API_KEY` | OANDA | Personal Access Token |
| `OANDA_ACCOUNT_ID` | OANDA | Account ID |
| `OANDA_ENV` | OANDA | `PRACTICE` (demo) or `LIVE` (production) |

> **Important**: Always use `MOOMOO_TRD_ENV=SIMULATE` / `OANDA_ENV=PRACTICE` for testing. The maintainers are not liable for accidental orders on live accounts.

## Webhook Payload

See [docs/tradingview.md](docs/tradingview.md) and [docs/webhook-payload-v2.md](docs/webhook-payload-v2.md) for the full spec. Minimal example:

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

| `asset_class` | broker | ticker example |
|---|---|---|
| `US` | moomoo / oanda | `US.AAPL` / `AAPL` |
| `HK` | moomoo | `HK.00700` |
| `CRYPTO` | moomoo | `CC.BTC` / `CC.ETH` / `CC.XRP` |
| `FX` | oanda | `USDJPY` |
| `COMMODITY` | oanda | `XAUUSD` |
| `INDEX` | oanda | `NAS100` |

## Documentation

- 📖 [Official Documentation](https://alforgelabs.com/en/docs/) — full doc index
- 📖 [alpha-strike Setup Guide](https://alforgelabs.com/en/docs/guides/alpha-strike-setup/) — production go-live procedure
- 📖 [Webhook payload spec](docs/tradingview.md)
- 📖 [VM provisioning playbook](docs/ops/vm-provisioning.md) — Oracle Cloud E2.1.Micro + Cloudflare Tunnel
- 📖 [Paper-trading go-live checklist](docs/ops/paper-trading-go-live.md)
- 📖 [moomoo OpenD setup](docs/moomoo_futud.md)

## Contributing

- **Contribution guide**: [CONTRIBUTING.en.md](CONTRIBUTING.en.md)
- **Security reporting**: [SECURITY.en.md](SECURITY.en.md)
- **Code of conduct**: [CODE_OF_CONDUCT.en.md](CODE_OF_CONDUCT.en.md) (Contributor Covenant v2.1)
- **Changelog**: [CHANGELOG.md](CHANGELOG.md)

## Development

```bash
# Install deps
uv sync

# Test + lint
uv run pytest tests/ -q
uv run ruff check .

# Local build verification (PyInstaller + /health smoke)
bash verify-build.sh
```

## Related Projects

- 🌐 [alforgelabs.com](https://alforgelabs.com/) — Alforge Labs official site
- 📊 [alpha-visualizer](https://github.com/alforge-labs/alpha-visualizer) — Web visualization for AlphaForge backtest results (Apache-2.0)
- 🧪 [AlphaForge](https://alforgelabs.com/en/docs/) — Backtesting and optimization engine (commercial license)

## Disclaimer

This software is provided **AS IS** without any warranty. **Automated trading can result in losses exceeding your principal.** The copyright holders and contributors disclaim all liability for any direct or indirect damages (including financial losses) arising from use of this software. Use entirely at your own risk.

You are responsible for complying with each broker's terms of service, trading hours, and regulatory requirements. For example, moomoo crypto for US residents is subject to FinCEN MSB regulations; verify and comply yourself.

## License

[Apache License 2.0](LICENSE) © [alforge-labs](https://github.com/alforge-labs)
