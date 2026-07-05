# alpha-strike

[![PyPI version](https://img.shields.io/pypi/v/alpha-strike.svg)](https://pypi.org/project/alpha-strike/)
[![CI](https://github.com/alforge-labs/alpha-strike/actions/workflows/ci.yml/badge.svg)](https://github.com/alforge-labs/alpha-strike/actions/workflows/ci.yml)
[![CodeQL](https://github.com/alforge-labs/alpha-strike/actions/workflows/codeql.yml/badge.svg)](https://github.com/alforge-labs/alpha-strike/actions/workflows/codeql.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/alpha-strike.svg)](https://pypi.org/project/alpha-strike/)
[![Follow @Alforge_bot](https://img.shields.io/badge/Follow-%40Alforge__bot-000?logo=x)](https://x.com/Alforge_bot)

**English** | [日本語](README.md)

> **Pairs with [AlphaForge](https://alforgelabs.com) strategies (Pine v6 → webhook execution)** — the agent-native quant CLI for strategy authoring, Optuna TPE optimization, walk-forward validation, and TradingView Pine v6 export. alpha-strike handles the execution half. → **[Try AlphaForge free](https://alforgelabs.com)**

---

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

### From PyPI (recommended)

```bash
# uv (recommended)
uv add alpha-strike

# Or pipx for a global CLI
pipx install alpha-strike

# Or plain pip into a venv
pip install alpha-strike
```

Run:

```bash
WEBHOOK_PASSPHRASE=your-secret-passphrase alpha-strike

# Custom host / port
WEBHOOK_PASSPHRASE=your-secret-passphrase alpha-strike --host 127.0.0.1 --port 9000

# Verify
curl http://localhost:8080/health
# → {"status":"ok"}
```

### From source (for development)

```bash
git clone https://github.com/alforge-labs/alpha-strike.git
cd alpha-strike
uv sync

# Configure .env
echo 'WEBHOOK_PASSPHRASE=your-secret-passphrase' > .env
echo 'MOOMOO_TRD_ENV=SIMULATE' >> .env  # if using moomoo
echo 'OANDA_ENV=PRACTICE'     >> .env   # if using OANDA

# Run via the CLI
uv run alpha-strike

# Hot-reload (dev)
uv run alpha-strike --reload
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
| `MOOMOO_TIME_IN_FORCE` | — | Time-in-force for **REAL** US-market MARKET orders (#76). `GTC` (default) carries orders received after the market close over to the next session's open / `DAY` restores the legacy same-day-only behavior (after-close orders expire unfilled). HK / CRYPTO always use `DAY` per moomoo spec and trading-hour characteristics. **SIMULATE (paper) always uses `DAY` regardless of this setting because moomoo 10.7 rejects GTC for paper trading**. SIMULATE after-close signals would expire as DAY, so `CARRYOVER_*` (#89) re-submits them at the next market open to fill them |
| `MOOMOO_SELL_POSITION_GUARD` | — | Over-sell guard that clamps a moomoo SELL to the broker's actual `can_sell_qty` (trims the excess) and skips it when no position is held (default `1`=on; set `0`/`false` to disable). Prevents `Not enough positions` caused by the open-loop drift in the Pine→webhook→broker pipeline |
| `MOOMOO_TARGET_QTY_RECONCILE` | — | When the payload carries `target_qty` (absolute target holding), re-resolves the order side/quantity against the broker's actual position (closed-loop, #80; default `1`=on). Set `0`/`false` to fall back to the legacy delta interpretation |
| `PENDING_RECONCILE_ENABLED` | — | Periodically re-reconciles non-terminal orders (e.g. GTC orders that fill on the next trading day) and appends the confirmed fill as an `order_reconciled` event (#79; default `1`=on). Set `0`/`false` to disable |
| `PENDING_RECONCILE_INTERVAL_SECONDS` | — | Sweep interval in seconds (default `600`). Also runs once immediately at startup |
| `CARRYOVER_ENABLED` | — | **SIMULATE only**: carry-over emulation that re-submits US-market after-close signals at the next market open (#89; default `1`=on). Set `0`/`false` to disable. Disabled internally for REAL (which has GTC carry-over) |
| `CARRYOVER_RESUBMIT_INTERVAL_SECONDS` | — | Carry-over re-submit sweep interval in seconds (default `300`). Also runs once immediately at startup |
| `CARRYOVER_LOOKBACK_HOURS` | — | Expiry of a queued intent in hours (default `48`). Older intents are abandoned as stale rather than re-submitted (covers weekend gaps) |
| `CARRYOVER_MAX_RESUBMITS` | — | Max re-submit attempts per intent (default `3`). Intents exceeding this are abandoned (prevents repeated misfires) |
| `OANDA_API_KEY` | OANDA | Personal Access Token |
| `OANDA_ACCOUNT_ID` | OANDA | Account ID |
| `OANDA_ENV` | OANDA | `PRACTICE` (demo) or `LIVE` (production) |
| `LIVE_EVENTS_PATH` | — | JSONL event-log directory (default `/app/data/events`; match the docker-compose volume mount) |
| `STATUS_API_TOKEN` | — | Bearer token for `/status` and `/status/events` (#57). When unset, those endpoints are disabled with 503 (fail-safe, private by default) |
| `NTFY_TOPIC` | — | ntfy topic for fill push notifications (#57 Phase 2). No-op when unset |
| `NTFY_SERVER` | — | ntfy server (default `https://ntfy.sh`) |
| `ORDER_RECONCILE_DELAY_SECONDS` | — | Seconds to wait before reconciling order status after submission (default `5`) |

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

With the optional `target_qty` field (absolute target holding, `>= 0`), moomoo orders are re-resolved against the broker's actual position (closed-loop, #80). `quantity` is still sent as the delta fallback for versions without `target_qty` support.

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

# Build PyPI artifacts locally
uv build
# → dist/alpha_strike-X.Y.Z-py3-none-any.whl + .tar.gz
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
