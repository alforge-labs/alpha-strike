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

echo "  alpha-strike --help"
"$BINARY" --help > /dev/null

echo ""
echo "=== バイナリ情報 ==="
ls -lh "$BINARY"
file "$BINARY"

echo ""
echo "=== ビルド検証完了 ==="
echo "問題がなければ bash release.sh [patch|minor|major] でリリースしてください。"
