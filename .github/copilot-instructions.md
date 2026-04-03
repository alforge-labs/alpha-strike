# Alpha-Strike — Copilot Instructions

FastAPI webhook server that receives TradingView alerts and routes orders to OANDA or moomoo brokers.

## Commands

```bash
uv sync                                              # install / update dependencies
uv run uvicorn webhook_server:app --host 0.0.0.0 --port 8080 --reload  # dev server
uv run pytest                                        # full test suite
uv run pytest tests/test_oanda_handler.py -v        # single test file
uv run pytest tests/test_webhook_server.py::test_name -v  # single test
uv run ruff check .                                  # lint
```

Requires Python 3.14. Add packages with `uv add <package>`, not `pip install`.

## Architecture

```
POST /webhook  →  webhook_server.py  →  handlers/oanda_handler.py   →  OANDA REST API v20
                                     →  handlers/moomoo_handler.py  →  moomoo OpenD (local)
POST /events/trade-closed  →  event_logger.py  →  JSONL files
```

**Entry point**: `webhook_server.py` (FastAPI + slowapi rate limiting)

**Handlers** (`handlers/`):
- `oanda_handler.py` — OANDA REST API v20 (`api-fxpractice.oanda.com` / `api-fxtrade.oanda.com`)
- `moomoo_handler.py` — moomoo/Futu OpenAPI via OpenD local gateway

**Event logger**: `event_logger.py` — writes `SignalEvent`, `OrderEvent`, `FillEvent`, `TradeClosedEvent` as JSONL to `$LIVE_EVENTS_PATH` (format: `YYYY-MM-DD.<broker>.jsonl`)

**Models**: `models.py` — all Pydantic models (`WebhookPayload`, `OrderResult`, event types)

## Key Implementation Details

### OANDA instrument conversion (`oanda_handler.py`)
- `FX` / `COMMODITY`: `USDJPY` → `USD_JPY` (split 6-char ticker at position 3)
- `US` / `INDEX`: `AAPL` → `AAPL_USD` (append `_USD`)
- Other: pass through as-is

### OANDA SELL orders
`units` must be negative for SELL (e.g., `-1000`).

### moomoo asset classes
- `HK` → `OpenHKTradeContext`; all others → `OpenUSTradeContext`
- Ticker format: `US.AAPL`, `HK.00700`

### OpenD pre-flight check
`moomoo_handler.py` does a TCP connection check to `MOOMOO_HOST:MOOMOO_PORT` (3s timeout) before placing any order. Raises `RuntimeError` if unreachable. Start OpenD before the server.

### Opposite-fill detection
Both handlers detect when an incoming fill closes an existing open position (opposite-side fill for same strategy/ticker). Generates `TradeClosedEvent` automatically. For reversal fills (quantity exceeds open position), the excess becomes a new `FillEvent` with a fresh `trade_id`.

### Passphrase authentication
All endpoints (`/webhook`, `/events/trade-closed`) validate `passphrase` against `WEBHOOK_PASSPHRASE` env var. Use `hmac.compare_digest` to prevent timing attacks.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `WEBHOOK_PASSPHRASE` | Yes | — | TradingView auth passphrase |
| `LIVE_EVENTS_PATH` | No | — | JSONL event output directory |
| `OANDA_API_KEY` | OANDA | — | Personal Access Token |
| `OANDA_ACCOUNT_ID` | OANDA | — | Account ID |
| `OANDA_ENV` | OANDA | `PRACTICE` | `PRACTICE` or `LIVE` |
| `MOOMOO_HOST` | moomoo | `127.0.0.1` | OpenD host |
| `MOOMOO_PORT` | moomoo | `11111` | OpenD port |
| `MOOMOO_TRD_ENV` | moomoo | `SIMULATE` | `SIMULATE` or `REAL` |

## Webhook Payload

```json
{
  "passphrase": "secret",
  "broker": "oanda",
  "asset_class": "FX",
  "action": "buy",
  "ticker": "USDJPY",
  "quantity": 1000
}
```

`broker` is `"oanda"` or `"moomoo"`. `action` is `"buy"` or `"sell"`.

## Docker

```bash
docker compose up -d   # production (port 8080, nginx reverse proxy)
```

`ecosystem.config.js` is a PM2 config for non-Docker deployments.
