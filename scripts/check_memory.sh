#!/usr/bin/env bash
# check_memory.sh
#
# alpha-strike VM (oracle-strike, E2.1.Micro 1GB RAM) のメモリ・swap・主要
# サービス状態・OOM 発生を 5 分ごとに監視し、閾値超または異常時に ntfy で通知する。
#
# VM cron entry 例:
#   */5 * * * * /home/ubuntu/dev/alpha-strike/scripts/check_memory.sh
#
# 前提:
# - ubuntu ユーザーが NOPASSWD sudo を持つこと (Ubuntu Cloud Image の既定)
# - ~/.ntfy.env に NTFY_TOPIC=<topic> が定義されていること (mode 600 推奨)
# - sudo journalctl が読めること（kernel ログ含む OOM 検知のため）

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

LOG_DIR="$HOME/var/log/alphastrike"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/memory-monitor.log"
STATE_FILE="$LOG_DIR/memory-monitor.state"

# === 閾値設定 ===
MEM_WARN_PCT=85
MEM_CRIT_PCT=95
SWAP_WARN_MB=1000
SWAP_CRIT_MB=3000
NOTIFY_COOLDOWN_SEC=1800   # 同 severity 内で 30 分間隔
OOM_LOOKBACK="10 min ago"

# 監視対象サービス
SERVICES=(moomoo-opend alpha-strike cloudflared)

# NTFY_TOPIC を ~/.ntfy.env から取り込む (cron 環境では env 継承されない)
NTFY_ENV_FILE="$HOME/.ntfy.env"
if [[ -f "$NTFY_ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  . "$NTFY_ENV_FILE"
fi

# === ntfy 通知ヘルパー ===
notify_ntfy() {
  local status="$1" title="$2" body="$3"
  if [[ -z "${NTFY_TOPIC:-}" ]]; then
    echo "[$(date '+%H:%M:%S')] [WARN] NTFY_TOPIC 未設定、通知をスキップ" >> "$LOG_FILE"
    return 0
  fi
  local tags priority
  case "$status" in
    success)  tags="rocket,white_check_mark"; priority="default" ;;
    warning)  tags="warning,server"; priority="default" ;;
    *)        tags="rotating_light,server"; priority="high" ;;
  esac
  if curl -fsS --max-time 10 \
        -H "Title: $title" \
        -H "Tags: $tags" \
        -H "Priority: $priority" \
        -d "$body" \
        "https://ntfy.sh/$NTFY_TOPIC" > /dev/null 2>&1; then
    echo "[$(date '+%H:%M:%S')] notify($status) sent: $title" >> "$LOG_FILE"
  else
    echo "[$(date '+%H:%M:%S')] [WARN] notify failed (curl exit=$?)" >> "$LOG_FILE"
  fi
}

# === クールダウン判定 ===
# 同じキーで NOTIFY_COOLDOWN_SEC 以内に通知済みなら 1 (= skip) を返す
should_notify() {
  local key="$1"
  local now
  now=$(date +%s)
  if [[ -f "$STATE_FILE" ]]; then
    local last_ts
    last_ts=$(grep "^${key}=" "$STATE_FILE" 2>/dev/null | cut -d= -f2 || true)
    if [[ -n "${last_ts:-}" && $((now - last_ts)) -lt $NOTIFY_COOLDOWN_SEC ]]; then
      return 1
    fi
  fi
  # 通知時刻を更新（簡易 upsert）
  if [[ -f "$STATE_FILE" ]]; then
    grep -v "^${key}=" "$STATE_FILE" > "${STATE_FILE}.tmp" 2>/dev/null || true
    mv "${STATE_FILE}.tmp" "$STATE_FILE" 2>/dev/null || true
  fi
  echo "${key}=${now}" >> "$STATE_FILE"
  return 0
}

# === 監視ロジック ===
ALERTS=()
SEVERITY="warning"

# メモリ・swap
read -r mem_used mem_total <<<"$(free -m | awk '/^Mem:/ {print $3, $2}')"
swap_used=$(free -m | awk '/^Swap:/ {print $3}')
mem_pct=$((mem_used * 100 / mem_total))

if [[ $mem_pct -ge $MEM_CRIT_PCT ]]; then
  ALERTS+=("MEM CRIT: ${mem_pct}% (${mem_used}/${mem_total} MB)")
  SEVERITY="failure"
elif [[ $mem_pct -ge $MEM_WARN_PCT ]]; then
  ALERTS+=("MEM WARN: ${mem_pct}% (${mem_used}/${mem_total} MB)")
fi

if [[ $swap_used -ge $SWAP_CRIT_MB ]]; then
  ALERTS+=("SWAP CRIT: ${swap_used} MB")
  SEVERITY="failure"
elif [[ $swap_used -ge $SWAP_WARN_MB ]]; then
  ALERTS+=("SWAP WARN: ${swap_used} MB")
fi

# サービス状態
for svc in "${SERVICES[@]}"; do
  if ! systemctl is-active --quiet "$svc"; then
    state=$(systemctl is-active "$svc" 2>/dev/null || echo unknown)
    ALERTS+=("SVC FAIL: $svc=$state")
    SEVERITY="failure"
  fi
done

# OOM ログ（過去 OOM_LOOKBACK）
oom_lines=$(sudo -n journalctl --since="$OOM_LOOKBACK" -k 2>/dev/null \
  | grep -ciE "killed process|oom-killer|out of memory" || true)
if [[ -n "${oom_lines:-}" && "$oom_lines" -gt 0 ]]; then
  ALERTS+=("OOM DETECTED: $oom_lines lines in last $OOM_LOOKBACK")
  SEVERITY="failure"
fi

# === 結果ログ + 通知 ===
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
if [[ ${#ALERTS[@]} -gt 0 ]]; then
  KEY="alert_${SEVERITY}"
  if should_notify "$KEY"; then
    BODY="$(printf '%s\n' "${ALERTS[@]}")"
    notify_ntfy "$SEVERITY" "alpha-strike-01 ${SEVERITY^^}" "$BODY"
  fi
  echo "${TIMESTAMP} [${SEVERITY^^}] ${ALERTS[*]}" >> "$LOG_FILE"
else
  echo "${TIMESTAMP} OK mem=${mem_pct}% swap=${swap_used}MB" >> "$LOG_FILE"
fi
