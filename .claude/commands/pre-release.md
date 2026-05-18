---
name: pre-release
description: PyPI リリース前のローカル検証 → リリース実行
command: true
---

# pre-release コマンド

alpha-strike を PyPI に公開する前にローカルで lint / test / build を検証し、問題なければリリースする。

## 使い方

```
/pre-release [patch|minor|major]
```

引数を省略した場合は `patch` として扱う。

## 実行手順

1. **作業ディレクトリの確認**

   alpha-strike リポジトリのルートにいること、かつ `main` ブランチにいることを確認する。

   ```bash
   git branch --show-current
   git status
   ```

   未コミットの変更があれば中断してユーザーに確認を求める。

2. **ローカル検証**

   ```bash
   uv sync --all-groups
   uv run ruff check .
   uv run pytest -q
   uv build
   ```

   wheel / sdist が `dist/alpha_strike-*.whl` / `dist/alpha_strike-*.tar.gz` として生成されることを確認。
   失敗した場合は中断してエラー内容をユーザーに報告する。

3. **バージョン確認**

   現在のバージョンと、バンプ後のバージョンをユーザーに提示して確認を求める。

   ```bash
   grep '^version' pyproject.toml
   ```

4. **リリース実行**

   ユーザーの承認を得てから実行する。

   ```bash
   bash release.sh ${PART}
   ```

   `release.sh` がタグを push すると `.github/workflows/release.yml` が走り、
   PyPI Trusted Publisher 経由で wheel / sdist が PyPI に公開される。

## 注意

- `release.sh` は `git push --tags` まで実行するため、実行前に必ずユーザーの承認を取ること。
- `git-cliff` と `bump-my-version` が必要（`uv sync --all-groups` で導入済み）。
- PyPI 公開には事前に PyPI の Trusted Publisher 設定で `alforge-labs/alpha-strike` を登録する必要がある。
