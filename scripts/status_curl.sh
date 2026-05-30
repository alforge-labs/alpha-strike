#!/usr/bin/env bash
# status_curl.sh — alpha-strike の read-only status API を二段認証で叩く運用ヘルパー (issue #57)
#
# Cloudflare Access (Service Token) + origin Bearer の二段認証を 1 コマンドにまとめ、
# `/status`（口座サマリ + 建玉 + 実 order_status 付き直近注文）や `/status/events` を取得する。
#
# 認証情報の解決順（各値ごと）:
#   1. *_REF 環境変数に op:// 参照があれば 1Password から `op read` で取得:
#        STRIKE_CF_ID_REF / STRIKE_CF_SECRET_REF / STRIKE_STATUS_TOKEN_REF
#   2. 値を直接渡す環境変数:
#        CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET / STATUS_API_TOKEN
#   どちらも無ければエラー終了（値は標準出力に表示しない）。
#
# 使い方:
#   scripts/status_curl.sh             # /status
#   scripts/status_curl.sh events      # /status/events（既定 limit）
#   scripts/status_curl.sh events 20   # /status/events?limit=20
#
# 環境変数:
#   STRIKE_BASE_URL   既定 https://strike.alforgelabs.com
#
# 設定例（~/.zshrc 等に追記。実値は 1Password に置く）:
#   export STRIKE_CF_ID_REF="op://AlphaTrade/alpha-strike-status/CF-Access-Client-Id"
#   export STRIKE_CF_SECRET_REF="op://AlphaTrade/alpha-strike-status/CF-Access-Client-Secret"
#   export STRIKE_STATUS_TOKEN_REF="op://AlphaTrade/alpha-strike/STATUS_API_TOKEN"
set -euo pipefail

BASE="${STRIKE_BASE_URL:-https://strike.alforgelabs.com}"

_resolve() { # $1=ref-env-name $2=value-env-name $3=label
  local ref="${!1:-}" val="${!2:-}"
  if [[ -n "$ref" ]]; then
    op read "$ref" 2>/dev/null || { echo "ERROR: op read '$ref' に失敗しました（$3）" >&2; exit 1; }
  elif [[ -n "$val" ]]; then
    printf '%s' "$val"
  else
    echo "ERROR: $3 が未設定です（$1 に op:// 参照、または $2 に値を設定してください）" >&2
    exit 1
  fi
}

case "${1:-status}" in
  status)    EP="/status" ;;
  events)    EP="/status/events${2:+?limit=$2}" ;;
  -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
  *) echo "usage: $0 [status | events [limit]]" >&2; exit 2 ;;
esac

CID=$(_resolve STRIKE_CF_ID_REF CF_ACCESS_CLIENT_ID "CF Access Client ID")
CSEC=$(_resolve STRIKE_CF_SECRET_REF CF_ACCESS_CLIENT_SECRET "CF Access Client Secret")
TOK=$(_resolve STRIKE_STATUS_TOKEN_REF STATUS_API_TOKEN "STATUS_API_TOKEN")

resp=$(curl -s -w $'\n%{http_code}' \
  -H "CF-Access-Client-Id: $CID" \
  -H "CF-Access-Client-Secret: $CSEC" \
  -H "Authorization: Bearer $TOK" \
  "$BASE$EP")
code="${resp##*$'\n'}"
body="${resp%$'\n'*}"

echo "HTTP $code  ($BASE$EP)"
if command -v jq >/dev/null 2>&1; then
  echo "$body" | jq . 2>/dev/null || echo "$body"
else
  echo "$body"
fi
[[ "$code" == 2* ]] || exit 1
