#!/bin/bash
# ローカルビルド検証スクリプト
# リリース前にバイナリを生成してスモークテストを実行する
# 使い方: bash verify-build.sh
set -euo pipefail

BINARY="dist/alpha-strike"

echo "=== 依存関係の同期 ==="
uv sync --all-groups

echo ""
echo "=== PyInstaller ビルド ==="
uv run pyinstaller alpha-strike.spec --noconfirm

echo ""
echo "=== スモークテスト ==="

# main.py には CLI フラグ (--help 等) がなく直接 uvicorn を起動するため、
# ダミー passphrase を設定して /health が 200 を返すかで起動可否を判定する。
SMOKE_PORT=8765   # 8080 の衝突を避けるため固定の検証用ポート
echo "  alpha-strike を 127.0.0.1:${SMOKE_PORT} で起動し /health の 200 を確認..."
SMOKE_LOG="/tmp/alpha-strike-smoke.log"
ALPHA_STRIKE_HOST="127.0.0.1" \
    ALPHA_STRIKE_PORT="$SMOKE_PORT" \
    WEBHOOK_PASSPHRASE="verify-build-smoke" \
    "$BINARY" > "$SMOKE_LOG" 2>&1 &
SMOKE_PID=$!
trap 'kill "$SMOKE_PID" 2>/dev/null || true' EXIT

# 起動を最大 30 秒待つ（PyInstaller のコールドスタートは futu/moomoo の import で 10-15 秒かかる）
for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${SMOKE_PORT}/health" > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

if ! curl -sf "http://127.0.0.1:${SMOKE_PORT}/health" > /dev/null 2>&1; then
    echo "  ❌ /health が 200 を返しません。直近のログ:"
    tail -30 "$SMOKE_LOG"
    exit 1
fi

echo "  ✓ /health 200 OK"
kill "$SMOKE_PID" 2>/dev/null || true
wait "$SMOKE_PID" 2>/dev/null || true
trap - EXIT

echo ""
echo "=== バイナリ情報 ==="
ls -lh "$BINARY"
file "$BINARY"

echo ""
echo "=== ビルド検証完了 ==="
echo "問題がなければ bash release.sh [patch|minor|major] でリリースしてください。"
