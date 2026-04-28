# セットアップ手順

## 前提条件

- Python 3.12+
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

<!-- AUTO-GENERATED from .env.example -->
| 変数名 | 必須 | 説明 | デフォルト / 例 |
|-------|:----:|------|----------------|
| `WEBHOOK_PASSPHRASE` | **Yes** | TradingView アラート認証パスフレーズ | `your-secret-passphrase` |
| `OANDA_API_KEY` | No | OANDA Personal Access Token | — |
| `OANDA_ACCOUNT_ID` | No | OANDA 口座 ID | — |
| `OANDA_ENV` | No | OANDA 環境（`PRACTICE` / `LIVE`） | `PRACTICE` |
| `MOOMOO_HOST` | No | moomoo OpenD ホスト | `127.0.0.1` |
| `MOOMOO_PORT` | No | moomoo OpenD ポート | `11111` |
| `MOOMOO_TRD_ENV` | No | moomoo 取引環境（`SIMULATE` / `REAL`） | `SIMULATE` |
| `LIVE_EVENTS_PATH` | No | イベントログ保存先（Docker volumes と一致させること） | `./data/events` |
<!-- /AUTO-GENERATED -->

> **重要**: `.env` は絶対にGitにコミットしないでください。`.gitignore` で除外済みです。  
> 1Password CLI を使う場合は `op run --env-file=.env.op -- uv run ...` で実行してください。

## Docker でのデプロイ

> **重要:** `data/events/` ディレクトリは Docker コンテナ内の `appuser`（UID 1001）が書き込む必要があります。
> 初回デプロイ前に以下を実行してください:
> ```bash
> mkdir -p ./data/events
> chown -R 1001:1001 ./data/events
> ```

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

---

## リリース手順

リリースは **ローカルビルド検証 → バージョンバンプ → タグ push** の順で行う。

### 事前確認

- `main` ブランチにいること、かつ未コミットの変更がないことを確認する
- `git-cliff` がインストールされていること（`brew install git-cliff`）
- `uv sync --all-groups` で依存関係が最新であること

### ステップ 1: ローカルビルド検証

```bash
bash verify-build.sh
```

PyInstaller で `dist/alpha-strike` バイナリを生成し、起動確認を行う。

### ステップ 2: リリース実行

```bash
bash release.sh patch   # パッチバージョン（バグ修正）
bash release.sh minor   # マイナーバージョン（機能追加）
bash release.sh major   # メジャーバージョン（破壊的変更）
```

内部処理：
1. `bump-my-version` で `pyproject.toml` のバージョンをバンプし、git タグを作成
2. `git-cliff` で `CHANGELOG.md` を自動生成してコミット
3. `git push && git push --tags` でリモートへ送信
