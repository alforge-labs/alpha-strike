# セットアップ手順

## 前提条件

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) インストール済み

## uv のインストール

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## プロジェクトのセットアップ

```bash
# リポジトリをクローン
git clone <repo-url>
cd alpha-strike

# 依存パッケージをインストール（uv が自動で仮想環境を作成）
uv sync
```

## 環境変数の設定

```bash
cp .env.example .env
```

`.env` を編集して各証券会社の認証情報を設定してください。

```
WEBHOOK_PASSPHRASE=your-secret-passphrase   # TradingViewアラートと同じ値を設定

# IG証券（IG証券を使う場合のみ）
IG_API_KEY=your-ig-api-key
IG_USERNAME=your-ig-username
IG_PASSWORD=your-ig-password
IG_ACC_TYPE=DEMO   # テスト時は必ずDEMO

# moomoo証券（moomooを使う場合のみ）
MOOMOO_HOST=127.0.0.1
MOOMOO_PORT=11111
MOOMOO_TRD_ENV=SIMULATE   # テスト時は必ずSIMULATE
```

> **重要**: `.env` は絶対にGitにコミットしないでください。`.gitignore` で除外済みです。

## サーバー起動

```bash
uv run uvicorn webhook_server:app --host 0.0.0.0 --port 8080 --reload
```

開発時は `--reload` オプションでコード変更を自動反映。
本番環境では `--reload` を外してください。

## 動作確認

サーバー起動後、以下でヘルスチェック:

```bash
curl http://localhost:8080/health
# → {"status":"ok"}
```

Swagger UI でAPIドキュメントを確認:

```
http://localhost:8080/docs
```

## moomoo証券を使う場合の追加手順

moomoo証券での発注には OpenD のローカル起動が必要です。
詳細は [moomoo_futud.md](./moomoo_futud.md) を参照してください。
