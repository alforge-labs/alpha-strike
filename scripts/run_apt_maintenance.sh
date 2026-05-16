#!/usr/bin/env bash
# run_apt_maintenance.sh
#
# VM (oracle-strike) で apt の週次 full upgrade + 再起動制御 + ntfy 通知を行う。
#
# unattended-upgrades は既に security パッチを毎日適用しているが、
# kernel など再起動を伴う更新や全パッケージの up-to-date 化は別途必要。
# 本スクリプトは毎週日曜 04:30 (JST、oracle-bot の 30 分後) に実行し、結果を
# ntfy.sh で通知する。
#
# VM cron entry 例:
#   30 4 * * 0 /home/ubuntu/dev/alpha-strike/scripts/run_apt_maintenance.sh
#
# 前提:
# - ubuntu ユーザーが NOPASSWD sudo を持つこと (Ubuntu Cloud Image の既定)
# - ~/.ntfy.env に NTFY_TOPIC=<topic> が定義されていること (mode 600 推奨)
# - alpha-strike VM には 1Password CLI を入れていないため curl 直叩きで通知

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

LOG_DIR="$HOME/var/log/alphastrike"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/apt-maintenance.log"

# NTFY_TOPIC を ~/.ntfy.env から取り込む (cron 環境では env 継承されない)
NTFY_ENV_FILE="$HOME/.ntfy.env"
if [[ -f "$NTFY_ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  . "$NTFY_ENV_FILE"
fi

# ntfy 通知ヘルパー (NTFY_TOPIC 未設定時は no-op)
# Usage: notify_ntfy <success|warning|failure> <title> <body>
notify_ntfy() {
  local status="$1" title="$2" body="$3"
  if [[ -z "${NTFY_TOPIC:-}" ]]; then
    echo "[$(date '+%H:%M:%S')] [WARN] NTFY_TOPIC 未設定、通知をスキップ" | tee -a "$LOG_FILE"
    return 0
  fi
  local tags priority
  case "$status" in
    success)
      tags="rocket,white_check_mark"
      priority="default"
      ;;
    warning)
      tags="warning,white_check_mark"
      priority="default"
      ;;
    *)
      tags="warning,x"
      priority="high"
      ;;
  esac
  if curl -fsS --max-time 10 \
        -H "Title: $title" \
        -H "Tags: $tags" \
        -H "Priority: $priority" \
        -d "$body" \
        "https://ntfy.sh/$NTFY_TOPIC" > /dev/null 2>&1; then
    echo "[$(date '+%H:%M:%S')] notify($status) sent" | tee -a "$LOG_FILE"
  else
    echo "[$(date '+%H:%M:%S')] [WARN] notify($status) failed (curl exit=$?)" | tee -a "$LOG_FILE" >&2
  fi
}

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === apt-maintenance start ===" | tee -a "$LOG_FILE"

# Phase 1: apt-get update
RESULT="success"
if sudo -n apt-get update >> "$LOG_FILE" 2>&1; then
  echo "[$(date '+%H:%M:%S')] apt-get update OK" | tee -a "$LOG_FILE"
else
  RESULT="failure"
  echo "[$(date '+%H:%M:%S')] [ERROR] apt-get update failed" | tee -a "$LOG_FILE"
fi

# Phase 2: アップグレード予定数の事前カウント
UPGRADABLE=0
if [[ "$RESULT" == "success" ]]; then
  UPGRADABLE=$(sudo -n apt-get -s upgrade 2>/dev/null \
               | grep -c "^Inst" || true)
  echo "[$(date '+%H:%M:%S')] アップグレード予定: $UPGRADABLE packages" | tee -a "$LOG_FILE"
fi

# Phase 3: apt-get upgrade (非対話)
if [[ "$RESULT" == "success" ]] && (( UPGRADABLE > 0 )); then
  if sudo -n DEBIAN_FRONTEND=noninteractive apt-get -y \
       -o Dpkg::Options::="--force-confdef" \
       -o Dpkg::Options::="--force-confold" \
       upgrade >> "$LOG_FILE" 2>&1; then
    echo "[$(date '+%H:%M:%S')] apt-get upgrade OK" | tee -a "$LOG_FILE"
  else
    RESULT="warning"
    echo "[$(date '+%H:%M:%S')] [WARN] apt-get upgrade had non-zero exit" | tee -a "$LOG_FILE"
  fi
  # 不要パッケージ整理
  sudo -n DEBIAN_FRONTEND=noninteractive apt-get -y autoremove \
       >> "$LOG_FILE" 2>&1 || true
fi

# Phase 4: 再起動判定
REBOOT_REQUIRED=0
REBOOT_PKGS=""
if [[ -f /var/run/reboot-required ]]; then
  REBOOT_REQUIRED=1
  if [[ -f /var/run/reboot-required.pkgs ]]; then
    REBOOT_PKGS=$(tr '\n' ',' < /var/run/reboot-required.pkgs | sed 's/,$//')
  fi
fi

# Phase 5: 結果通知
BODY="upgraded: ${UPGRADABLE} packages
reboot_required: ${REBOOT_REQUIRED}
status: ${RESULT}"
if [[ -n "$REBOOT_PKGS" ]]; then
  BODY="${BODY}
pkgs: ${REBOOT_PKGS}"
fi

notify_ntfy "$RESULT" "AlphaStrike oracle-strike: apt maintenance" "$BODY"

# Phase 6: 再起動 (60 秒後)
if [[ "$REBOOT_REQUIRED" == "1" ]] && [[ "$RESULT" != "failure" ]]; then
  echo "[$(date '+%H:%M:%S')] 再起動が必要。60 秒後に reboot します..." | tee -a "$LOG_FILE"
  notify_ntfy "warning" \
    "AlphaStrike oracle-strike: 60 秒後に再起動します ♻️" \
    "kernel/system パッケージ更新のため再起動します。
cron / systemd 自動復帰のため数分後に運用再開予定。
pkgs: ${REBOOT_PKGS:-(unknown)}"
  sleep 60
  echo "[$(date '+%H:%M:%S')] systemctl reboot" | tee -a "$LOG_FILE"
  sudo -n systemctl reboot
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === apt-maintenance done ===" | tee -a "$LOG_FILE"
