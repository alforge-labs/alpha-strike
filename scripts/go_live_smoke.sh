#!/usr/bin/env bash
# go_live_smoke.sh — ペーパートレード本番運用前の E2E スモークテスト
#
# 流れ:
#   1. /health          200          (Tunnel + alpha-strike 起動確認)
#   2. /webhook (wrong) 401          (passphrase 検証パスの確認)
#   3. /webhook (real)  200          (実 SIMULATE 発注)
#   4. show_simulate_status (VM)     (発注の着弾確認)
#   5. cleanup_simulate_orders (VM)  (テスト発注の取消)
#
# 利用前提:
#   - Mac から実行する
#   - 1Password CLI (op) が認証済み
#   - vault "AlphaTrade" の item "alpha-strike" に WEBHOOK_PASSPHRASE が登録済み
#   - ssh oracle-strike が疎通する
#
# Usage:
#   ./scripts/go_live_smoke.sh                 # 対話的に各段で確認しながら進む
#   ./scripts/go_live_smoke.sh --yes           # 全段を確認なしで連続実行
#   ./scripts/go_live_smoke.sh --dry-run       # 実発注はしない（401 確認まで）

set -euo pipefail

WEBHOOK_URL="${WEBHOOK_URL:-https://strike.alforgelabs.com/webhook}"
HEALTH_URL="${HEALTH_URL:-https://strike.alforgelabs.com/health}"
TICKER="${TICKER:-US.AAPL}"
QTY="${QTY:-1}"
REMOTE_HOST="${REMOTE_HOST:-oracle-strike}"
# VM は pip インストールのみで git チェックアウトが無いため、ローカルの
# ヘルパースクリプトを VM の venv python へ stdin パイプして実行する。
REMOTE_PYTHON="${REMOTE_PYTHON:-/opt/alpha-strike/.venv/bin/python}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AUTO_YES=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y)   AUTO_YES=1 ;;
    --dry-run)  DRY_RUN=1 ;;
    -h|--help)
      sed -n '1,30p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

confirm() {
  local msg="$1"
  if [[ $AUTO_YES -eq 1 ]]; then return 0; fi
  read -r -p "$msg [y/N]: " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

step() {
  printf '\n==== %s ====\n' "$1"
}

# --- 1. /health ---
step "1/5  /health 疎通確認"
status=$(curl -sS -o /tmp/strike_health.body -w '%{http_code}' "$HEALTH_URL") || true
echo "HTTP $status  body: $(cat /tmp/strike_health.body)"
[[ "$status" == "200" ]] || { echo "FAIL: /health が 200 を返さない"; exit 1; }

# --- 2. /webhook (wrong passphrase) ---
step "2/5  /webhook 認証失敗 (401) 確認"
status=$(curl -sS -o /tmp/strike_401.body -w '%{http_code}' \
  -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d '{"passphrase":"DELIBERATELY_WRONG","broker":"moomoo","asset_class":"US","action":"buy","ticker":"US.AAPL","quantity":1,"run_mode":"paper"}') || true
echo "HTTP $status  body: $(cat /tmp/strike_401.body)"
[[ "$status" == "401" ]] || { echo "FAIL: 401 を期待したが $status"; exit 1; }

if [[ $DRY_RUN -eq 1 ]]; then
  echo
  echo "--dry-run 指定のためここで終了。401 まで OK。"
  exit 0
fi

# --- 3. /webhook (real passphrase → SIMULATE 発注) ---
step "3/5  /webhook 実 passphrase で SIMULATE 発注"
echo "発注内容: broker=moomoo asset_class=US action=buy ticker=$TICKER qty=$QTY run_mode=paper"
confirm "本当に SIMULATE 発注を出してよいですか?" || { echo "abort"; exit 0; }

PASSPHRASE=$(op item get "alpha-strike" --vault AlphaTrade --fields WEBHOOK_PASSPHRASE --reveal)
[[ -n "$PASSPHRASE" ]] || { echo "FAIL: 1Password から passphrase を取得できなかった"; exit 1; }

BODY=$(cat <<JSON
{"passphrase":"$PASSPHRASE","broker":"moomoo","asset_class":"US","action":"buy","ticker":"$TICKER","quantity":$QTY,"run_mode":"paper","strategy_id":"go_live_smoke","alert_name":"go_live_smoke.sh"}
JSON
)

status=$(curl -sS -o /tmp/strike_200.body -w '%{http_code}' \
  -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d "$BODY") || true
echo "HTTP $status  body: $(cat /tmp/strike_200.body | head -c 400)..."
[[ "$status" == "200" ]] || { echo "FAIL: 200 を期待したが $status"; exit 1; }

ORDER_ID=$(python3 -c "import json,sys; print(json.load(open('/tmp/strike_200.body')).get('broker_order_id',''))" 2>/dev/null || true)
echo "broker_order_id: ${ORDER_ID:-<取得失敗>}"

# --- 4. show_simulate_status (VM) ---
step "4/5  show_simulate_status で着弾確認"
ssh "$REMOTE_HOST" "$REMOTE_PYTHON - --days 1" < "$SCRIPT_DIR/show_simulate_status.py" || {
  echo "WARN: show_simulate_status の実行に失敗。手動で ssh して確認してください。"
}

# --- 5. cleanup_simulate_orders (VM) ---
step "5/5  cleanup_simulate_orders でテスト発注を取消"
confirm "テスト発注を取り消しますか?" || { echo "skip cleanup"; exit 0; }
ssh "$REMOTE_HOST" "$REMOTE_PYTHON -" < "$SCRIPT_DIR/cleanup_simulate_orders.py" || {
  echo "WARN: cleanup_simulate_orders の実行に失敗。手動で ssh して確認してください。"
}

echo
echo "==== ✅ go-live スモークテスト完了 ===="
echo "次のステップ:"
echo "  - Cloudflare WAF Custom Rule を設定 (docs/tradingview.md §2-A)"
echo "  - TradingView Premium で対象戦略のアラートを作成 (docs/tradingview.md §3-§5)"
echo "  - docs/ops/paper-trading-go-live.md §1-§4 を上から潰す"
